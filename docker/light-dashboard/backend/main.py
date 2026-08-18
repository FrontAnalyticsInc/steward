import os
import re
import json
import secrets
import sqlite3
import time
import httpx
from collections import OrderedDict
import tempfile
import datetime
import yaml
from fastapi import FastAPI, HTTPException, Request, Depends, Query
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from typing import Optional, List, Any

from .review_types import (
    GENERIC_TYPE,
    REJECT_ACTION,
    REVIEW_TYPE_LABELS,
    actions_for,
    find_action,
    legal_action_ids,
    reject_action_for,
    review_type_of,
)

from . import (
    chat_transcript,
    adk_introspect,
    adk_live,
    adk_scorecard,
    adk_cron_link,
    automation_health,
    cron_watchdog,
    health,
    integrations,
    channels,
    kanban_review,
    mcp_servers,
    metrics_store,
    review_flow,
    wiki_api,
    settings_integrations,
)

app = FastAPI(title="Hermes Unified Light Dashboard API")

# CORS. This was `allow_origins=["*"]` with credentials, which was survivable
# while the most a cross-origin POST could do was move a JSON file between two
# directories. It is not survivable now: approving a review item sends mail as
# alton@, so any page open in this browser could have mailed a client.
#
# The service listens on 0.0.0.0 by design (it is reached from other machines on
# the LAN), so "it's only local" was never the control it sounded like.
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "DASHBOARD_ALLOWED_ORIGINS",
        "http://localhost:9120,http://127.0.0.1:9120",
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# The second half of the control, for the routes that now *do* something.
#
# CORS alone is not enough: it is enforced by the browser on the response, so a
# simple request (form post, no preflight) still reaches the handler and still
# has its side effect — the attacker never needs to read the reply. Requiring a
# custom header forces a preflight, and a preflight this origin list refuses
# means the request is never sent at all.
#
# Sec-Fetch-Site is the belt to that suspenders: browsers set it themselves and
# script cannot forge it. Absent (curl, an older browser) we fall back to the
# header requirement alone, which is what keeps the documented curl examples in
# docs/automations/review.md working.
REVIEW_CONFIRM_HEADER = "x-review-confirm"


def require_same_origin(request: Request):
    """Gate the review write routes.

    Applied to decision/retry/dismiss — every route that can cause an outbound
    side effect. Read routes stay open; listing the queue is not the dangerous
    part.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "same-site", "none"):
        raise HTTPException(
            status_code=403,
            detail="Cross-site requests may not decide review items.",
        )
    if request.headers.get(REVIEW_CONFIRM_HEADER) != "1":
        raise HTTPException(
            status_code=403,
            detail=(
                "Missing X-Review-Confirm: 1. This header is required on review "
                "write routes so the request cannot be made without a preflight."
            ),
        )
    origin = request.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        raise HTTPException(status_code=403, detail=f"Origin {origin} is not allowed.")
    return True


# The frontend is baked into the image, so a rebuild changes index.html behind
# a URL the browser already cached. Tell it never to reuse the page.
@app.middleware("http")
async def no_store_frontend(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


DB_DIR = "/opt/data"
STATE_DB = os.path.join(DB_DIR, "state.db")
KANBAN_DB = os.path.join(DB_DIR, "kanban.db")
CRON_JOBS_FILE = os.path.join(DB_DIR, "cron", "jobs.json")
# The gateway's own API server. Chat and the cron trigger both go through it —
# it is the only process that may act on the agent, and this dashboard holds no
# scheduler of its own.
HERMES_API_BASE = os.getenv("HERMES_API_BASE", "http://localhost:8642").rstrip("/")
HERMES_API_URL = f"{HERMES_API_BASE}/v1/chat/completions"
API_SERVER_KEY = os.getenv("API_SERVER_KEY", "some_long_secure_secret_key_here")

# Run traces, written by hermes/scripts/invoke_workflow.py on every invocation
# and read back by adk_scorecard. Independent of which server ran the app.
ADK_STATE_DIR = os.getenv("ADK_STATE_DIR", os.path.join(DB_DIR, "adk"))
# The ADK server (hermes-workflows, the agents-cli project). Described via its
# own app-info where it can, parsed from the bind-mounted source where it
# cannot — see adk_teams(). Set empty to hide. 127.0.0.1 rather than a service
# name because every container here runs network_mode: host.
ADK_WORKFLOWS_URL = os.getenv("ADK_WORKFLOWS_URL", "http://127.0.0.1:8020")

# Per-call outcome records for the workflow side, written by
# workflows/app/integration_log.py into the ADK state directory. The gateway
# side has no equivalent — see integrations.gaps().
INTEGRATION_CALL_LOG_DIR = os.getenv(
    "INTEGRATION_CALL_LOG_DIR",
    os.path.join(ADK_STATE_DIR, integrations.CALL_LOG_DIRNAME),
)
# Optional. Supplies only what no log can state: a human label, the credential
# type, and how often a grant is expected to run. Lives beside the other Hermes
# state so it survives an image rebuild.
INTEGRATIONS_CONFIG = os.getenv(
    "INTEGRATIONS_CONFIG", os.path.join(DB_DIR, "integrations.json")
)
# Which consumer the chat screen is. The gateway records a chat session's
# source as `api_server`, and the chat sidebar asks "what can this conversation
# reach" — the same grants, read from the consumer end.
CHAT_CONSUMER = os.getenv("CHAT_CONSUMER", "api_server")

# Hermes skills tree and config (both live under the mounted ~/.hermes)
SKILLS_DIR = os.path.join(DB_DIR, "skills")
HERMES_CONFIG_FILE = os.path.join(DB_DIR, "config.yaml")
# Skills declaring platforms that exclude this host are never loaded by Hermes
HOST_PLATFORM = "linux"

# The agent identity file, and dirs to skip when discovering context markdown.
# skills/ is vendored per-skill docs; hermes-agent/ is vendor source; the rest
# is cache. What survives is what actually shapes the agent: SOUL.md, memories.
SOUL_FILE = os.path.join(DB_DIR, "SOUL.md")
CONTEXT_SKIP_DIRS = {
    "skills", "hermes-agent", "node_modules", "__pycache__",
    "cache", "audio_cache", "image_cache", "state-snapshots",
    "logs",
    # cron/output holds one markdown file per scheduled run — a run log, not
    # context. Left in, it buried SOUL.md and USER.md under 31 transcripts.
    "cron",
    # Vendored dependency trees ship their own READMEs/LICENSEs — noise, not context.
    "lazy-packages", "site-packages", ".venv", "bin",
    # Each profile is a separate agent with its own home; it is surfaced under
    # /api/agents/<name>/... rather than flattened into the default agent's list.
    "profiles",
    # wiki/ is the memory store — one markdown file per contact, written by the
    # workflows service and growing without bound. It has its own tab, which
    # renders it as records rather than as files. Listing it here was a display
    # artifact of walking the whole home for *.md, and the same artifact is what
    # buried SOUL.md under cron transcripts above. It also invited a reasonable
    # but wrong conclusion — that these files were being fed to the agent.
    # They are not: prompt_builder reads exactly one file out of HERMES_HOME,
    # SOUL.md, plus project context from the working directory. The store stays
    # where it is precisely because the home is what every container mounts.
    "wiki",
}

# Every profile is a full Hermes home (config.yaml, SOUL.md, skills/, memories/,
# cron/, sessions/). "default" is HERMES_HOME itself; named profiles live under
# HERMES_HOME/profiles/<name>.
PROFILES_DIR = os.path.join(DB_DIR, "profiles")
DEFAULT_AGENT = "default"

# Review queue directories (inside /approvals).
#
# The directory an item sits in IS its state; nothing is mutated in place. The
# path is still /approvals on disk — renaming the mount would strand the live
# queue for no gain, and the storage contract is what producers write against.
#
#   pending/    written by producers. The only directory they can write.
#   approved/   written by the dashboard, on a human keystroke. Nothing else.
#   rejected/   terminal.
#   executing/  claimed by the executor, exclusive-create.
#   executed/   the action was performed.
#   failed/     the action was attempted and did not succeed.
APPROVAL_ROOT = "/approvals"
APPROVAL_PENDING_DIR = os.path.join(APPROVAL_ROOT, "pending")
APPROVAL_APPROVED_DIR = os.path.join(APPROVAL_ROOT, "approved")
APPROVAL_REJECTED_DIR = os.path.join(APPROVAL_ROOT, "rejected")
APPROVAL_EXECUTING_DIR = os.path.join(APPROVAL_ROOT, "executing")
APPROVAL_EXECUTED_DIR = os.path.join(APPROVAL_ROOT, "executed")
APPROVAL_FAILED_DIR = os.path.join(APPROVAL_ROOT, "failed")
APPROVAL_HEALTH_FILE = os.path.join(APPROVAL_ROOT, "health.json")

# Ordered so a lookup finds the most recent meaning of an id first.
REVIEW_STATE_DIRS = [
    ("pending", APPROVAL_PENDING_DIR),
    ("failed", APPROVAL_FAILED_DIR),
    ("executing", APPROVAL_EXECUTING_DIR),
    ("approved", APPROVAL_APPROVED_DIR),
    ("executed", APPROVAL_EXECUTED_DIR),
    ("rejected", APPROVAL_REJECTED_DIR),
]

# Producers write 0644 (workflows/app/approvals.py:ITEM_MODE) because they run as
# root and the reviewer does not. The same applies in the other direction: the
# executor is a separate container with its own uid, and mkstemp creates 0600
# which os.replace preserves. An unreadable decision is a silently stuck queue.
ITEM_MODE = 0o644

# A file claimed but not finished within this long is treated as stalled. It is
# never retried automatically — see the executor for why an ambiguous send has
# to be assumed sent.
EXECUTION_LEASE_SECONDS = int(os.environ.get("REVIEW_EXECUTION_LEASE", "600"))

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None

class ApprovalDecisionRequest(BaseModel):
    id: str
    decision: str
    # Which of the type's actions to perform. Absent means "the primary one",
    # which is what the legacy /api/approvals/decision callers meant by approve.
    action: Optional[str] = None
    edited_body: Optional[str] = None
    rejection_reason: Optional[str] = None

    @field_validator("decision")
    @classmethod
    def _known_decision(cls, v):
        # Was unvalidated, and the handler read `approved if decision ==
        # 'approve' else rejected` — so a typo, or anything at all, filed the
        # item as rejected and reported success.
        if v not in ("approve", "reject"):
            raise ValueError("decision must be 'approve' or 'reject'")
        return v


class ReviewItemActionRequest(BaseModel):
    id: str

# Helper to execute sqlite queries safely
def query_db(db_path: str, query: str, args=(), one=False):
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, args)
        rv = cur.fetchall()
        cur.close()
        conn.close()
        return (rv[0] if rv else None) if one else rv
    except Exception as e:
        print(f"Database error on {db_path}: {e}")
        return []

# Helper to execute sqlite write operations safely
def execute_db(db_path: str, query: str, args=()):
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(query, args)
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Write database error on {db_path}: {e}")
        return False

# --- Unread tracking ---------------------------------------------------------
#
# `sessions.last_read_at` is in the gateway's schema but nothing writes it, so
# the dashboard owns it: the timestamp of the last time a human looked at the
# conversation. Anything the agent has said since is unread — which is the
# point, because a cron job wakes up, works a session and finishes with nobody
# watching, and until now the only trace was a new row in a list of 50.
UNREAD_BASELINE_MARKER = os.path.join(DB_DIR, ".dashboard_unread_baseline")


def ensure_unread_baseline():
    """Treat everything that already exists as read, once.

    Every one of these sessions predates unread tracking, so without a baseline
    the feature would arrive announcing ninety-odd unread conversations — which
    says nothing and trains you to ignore the badge on day one.

    Guarded by a marker file rather than by "is the column still NULL", because
    the backend runs under --reload: re-running this on every code edit would
    quietly mark as read exactly the sessions that had gone unread since the
    last edit.
    """
    if os.path.exists(UNREAD_BASELINE_MARKER):
        return
    if not os.path.exists(STATE_DB):
        return  # No database yet; the next start will lay the baseline down.
    # Dated from the session's own last message rather than from
    # `last_activity_at`, which is NULL on half these rows and, where it is set,
    # is stamped a few seconds before the reply it belongs to — either way it
    # leaves the newest message looking unread and the baseline achieving
    # nothing. The message timestamps are the only record that cannot disagree
    # with what the unread query counts, because it counts the same rows.
    execute_db(
        STATE_DB,
        "UPDATE sessions SET last_read_at = COALESCE("
        "  (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = sessions.id),"
        "  last_activity_at, started_at, ?) "
        "WHERE last_read_at IS NULL",
        (time.time(),),
    )
    try:
        with open(UNREAD_BASELINE_MARKER, "w") as fh:
            fh.write(str(time.time()))
    except OSError as ex:
        # Without the marker the next start would re-baseline and wipe real
        # unread state, so a session started from here on is better off simply
        # never being counted than being counted once and then silently reset.
        print(f"Could not write unread baseline marker: {ex}")


def _unread_counts(session_ids: List[str]) -> dict:
    """Assistant messages per session newer than that session's last_read_at.

    Assistant messages only: tool calls and the prompt that triggered a cron run
    are how the reply got made, not something waiting to be read. Matched to
    `active = 1`, the same filter the transcript endpoint renders, so the badge
    cannot promise a message the session will not show.
    """
    if not session_ids:
        return {}
    placeholders = ",".join("?" for _ in session_ids)
    rows = query_db(
        STATE_DB,
        f"SELECT m.session_id AS session_id, COUNT(*) AS n "
        f"FROM messages m JOIN sessions s ON s.id = m.session_id "
        f"WHERE m.session_id IN ({placeholders}) "
        f"  AND m.role = 'assistant' AND m.active = 1 "
        f"  AND m.timestamp > COALESCE(s.last_read_at, 0) "
        f"GROUP BY m.session_id",
        tuple(session_ids),
    )
    return {r["session_id"]: r["n"] for r in rows}


def _tag_unread(sessions: List[dict]) -> List[dict]:
    counts = _unread_counts([s.get("id") for s in sessions if s.get("id")])
    for s in sessions:
        s["unread_count"] = counts.get(s.get("id"), 0)
    return sessions


@app.post("/api/sessions/{session_id}/read")
def mark_session_read(session_id: str):
    """Called when the conversation is on screen in front of someone."""
    ok = execute_db(
        STATE_DB,
        "UPDATE sessions SET last_read_at = ? WHERE id = ?",
        (time.time(), session_id),
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to mark session read")
    return {"status": "success", "id": session_id}


def ensure_approval_dirs():
    """Ensure all required review-queue directories exist.

    Mode is explicit for the same reason item files set theirs: the producer,
    the dashboard and the executor are three containers with three uids, and a
    directory created 0700 by whichever got there first stops the other two.
    """
    for _state, d in REVIEW_STATE_DIRS:
        os.makedirs(d, exist_ok=True)
        try:
            os.chmod(d, 0o755)
        except OSError:
            # A read-only mount is legitimate — the executor mounts approved/
            # that way on purpose. Not being able to widen it is not an error.
            pass


# Laid down before the first request, so nothing is ever counted unread purely
# because it is older than the feature.
ensure_unread_baseline()

# --- Core API Endpoints ---

@app.get("/api/sessions")
def get_sessions(search: Optional[str] = Query(None)):
    # 1. Run the 30-day auto-archive query
    auto_archive_query = """
    UPDATE sessions 
    SET archived = 1 
    WHERE (archived = 0 OR archived IS NULL) 
      AND datetime(started_at) < datetime('now', '-30 days')
    """
    execute_db(STATE_DB, auto_archive_query)

    # 2. Query sessions with optional LIKE search
    if search:
        search_pattern = f"%{search}%"
        query = """
        SELECT id, title, started_at, message_count, source 
        FROM sessions 
        WHERE (archived = 0 OR archived IS NULL)
          AND (title LIKE ? OR id LIKE ?)
        ORDER BY started_at DESC 
        LIMIT 50
        """
        rows = query_db(STATE_DB, query, (search_pattern, search_pattern))
    else:
        query = """
        SELECT id, title, started_at, message_count, source 
        FROM sessions 
        WHERE archived = 0 OR archived IS NULL
        ORDER BY started_at DESC
        LIMIT 50
        """
        rows = query_db(STATE_DB, query)

    return _tag_unread(_tag_kanban_sessions([dict(ix) for ix in rows]))


def _tag_kanban_sessions(sessions: List[dict]) -> List[dict]:
    """Mark the sessions a dispatcher opened to work a task, with which task.

    The other half of the task↔chat link. A `kanban` session otherwise appears in
    the list as an untitled conversation with no indication that it is an agent
    working a board item, which is the one thing worth knowing about it.

    One query for the whole page rather than one per session: this runs on the
    7s poll.
    """
    ids = [s.get("id") for s in sessions if s.get("source") == "kanban" and s.get("id")]
    if not ids:
        return sessions
    placeholders = ",".join("?" for _ in ids)
    rows = query_db(
        STATE_DB,
        f"SELECT session_id, content FROM messages WHERE role = 'user' "
        f"AND session_id IN ({placeholders}) AND content LIKE 'work kanban task %'",
        tuple(ids),
    )
    by_session = {}
    for r in rows:
        match = re.match(r"work kanban task (\S+)", r["content"] or "")
        if match:
            by_session.setdefault(r["session_id"], match.group(1))
    for s in sessions:
        if s.get("id") in by_session:
            s["kanban_task_id"] = by_session[s["id"]]
    return sessions


@app.get("/api/sessions/archived")
def get_archived_sessions(search: Optional[str] = Query(None)):
    # Query only archived sessions (archived = 1)
    if search:
        search_pattern = f"%{search}%"
        query = """
        SELECT id, title, started_at, message_count, source 
        FROM sessions 
        WHERE archived = 1
          AND (title LIKE ? OR id LIKE ?)
        ORDER BY started_at DESC 
        LIMIT 50
        """
        rows = query_db(STATE_DB, query, (search_pattern, search_pattern))
    else:
        query = """
        SELECT id, title, started_at, message_count, source 
        FROM sessions 
        WHERE archived = 1
        ORDER BY started_at DESC 
        LIMIT 50
        """
        rows = query_db(STATE_DB, query)

    return _tag_kanban_sessions([dict(ix) for ix in rows])

@app.post("/api/sessions/{session_id}/archive")
def archive_session(session_id: str):
    # Archive a single session manually
    query = "UPDATE sessions SET archived = 1 WHERE id = ?"
    success = execute_db(STATE_DB, query, (session_id,))
    if not success:
        raise HTTPException(status_code=500, detail="Failed to archive session in database.")
    return {"status": "success", "id": session_id}

def _messages_columns():
    """Which of the columns we want this state.db actually has.

    Read per request rather than cached: the gateway owns this schema and can
    gain columns under a running dashboard when its image is updated.
    """
    info = query_db(STATE_DB, "PRAGMA table_info(messages)")
    return chat_transcript.select_columns([row["name"] for row in info])


@app.get("/api/sessions/{session_id}/messages")
def get_session_messages(session_id: str):
    """A session's history, shaped for display rather than dumped raw.

    The transcript is the agent's working record: tool results are stored as
    the tool's own JSON, and a tool-calling turn stores an assistant row with
    no content. Handing those straight to a chat panel fills it with JSON and
    empty bubbles — see chat_transcript for what each row becomes instead.
    """
    columns = ", ".join(_messages_columns())
    query = f"""
    SELECT {columns}
    FROM messages
    WHERE session_id = ? AND active = 1
    ORDER BY timestamp ASC
    """
    rows = query_db(STATE_DB, query, (session_id,))
    return chat_transcript.shape(rows)

@app.get("/api/kanban")
def get_kanban_tasks(search: Optional[str] = Query(None)):
    # Query Kanban tasks with optional search filtering
    if search:
        search_pattern = f"%{search}%"
        query = """
        SELECT id, title, body, status, priority, assignee, created_at, result, branch_name, skills 
        FROM tasks 
        WHERE title LIKE ? OR body LIKE ? OR assignee LIKE ? OR id LIKE ?
        ORDER BY created_at DESC
        """
        rows = query_db(KANBAN_DB, query, (search_pattern, search_pattern, search_pattern, search_pattern))
    else:
        query = """
        SELECT id, title, body, status, priority, assignee, created_at, result, branch_name, skills 
        FROM tasks 
        ORDER BY created_at DESC
        """
        rows = query_db(KANBAN_DB, query)

    return [dict(ix) for ix in rows]


def _kanban_session_id(task_id: str) -> Optional[str]:
    """The chat session an agent worked this task in, if there was one.

    `tasks.session_id` is only set for tasks created from inside an ACP loop; a
    task filed by a script and picked up by the dispatcher has it null even
    though a session exists. The dispatcher opens that session with a first user
    message of exactly `work kanban task <id>`, so the join is on that rather
    than on a timestamp window — two tasks claimed in the same second would make
    a time match pick the wrong one.
    """
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", task_id or ""):
        return None
    rows = query_db(
        STATE_DB,
        "SELECT session_id FROM messages WHERE role = 'user' AND content LIKE ? "
        "ORDER BY timestamp ASC LIMIT 1",
        (f"work kanban task {task_id}%",),
    )
    return rows[0]["session_id"] if rows else None


@app.get("/api/kanban/{task_id}")
def get_kanban_task_detail(task_id: str):
    """One task with everything an agent left on it.

    The board list carries only the task row, so the agent's actual findings —
    the comment explaining what it looked at, and the reason it blocked — were
    written and then invisible. A blocked task whose reason you cannot read is
    indistinguishable from one that simply stopped.
    """
    rows = query_db(
        KANBAN_DB,
        "SELECT id, title, body, status, priority, assignee, created_by, created_at, "
        "started_at, completed_at, result, branch_name, skills, workspace_kind, "
        "workspace_path, block_kind, block_recurrences, last_failure_error, session_id "
        "FROM tasks WHERE id = ?",
        (task_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Task not found")
    task = dict(rows[0])

    task["comments"] = [dict(r) for r in query_db(
        KANBAN_DB,
        "SELECT author, body, created_at FROM task_comments WHERE task_id = ? "
        "ORDER BY created_at ASC",
        (task_id,),
    )]
    # `summary` is where a blocked run states its reason; `error` is set when the
    # run died rather than decided. Both matter and they are not the same event.
    task["runs"] = [dict(r) for r in query_db(
        KANBAN_DB,
        "SELECT id, profile, status, outcome, summary, error, started_at, ended_at "
        "FROM task_runs WHERE task_id = ? ORDER BY id ASC",
        (task_id,),
    )]
    task["events"] = [dict(r) for r in query_db(
        KANBAN_DB,
        "SELECT kind, payload, created_at FROM task_events WHERE task_id = ? "
        "ORDER BY id ASC",
        (task_id,),
    )]
    # Prefer what the task itself recorded; fall back to the dispatcher join.
    task["session_id"] = task.get("session_id") or _kanban_session_id(task_id)
    return task


# Statuses a task may be archived from. Deliberately not "anything": archiving
# is a board gesture, not a kill switch, and setting a running task to
# `archived` from under a live worker would strand the run — the dispatcher
# would still hold the claim while the task had left the board. `done` is the
# case the button exists for; `blocked` is included because a task blocked on
# something that no longer matters is exactly the other thing people want off
# the board, and it has no worker attached.
ARCHIVABLE_STATUSES = {"done", "blocked"}


def _kanban_set_status(task_id: str, new_status: str, allowed_from: set) -> dict:
    """Move one task between statuses, refusing transitions the board shouldn't make.

    Reads the current row first rather than relying on `execute_db`, which
    returns only a bool: without the read, a 404 (no such task) and a refused
    transition and a successful write are indistinguishable to the caller.
    """
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", task_id or ""):
        raise HTTPException(status_code=400, detail="Malformed task id")

    rows = query_db(KANBAN_DB, "SELECT id, status FROM tasks WHERE id = ?", (task_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Task not found")

    current = str(dict(rows[0]).get("status") or "").lower()
    if current == new_status:
        # Idempotent: two clicks on a slow connection is not an error.
        return {"status": "success", "id": task_id, "task_status": new_status, "changed": False}
    if current not in allowed_from:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot move a task from '{current}' to '{new_status}'. "
                   f"Allowed from: {', '.join(sorted(allowed_from))}.",
        )

    ok = execute_db(KANBAN_DB, "UPDATE tasks SET status = ? WHERE id = ?", (new_status, task_id))
    if not ok:
        raise HTTPException(status_code=500, detail=f"Failed to set task status to '{new_status}'")
    return {"status": "success", "id": task_id, "task_status": new_status, "changed": True}


@app.post("/api/kanban/{task_id}/archive")
def archive_kanban_task(task_id: str):
    """Take a finished task off the board.

    `archived` is the kernel's own terminal status, not a dashboard invention:
    `hermes kanban` list queries already exclude it by default, and the
    workspace reaper collects archived tasks' scratch dirs. Writing it here
    means the board and the CLI agree about what is still live.
    """
    return _kanban_set_status(task_id, "archived", ARCHIVABLE_STATUSES)


@app.post("/api/kanban/{task_id}/unarchive")
def unarchive_kanban_task(task_id: str):
    """Put an archived task back, for the click that shouldn't have happened.

    Restores to `done` rather than to whatever it was before: the previous
    status isn't recorded anywhere, and guessing `todo` would put a finished
    task back in the queue where a dispatcher could pick it up again.
    """
    return _kanban_set_status(task_id, "done", {"archived"})


def _read_jobs_file(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error reading cron jobs file at {path}: {e}")
        return []
    if isinstance(data, dict):
        return data.get("jobs", []) or []
    return data or [] if isinstance(data, list) else []


def _enriched_cron_jobs():
    """Every scheduled job on this host, tagged with who runs it and on what.

    Jobs are per-profile — each agent home owns a `cron/jobs.json` — so the
    owning profile IS the assignment; there is no separate field for it.

    Model needs resolving rather than reading. A job's `model` is almost always
    null, which does not mean "no model": it means inherit whatever the owning
    profile is configured with at fire time. And a `no_agent` job runs a script
    with no LLM in the loop at all, where naming a model would be a fiction.
    Those three cases are distinguished by `model_source`, so the UI can label
    an inherited model as inherited instead of implying the job pinned it.

    Factored out of the route so `/api/automations/{id}` serves the identical
    job shape rather than a second, drifting description of the same job. The
    detail page shows a job's configuration next to its run history, and a
    field that means one thing in the list and another on the detail page is
    the specific bug that split view was built to end.
    """
    profiles = {}
    for name in agent_names():
        home = agent_home(name)
        if not home:
            continue
        summary = agent_summary(name) or {}
        profiles[name] = {
            "file": os.path.join(home, "cron", "jobs.json"),
            "model": summary.get("model") or "",
            "provider": summary.get("provider") or "",
        }
    # Keep the default profile's canonical path even if it were relocated.
    if DEFAULT_AGENT in profiles:
        profiles[DEFAULT_AGENT]["file"] = CRON_JOBS_FILE

    out = []
    for name, info in profiles.items():
        if not os.path.exists(info["file"]):
            continue
        for job in _read_jobs_file(info["file"]):
            if not isinstance(job, dict):
                continue
            override = job.get("model") or job.get("model_snapshot")
            runs_agent = not job.get("no_agent")
            if not runs_agent:
                model, source = None, "none"
            elif override:
                model, source = override, "override"
            else:
                model, source = info["model"], "inherited"
            # Which ADK app this launches, recovered from the wrapper script —
            # the job itself carries no such field. Null for the many jobs that
            # are not ADK jobs at all.
            adk_app, script_path, records_runs = adk_cron_link.resolve_app(
                job.get("script") or "")
            out.append({
                **job,
                "agent": name,
                "agent_is_default": name == DEFAULT_AGENT,
                "agent_model": info["model"],
                "agent_provider": info["provider"],
                "effective_model": model,
                "effective_provider": (
                    job.get("provider") or job.get("provider_snapshot") or info["provider"]
                ) if runs_agent else None,
                "model_source": source,
                "runs_agent": runs_agent,
                "adk_app": adk_app,
                "adk_app_source": "script" if adk_app else None,
                "script_path": script_path,
                # Only meaningful for a job that launches an ADK app: whether
                # its runs reach the scorecard at all. See adk_cron_link.
                "records_runs": records_runs if adk_app else None,
            })
    return out


@app.get("/api/cron/jobs")
def get_cron_jobs():
    """Every scheduled job on this host. See `_enriched_cron_jobs`."""
    return _enriched_cron_jobs()


# --- first-run setup -------------------------------------------------------
# What a fresh install still needs, computed from what this process can already
# see. Read-only by design, and that is not a limitation to be fixed later:
# this console has no authentication, so anything on the tailnet reaches it.
# A page that could write .env would let that same anything point the gateway
# at a model endpoint of its choosing; a page that could restart the stack
# would need the docker socket, which is root on the host. So this reports and
# instructs, and the operator runs the two commands it cannot safely run
# itself.
#
# The Anthropic key is reported from STEWARD_ANTHROPIC_KEY_SET, which compose
# fills with `${ANTHROPIC_API_KEY:+1}` — a boolean, never the key. The key is
# deliberately NOT in this container's environment: the console already exposes
# the gateway key to anyone who reaches it, and a gateway credential is
# rate-limited and revocable in a way a raw model key is not.
#
# It cannot be inferred instead. A gateway with no key answers /health and
# /v1/models with 200 and a plausible model list, and only fails on the first
# real completion — which is exactly the failure this page exists to pre-empt,
# and not something worth spending a token on every page load to detect.
def _setup_checklist() -> dict:
    items = []

    key_set = os.environ.get("STEWARD_ANTHROPIC_KEY_SET", "").strip() == "1"
    items.append({
        "id": "model_key",
        "title": "Anthropic API key",
        "status": "ok" if key_set else "blocked",
        "detail": (
            "Set. Steward can call a model."
            if key_set else
            "Not set. Every service is healthy and none of them can do any work: "
            "the gateway answers /health and lists models without one, and fails "
            "only on the first real completion."
        ),
        "fix": None if key_set else [
            "$EDITOR /srv/steward/stack/.env    # the ANTHROPIC_API_KEY= line",
            "docker compose -f /srv/steward/stack/steward-stack.yml \\",
            "  --env-file /srv/steward/stack/.env up -d",
        ],
        "why": None if key_set else (
            "The restart is not optional. Services read the key from their "
            "environment when they start, so editing .env alone changes nothing "
            "that is already running."
        ),
    })

    try:
        jobs = _enriched_cron_jobs() or []
    except Exception:
        jobs = []
    items.append({
        "id": "automations",
        "title": "Scheduled automations",
        "status": "ok" if jobs else "todo",
        "detail": (
            f"{len(jobs)} scheduled." if jobs else
            "None yet, which is correct for a fresh install — the schedule is "
            "yours to create, not something the install ships."
        ),
        "fix": None,
        "why": None,
    })

    return {"items": items, "configured": all(i["status"] != "blocked" for i in items)}


@app.get("/api/setup/state")
async def get_setup_state():
    """The first-run checklist, plus live service health."""
    try:
        health = await health_services()
    except Exception:
        health = None
    out = _setup_checklist()
    out["health"] = health
    return out


@app.get("/api/cron/watchdog")
def get_cron_watchdog():
    """Which scheduled jobs have stopped succeeding.

    Read-only: this reports what the background watcher would act on without
    filing anything, so the page can show the same judgement the board is
    getting. See backend/cron_watchdog.py for why the check lives here rather
    than in a cron job.
    """
    stale = cron_watchdog.scan(DB_DIR)
    return {
        # _utc_now() rather than .isoformat() so every timestamp this backend
        # emits uses the same "…Z" spelling the frontend's parser expects.
        "checked_at": _utc_now(),
        "enabled": cron_watchdog.ENABLED,
        "interval_seconds": cron_watchdog.CHECK_INTERVAL_SECONDS,
        "missed_runs_before_alert": cron_watchdog.MISSED_RUNS_BEFORE_ALERT,
        "stale": [
            {
                "profile": j.profile,
                "job_id": j.job_id,
                "name": j.name,
                "period_seconds": j.period_seconds,
                "threshold_seconds": j.threshold_seconds,
                "stale_for_seconds": j.stale_for_seconds,
                "last_success_at": j.last_success_at,
                "last_error": j.last_error,
                "consecutive_failures": j.consecutive_failures,
            }
            for j in stale
        ],
    }


@app.get("/api/cron/health-digest")
def get_automation_health_digest():
    """How well the automations work, and how well fixing them works.

    The watchdog next door answers "is anything broken now". This answers "is
    this getting better" — failure rates per job, which jobs have never once
    succeeded, and whether the cards the watchers file actually reach done.

    Read-only by design: the watchdog owns card-filing, and a second writer on
    the same idempotency key is how a closed task silences the next outbreak.
    """
    return automation_health.digest(DB_DIR, KANBAN_DB)


@app.on_event("startup")
async def _start_cron_watchdog():
    """Launch the staleness watcher alongside the API.

    In-process on purpose. The whole point is that the observer must not share
    a failure domain with the cron system it observes; running it here buys
    that for free, and costs one sleeping task.
    """
    if not cron_watchdog.ENABLED:
        print("[cron-watchdog] disabled by CRON_WATCHDOG_ENABLED", flush=True)
        return
    import asyncio

    asyncio.create_task(cron_watchdog.watchdog_loop(DB_DIR, KANBAN_DB))
    print(
        f"[cron-watchdog] watching {DB_DIR} every "
        f"{cron_watchdog.CHECK_INTERVAL_SECONDS}s",
        flush=True,
    )


# How far an ADK trace's start may sit outside a cron execution's window and
# still be called the same run. The two clocks are the same host's, so the slack
# covers wrapper startup before the trace is opened and the write that lands
# after the process is reaped — not clock skew.
_ADK_MATCH_SLACK = datetime.timedelta(seconds=120)


def _adk_run_for(execution: dict, runs: list) -> Optional[dict]:
    """The ADK trace belonging to one cron execution, or None.

    Matched on time, because nothing links them by id: the cron store knows a
    process exited, and the trace knows a workflow ran, and neither writes the
    other's identifier. So this is evidence rather than proof, and it is
    reported as `adk_run` on the execution — the run detail is shown as what
    happened during that window, never as a joined key.

    Ambiguity is resolved by not resolving it: if two traces start inside one
    execution's window, the earliest is returned, and a wrapper that fires two
    workflows per execution is a shape this cannot describe.
    """
    start = execution.get("started_at") or execution.get("claimed_at")
    if not start:
        return None
    end = execution.get("finished_at")
    lo = start - _ADK_MATCH_SLACK
    hi = (end or start) + _ADK_MATCH_SLACK
    best = None
    for run in runs:
        started = adk_scorecard.parse_ts(run.get("started_at"))
        if not started:
            continue
        # The store hands back naive UTC timestamps; traces carry an offset.
        naive = started.replace(tzinfo=None) if started.tzinfo else started
        if lo <= naive <= hi and (best is None or naive < best[0]):
            best = (naive, run)
    return best[1] if best else None


def _health_card_for(job_id: str, profile: Optional[str]) -> Optional[dict]:
    """The still-open watchdog card for this job, or None.

    A failing automation is not, by itself, something to read a stack trace
    about: `cron_watchdog` files a card for it and an agent works the card. So
    the detail page can point at the repair in progress instead of reprinting
    the traceback the executions below already carry.

    Matched on `idempotency_key`, which the watchdog builds as
    `cron-health:<profile>:<job_id>:<filed_at>` — an exact join, unlike the
    title, which is editable on the board. Only open cards count: a closed one
    means that outbreak was dealt with, and the next failure files a new card.
    """
    conn = automation_health._open_kanban(KANBAN_DB)
    if conn is None:
        return None
    try:
        prefix = f"cron-health:{profile}:{job_id}:" if profile else None
        rows = conn.execute(
            "SELECT id, title, status, assignee, created_at FROM tasks "
            "WHERE idempotency_key LIKE ? OR idempotency_key LIKE ? "
            "ORDER BY created_at DESC",
            (prefix or "\x00", f"cron-health:%:{job_id}:%"),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    for row in rows:
        if (row["status"] or "") in cron_watchdog.CLOSED_STATUSES:
            continue
        return {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "assignee": row["assignee"],
            "created_at": row["created_at"],
        }
    return None


@app.get("/api/automations/{job_id}")
def get_automation(job_id: str, days: int = Query(30), limit: int = Query(25)):
    """One automation, whole: what it is, what it is attached to, how it ran.

    This route exists because the answer was previously split across three
    pages that each held a third of it — the cron list knew the schedule, the
    scorecard knew the runs, and the profile page knew the configuration — and
    none of them could tell you why last night's execution failed.

    `executions` is the run history from the cron store, newest first, each
    carrying its own `error`. Where the job launches an ADK app, the matching
    trace is attached as `adk_run` so a failed execution can be read down to
    the stage that broke. See `_adk_run_for` for why that match is by time.

    A 404 here means no job carries this id in any profile's `jobs.json` — a
    deleted automation, or a mistyped link. The executions of a deleted job
    outlive it in the cron store, but they are not served without the job:
    a page of runs for something that no longer exists cannot say what it did.
    """
    days = max(1, min(days, 3650))
    limit = max(1, min(limit, 200))
    job = next((j for j in _enriched_cron_jobs() if j.get("id") == job_id), None)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No scheduled job with id {job_id}")

    executions = _metrics_call(metrics_store.automation_runs, job_id, days, limit)
    totals = _metrics_call(metrics_store.automation_totals, job_id, days)

    # ADK detail, only for a job that launches an app through the invoker. A
    # wrapper that bypasses invoke_workflow writes no trace, so there is
    # nothing to attach and `records_runs` already says so on the job.
    adk_app = job.get("adk_app")
    if adk_app:
        try:
            runs = adk_scorecard.load_runs(ADK_STATE_DIR, adk_app, days)
        except Exception as exc:
            # An unreadable trace directory costs the run detail, not the page:
            # the executions and their errors are the cron store's, and they
            # are the part that says whether the automation is working.
            print(f"automation {job_id}: ADK traces unavailable: {exc}")
            runs = []
        for execution in executions:
            execution["adk_run"] = _adk_run_for(execution, runs)

    return {
        "job": job,
        "days": days,
        "executions": executions,
        "totals": totals,
        "adk_app": adk_app,
        "health_task": _health_card_for(job_id, job.get("agent")),
    }


@app.post("/api/cron/jobs/{job_id}/run")
async def run_cron_job_now(job_id: str):
    """Arm a job to fire on the gateway's next scheduler tick.

    A relay, not an executor. The gateway owns the ticker and the agent, so
    this posts to its `/api/jobs/{id}/run` and nothing more — that sets
    `next_run_at` to now and the built-in ticker (~60s) picks the job up.
    It does NOT run inline, so the answer here is "queued", never a result;
    the outcome shows up in `last_status` on the next poll. Running inline is
    `hermes cron run` on the CLI, which needs the agent in-process.

    This is the one write on an otherwise read-only, unauthenticated port. It
    is deliberately narrow: it can only fire a job that already exists, and
    cannot create, edit, delete, or reschedule one.
    """
    # Job IDs are 12-char hex by construction. Validate before interpolating
    # into a URL so a crafted id cannot reach a different gateway route.
    if not re.fullmatch(r"[0-9a-f]{6,32}", job_id or ""):
        raise HTTPException(status_code=400, detail="Invalid job id")

    job = next((j for j in get_cron_jobs() if j.get("id") == job_id), None)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    # The API server speaks for the gateway's own home only. A job owned by
    # another profile is not addressable there, and firing blind risks hitting
    # a same-id job in the wrong home — refuse instead of guessing.
    if not job.get("agent_is_default"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{job.get('name') or job_id}' belongs to the {job.get('agent')} profile. "
                "Only the default profile's jobs can be triggered from here — run it with "
                f"`hermes cron run {job.get('name') or job_id}` for that profile."
            ),
        )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{HERMES_API_BASE}/api/jobs/{job_id}/run",
                headers={"Authorization": f"Bearer {API_SERVER_KEY}"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Could not reach the Hermes gateway at {HERMES_API_BASE}: {exc}",
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Gateway refused the trigger: {resp.text}",
        )
    return {"queued": True, "job": (resp.json() or {}).get("job", {})}

# --- Outbound Approval Queue Endpoints ---

def _with_producer_access(item: dict) -> dict:
    """Attach what the agent that produced this item could reach.

    Put in front of the reviewer at the moment they decide whether to trust the
    output: the Integrations tab answers "what can reach what", and this answers
    "what produced this, and with what access". An item written before
    app/approvals.py started recording a producer has none, and says so rather
    than borrowing `evidence.source` — that field is free text already used for
    other things, so guessing from it would put a confident wrong agent name
    next to a decision about sending mail.
    """
    producer = item.get("producer")
    if not isinstance(producer, dict) or not producer.get("agent"):
        item["producer_access"] = None
        return item
    consumer = producer.get("consumer") or producer.get("agent")
    try:
        payload = integrations_payload(max_age=_INTEGRATIONS_TTL)
        item["producer_access"] = integrations.grants_for_consumer(payload, consumer)
    except Exception as exc:
        print(f"Could not resolve producer access for {item.get('id')}: {exc}")
        item["producer_access"] = None
    return item


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_display_timezone() -> dict:
    """The user's configured display timezone, for the frontend to render in.

    Every timestamp this backend emits is UTC; the browser used to render them
    in whatever zone the machine happened to be in, with no label, so a correct
    timestamp and a wrong one looked identical. Handing the frontend the
    configured zone lets it render one consistent clock and name it.

    Resolution mirrors hermes_time upstream: HERMES_TIMEZONE beats the
    `timezone` key in config.yaml. Read-only — this service never writes config.

    Cron is deliberately NOT on this clock (it runs on UTC), which is why the
    payload reports the scheduler zone separately.
    """
    name = os.getenv("HERMES_TIMEZONE", "").strip()
    if not name:
        try:
            with open(HERMES_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            value = cfg.get("timezone")
            if isinstance(value, str):
                name = value.strip()
        except (OSError, yaml.YAMLError):
            name = ""
    return {"timezone": name or "UTC", "scheduler_timezone": "UTC"}


@app.get("/api/timezone")
def get_display_timezone():
    return resolve_display_timezone()


def normalize_item(item: dict, state: str) -> dict:
    """The single shape every endpoint returns.

    Two jobs. First, give the item a `review_type` — derived from the legacy
    `channel` for anything written before the field existed, so none of the
    items already sitting in the queue have to be rewritten to be understood.

    Second, flatten the ragged shapes that are genuinely on disk. Real items
    exist with `evidence: null`, with no `subject` key at all, and with no
    `recipient`. Absorbing that here rather than at three call sites in the
    frontend is what lets the templates be written as though the data were
    regular, and is why an unrecognised type still renders instead of throwing.
    """
    out = dict(item)
    review_type = review_type_of(out)
    out["review_type"] = review_type
    out["review_type_label"] = REVIEW_TYPE_LABELS.get(review_type, review_type)
    out["state"] = state

    if not isinstance(out.get("evidence"), dict):
        out["evidence"] = {}
    if not isinstance(out["evidence"].get("scores"), dict):
        out["evidence"]["scores"] = {}
    if not isinstance(out.get("recipient"), dict):
        out["recipient"] = {"name": None, "address": None, "org": None}
    out.setdefault("subject", None)
    out.setdefault("body", None)
    out.setdefault("reason", None)

    # The generic contract. A type with no hand-written template renders from
    # these three, so `title`/`summary`/`fields` are what a new producer needs
    # to fill in to get a usable page with no frontend change at all.
    if not out.get("title"):
        out["title"] = out.get("subject") or out.get("reason") or f"Review item {out.get('id')}"
    if not out.get("summary"):
        out["summary"] = out.get("reason")
    if not isinstance(out.get("fields"), list):
        out["fields"] = []

    out["actions"] = actions_for(review_type, suggested_action=out.get("suggested_action"))
    out["reject_action"] = reject_action_for(review_type)
    return _with_producer_access(out)


def _read_item(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _items_in(state: str, directory: str):
    items = []
    if not os.path.isdir(directory):
        return items
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".json") or filename.startswith("."):
            continue
        path = os.path.join(directory, filename)
        try:
            items.append(normalize_item(_read_item(path), state))
        except Exception as ex:
            print(f"Skipping malformed review file {path}: {ex}")
    return items


def _find_item_path(item_id: str):
    """Locate an item by id across every state. Returns (state, path) or None.

    The `--` in `{created_at}--{id}.json` is load-bearing: it is what makes this
    an unambiguous suffix match rather than a prefix collision between ids.
    """
    if not item_id or "/" in item_id or "\\" in item_id or item_id.startswith("."):
        return None
    for state, directory in REVIEW_STATE_DIRS:
        if not os.path.isdir(directory):
            continue
        for filename in os.listdir(directory):
            if filename.endswith(f"--{item_id}.json"):
                return state, os.path.join(directory, filename)
    return None


def _is_stalled(item: dict) -> bool:
    started = ((item.get("execution") or {}).get("started_at")) or ""
    try:
        when = datetime.datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return False
    age = (datetime.datetime.now(datetime.timezone.utc) - when).total_seconds()
    return age > EXECUTION_LEASE_SECONDS


def _write_item(item: dict, dest_dir: str, filename: str):
    """Write an item into a state directory, atomically and readably.

    mkstemp creates 0600 and os.replace preserves it, so the chmod is not
    optional — the executor runs as a different uid and a 0600 decision is one
    it cannot act on. This mirrors write_pending() on the producer side, which
    exists because exactly this bug bit in the other direction.
    """
    os.makedirs(dest_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=dest_dir, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(item, f, indent=2)
        os.chmod(temp_path, ITEM_MODE)
        os.replace(temp_path, os.path.join(dest_dir, filename))
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def get_review_queue():
    """The queue, split into what needs deciding and what needs rescuing.

    `attention` is failed and stalled executions. It is separate from `pending`
    because it is a different question being asked of the reviewer — not "should
    this go out" but "this was supposed to have gone out and did not" — and it
    outranks everything else on the page.
    """
    ensure_approval_dirs()
    try:
        pending = _items_in("pending", APPROVAL_PENDING_DIR)
        # Kanban tasks blocked on a human are the same question in a different
        # store, so they join the same list rather than getting a queue of their
        # own. They go through normalize_item too: the type table, the action
        # list and the producer panel are the point of this queue, and an item
        # that skipped them would be a second contract nobody is maintaining.
        #
        # Failing soft is deliberate. kanban.db is a different file with a
        # different writer, and an unreadable board must not take the email
        # queue down with it.
        try:
            pending.extend(
                normalize_item(item, "pending")
                for item in kanban_review.pending_items(KANBAN_DB)
            )
        except Exception as ex:
            print(f"Could not load kanban review items: {ex}")
        pending.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        attention = _items_in("failed", APPROVAL_FAILED_DIR)
        for item in _items_in("executing", APPROVAL_EXECUTING_DIR):
            if _is_stalled(item):
                item["state"] = "failed"
                item.setdefault("execution", {})
                item["execution"]["state"] = "failed"
                item["execution"]["error"] = {
                    "kind": "stalled",
                    "message": (
                        "Claimed by the executor but never finished. Not retried "
                        "automatically: a stall around a send cannot be told apart "
                        "from a send that succeeded."
                    ),
                    "retryable": False,
                }
                attention.append(item)
        attention.sort(key=lambda x: x.get("decided_at", ""), reverse=True)

        return {"pending": pending, "attention": attention}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load review queue: {e}")


def get_review_item(item_id: str):
    """One item, in whatever state it is now.

    This exists for the cold load after a decision. The URL still names the item
    you just approved; pressing reload must not answer "not found", because the
    item did not go missing — it moved on, and after a failed send this URL is
    the only handle anyone has on the thing that broke.
    """
    ensure_approval_dirs()
    task_item = kanban_review.get_item(KANBAN_DB, item_id)
    if task_item is not None:
        return normalize_item(task_item, task_item.get("state", "pending"))
    found = _find_item_path(item_id)
    if not found:
        raise HTTPException(status_code=404, detail=f"No review item '{item_id}'.")
    state, path = found
    item = normalize_item(_read_item(path), state)
    if state == "executing" and _is_stalled(item):
        item["state"] = "failed"
    return item


def get_review_health():
    try:
        if os.path.exists(APPROVAL_HEALTH_FILE):
            with open(APPROVAL_HEALTH_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}
    except Exception as e:
        print(f"Error reading health.json: {e}")
        return {}


def _decide_kanban_review(req: ApprovalDecisionRequest):
    """The kanban half of `process_review_decision`.

    Same validation order as the file-backed path — the action is checked
    against the type table before anything is written, so a rejected request
    leaves the task exactly as blocked as it was and can be decided again.

    Reject *is* request-changes here. REJECT_ACTION already requires a reason,
    and that reason is the only thing that makes returning the task to the board
    useful: the worker picks it back up and reads the comment. A rejection that
    just parked the task would recreate the dead end this whole path exists to
    remove.
    """
    if req.decision == "reject":
        if not req.rejection_reason or not req.rejection_reason.strip():
            raise HTTPException(status_code=400, detail="Rejection reason is required.")
        action_id = REJECT_ACTION["id"]
    else:
        action = find_action(kanban_review.REVIEW_TYPE, req.action) if req.action else None
        if req.action and action is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Action '{req.action}' is not valid for review type "
                    f"'{kanban_review.REVIEW_TYPE}'. Legal actions: "
                    f"{', '.join(legal_action_ids(kanban_review.REVIEW_TYPE))}."
                ),
            )
        action_id = (action or {}).get("id") or "approve_done"

    try:
        if action_id == REJECT_ACTION["id"]:
            outcome = kanban_review.request_changes(KANBAN_DB, req.id, req.rejection_reason)
        else:
            outcome = kanban_review.approve(KANBAN_DB, req.id, req.edited_body)
    except kanban_review.DecisionConflict as ex:
        # 409, not 404: the task is still there, it just is not in the state
        # this decision assumed. Usually two tabs, or the dispatcher having
        # already moved it.
        return JSONResponse(
            status_code=409,
            content={"status": "error", "code": "conflict", "message": str(ex)},
        )
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex))
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"Could not decide task '{req.id}': {ex}")

    return {
        "status": "success",
        "id": req.id,
        "decision": req.decision,
        "action": action_id,
        "execution": "not_applicable",
        "task_status": outcome["status"],
    }


def process_review_decision(req: ApprovalDecisionRequest):
    """Record a human decision and, if it has an outcome, queue it.

    Everything is validated before anything moves. The order matters: an item
    that fails validation must still be in pending/ afterwards, so it can be
    decided again once whatever was wrong is fixed.
    """
    ensure_approval_dirs()

    if kanban_review.is_kanban_item(KANBAN_DB, req.id):
        return _decide_kanban_review(req)

    matches = [
        os.path.join(APPROVAL_PENDING_DIR, f)
        for f in os.listdir(APPROVAL_PENDING_DIR)
        if f.endswith(f"--{req.id}.json")
    ]
    if not matches:
        # Two tabs, one item. Distinguished from a 404 because the item probably
        # does still exist — somewhere further along.
        return JSONResponse(
            status_code=409,
            content={
                "status": "error",
                "code": "conflict",
                "message": (
                    f"Review item '{req.id}' is no longer pending. It may have "
                    "already been decided in another window."
                ),
            },
        )

    source_path = matches[0]
    item = _read_item(source_path)
    review_type = review_type_of(item)

    if req.decision == "reject":
        if not req.rejection_reason or not req.rejection_reason.strip():
            raise HTTPException(status_code=400, detail="Rejection reason is required.")
        action = dict(REJECT_ACTION)
    else:
        action_id = req.action
        if not action_id:
            # Legacy callers (and the old /api/approvals/decision contract) say
            # "approve" and mean the safe default for the type.
            primary = [a for a in actions_for(review_type) if a["primary"]]
            if not primary:
                raise HTTPException(
                    status_code=422,
                    detail=f"No approve action is available for review type '{review_type}'.",
                )
            action = primary[0]
        else:
            action = find_action(review_type, action_id)
            if action is None:
                # The check that makes the server-side action table real: a
                # filter cannot be sent, whatever the client asks for.
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Action '{action_id}' is not valid for review type "
                        f"'{review_type}'. Legal actions: {', '.join(legal_action_ids(review_type))}."
                    ),
                )
        if not action.get("available", True):
            return JSONResponse(
                status_code=409,
                content={
                    "status": "error",
                    "code": "capability_missing",
                    "message": action.get("unavailable_reason")
                    or "This action is not available on this host.",
                },
            )

    item["decision"] = req.decision
    item["action"] = action["id"]
    item["decided_at"] = _utc_now()
    item["decided_by"] = "dashboard"

    if req.decision == "reject":
        item["rejection_reason"] = req.rejection_reason
        item["execution"] = {"state": "not_applicable"}
        dest_dir = APPROVAL_REJECTED_DIR
    else:
        if req.edited_body is not None and req.edited_body.strip() != (item.get("body") or "").strip():
            item["edited_body"] = req.edited_body
        if action.get("executor"):
            item["execution"] = {
                "state": "queued",
                "executor": action["executor"],
                "attempts": 0,
            }
        else:
            item["execution"] = {"state": "not_applicable"}
        dest_dir = APPROVAL_APPROVED_DIR

    _write_item(item, dest_dir, os.path.basename(source_path))
    os.unlink(source_path)

    return {
        "status": "success",
        "id": req.id,
        "decision": req.decision,
        "action": action["id"],
        "execution": item["execution"]["state"],
    }


def retry_review_item(req: ReviewItemActionRequest):
    """Send a failed execution back to the executor.

    Moves failed/ -> approved/, which the dashboard may do and the executor may
    not: it holds approved/ read-only, so a retry is a human act by construction.
    """
    ensure_approval_dirs()
    found = _find_item_path(req.id)
    if not found or found[0] not in ("failed", "executing"):
        raise HTTPException(status_code=404, detail=f"No failed review item '{req.id}'.")
    state, path = found
    item = _read_item(path)

    execution = item.get("execution") or {}
    if execution.get("error", {}).get("retryable") is False:
        raise HTTPException(
            status_code=409,
            detail=(
                "This failure is not retryable. Re-running it could duplicate an "
                "action that may already have taken effect."
            ),
        )
    execution["state"] = "queued"
    execution["attempts"] = int(execution.get("attempts") or 0)
    execution.pop("error", None)
    item["execution"] = execution

    _write_item(item, APPROVAL_APPROVED_DIR, os.path.basename(path))
    os.unlink(path)
    return {"status": "success", "id": req.id, "execution": "queued"}


def dismiss_review_item(req: ReviewItemActionRequest):
    """Give up on a failed execution and stop it nagging."""
    ensure_approval_dirs()
    found = _find_item_path(req.id)
    if not found or found[0] not in ("failed", "executing"):
        raise HTTPException(status_code=404, detail=f"No failed review item '{req.id}'.")
    _state, path = found
    item = _read_item(path)
    execution = item.get("execution") or {}
    execution["state"] = "abandoned"
    execution["finished_at"] = _utc_now()
    item["execution"] = execution
    _write_item(item, APPROVAL_EXECUTED_DIR, os.path.basename(path))
    os.unlink(path)
    return {"status": "success", "id": req.id, "execution": "abandoned"}


# Registered under /api/review/* — and under the original /api/approvals/*
# names, against the same functions. The frontend is served with no-store
# precisely because stale bundles are a recurring problem here, and a bundle
# from before this change asking for a path that no longer exists would show an
# empty queue rather than an error.
app.add_api_route("/api/review/queue", get_review_queue, methods=["GET"])
app.add_api_route("/api/review/health", get_review_health, methods=["GET"])
app.add_api_route("/api/review/item/{item_id}", get_review_item, methods=["GET"])
app.add_api_route(
    "/api/review/decision",
    process_review_decision,
    methods=["POST"],
    dependencies=[Depends(require_same_origin)],
)
app.add_api_route(
    "/api/review/retry",
    retry_review_item,
    methods=["POST"],
    dependencies=[Depends(require_same_origin)],
)
app.add_api_route(
    "/api/review/dismiss",
    dismiss_review_item,
    methods=["POST"],
    dependencies=[Depends(require_same_origin)],
)


@app.get("/api/approvals/queue")
def get_approvals_queue_legacy():
    """Deprecated alias. Returns the bare pending array the old bundle expects."""
    return get_review_queue()["pending"]


app.add_api_route("/api/approvals/health", get_review_health, methods=["GET"])
app.add_api_route(
    "/api/approvals/decision",
    process_review_decision,
    methods=["POST"],
    dependencies=[Depends(require_same_origin)],
)

# --- Skills Endpoints (read-only view of the Hermes skills tree) ---

def get_disabled_skill_names(config_file: str = None):
    """Read skills.disabled from a Hermes config.

    Mirrors agent/skill_utils.py:get_disabled_skill_names — the per-platform
    list adds to the global list, it never replaces it. Names may refer to
    either a skill's frontmatter name or its directory name.

    ``config_file`` selects which agent's config to read; defaults to the
    default agent's.
    """
    config_file = config_file or HERMES_CONFIG_FILE
    try:
        if not os.path.exists(config_file):
            return set()
        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        skills_cfg = config.get("skills") or {}

        disabled = set()
        for name in (skills_cfg.get("disabled") or []):
            if isinstance(name, str):
                disabled.add(name)

        platform_disabled = skills_cfg.get("platform_disabled") or {}
        if isinstance(platform_disabled, dict):
            for name in (platform_disabled.get("linux") or []):
                if isinstance(name, str):
                    disabled.add(name)

        return disabled
    except Exception as e:
        print(f"Error reading disabled skills from {HERMES_CONFIG_FILE}: {e}")
        return set()


def parse_skill_frontmatter(skill_file: str):
    """Parse the YAML frontmatter out of a SKILL.md. Returns {} if absent."""
    with open(skill_file, 'r', encoding='utf-8') as f:
        content = f.read()

    if not content.startswith("---"):
        return {}

    # Split off the frontmatter block delimited by the first closing ---
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}

    data = yaml.safe_load(parts[1])
    return data if isinstance(data, dict) else {}


def list_skills_for(skills_dir: str, config_file: str):
    """List every skill in one agent's skills tree.

    Split out from the /api/skills route so each agent's tree can be listed
    with that agent's own disabled-skills config.
    """
    try:
        if not os.path.exists(skills_dir):
            return []

        disabled = get_disabled_skill_names(config_file)
        skills = []

        for dirpath, dirnames, filenames in os.walk(skills_dir):
            # Skip bookkeeping dirs: .hub, .archive, .curator_backups
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]

            if "SKILL.md" not in filenames:
                continue

            skill_file = os.path.join(dirpath, "SKILL.md")
            rel_path = os.path.relpath(dirpath, skills_dir)
            skill_name = os.path.basename(dirpath)
            parts = rel_path.split(os.sep)
            # Nested skills are grouped by their top-level category directory;
            # skills sitting directly in the tree root have no category.
            category = parts[0] if len(parts) > 1 else "general"

            try:
                fm = parse_skill_frontmatter(skill_file)
            except Exception as ex:
                print(f"Skipping malformed skill file {skill_file}: {ex}")
                continue

            name = fm.get("name") or skill_name
            hermes_meta = ((fm.get("metadata") or {}).get("hermes") or {})
            tags = hermes_meta.get("tags") or fm.get("tags") or []
            platforms = fm.get("platforms") or []
            if not isinstance(platforms, list):
                platforms = []

            # A skill is disabled if either its frontmatter name or its
            # directory name appears in the disabled list.
            enabled = name not in disabled and skill_name not in disabled
            # Hermes drops platform-incompatible skills from the prompt at
            # render time, so a macOS-only skill is inert on this host even
            # though nothing disabled it.
            platform_ok = not platforms or HOST_PLATFORM in platforms

            skills.append({
                "name": name,
                "skill_name": skill_name,
                "category": hermes_meta.get("category") or category,
                "description": fm.get("description") or "",
                "version": fm.get("version") or "",
                "author": fm.get("author") or "",
                "platforms": platforms,
                "tags": tags if isinstance(tags, list) else [],
                "enabled": enabled,
                "platform_ok": platform_ok,
                # What the agent can actually reach on this host
                "available": enabled and platform_ok,
                "rel_path": rel_path,
            })

        skills.sort(key=lambda s: (s["category"], s["name"]))
        return skills
    except Exception as e:
        print(f"Error listing skills from {skills_dir}: {e}")
        return []


@app.get("/api/skills")
def get_skills():
    """List the default agent's skills (kept for the legacy Skills view)."""
    return list_skills_for(SKILLS_DIR, HERMES_CONFIG_FILE)


@app.get("/api/skills/content")
def get_skill_content(rel_path: str = Query(...)):
    """Return the raw SKILL.md markdown for a single skill."""
    skills_root = os.path.realpath(SKILLS_DIR)
    target = os.path.realpath(os.path.join(skills_root, rel_path, "SKILL.md"))

    # Confine reads to the skills tree so rel_path cannot escape it
    if not target.startswith(skills_root + os.sep):
        raise HTTPException(status_code=400, detail="Invalid skill path.")

    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail="Skill not found.")

    try:
        # Full file, verbatim — the frontend splits frontmatter from body
        with open(target, 'r', encoding='utf-8') as f:
            return {"content": f.read()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read skill: {e}")


# --- Context Files Endpoints (soul + any other markdown under ~/.hermes) ---

@app.get("/api/context/files")
def get_context_files():
    """List the markdown files that make up the agent's context.

    SOUL.md is the agent identity (prompt slot #1). Everything else under
    ~/.hermes is surfaced too, minus the skills tree (its own tab), the
    vendored hermes-agent source, and cache directories.
    """
    return list_context_files_for(DB_DIR)


def list_context_files_for(home: str):
    """List the markdown context files inside one agent's home."""
    soul = os.path.join(home, "SOUL.md")
    try:
        if not os.path.exists(home):
            return []

        files = []
        for dirpath, dirnames, filenames in os.walk(home):
            dirnames[:] = [
                d for d in dirnames
                if d not in CONTEXT_SKIP_DIRS and not d.startswith(".")
            ]

            for filename in filenames:
                if not filename.lower().endswith(".md"):
                    continue

                full = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(full, home)
                try:
                    stat = os.stat(full)
                except Exception as ex:
                    print(f"Skipping unreadable context file {full}: {ex}")
                    continue

                group = os.path.dirname(rel_path) or "root"
                files.append({
                    "name": filename,
                    "rel_path": rel_path,
                    "group": group,
                    "size": stat.st_size,
                    # An offset-bearing instant, not a pre-rendered naive
                    # string. It used to emit "%Y-%m-%d %H:%M" with no zone
                    # marker at all — UTC, but saying so nowhere, so the
                    # frontend could not render it in the user's zone and the
                    # user could not tell which clock they were reading.
                    # (utcfromtimestamp is also deprecated as of 3.12.)
                    "modified": datetime.datetime.fromtimestamp(
                        stat.st_mtime, datetime.timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "is_soul": os.path.realpath(full) == os.path.realpath(soul),
                })

        # Soul first, then alphabetically by location
        files.sort(key=lambda f: (not f["is_soul"], f["group"], f["name"]))
        return files
    except Exception as e:
        print(f"Error listing context files from {home}: {e}")
        return []


@app.get("/api/context/content")
def get_context_content(rel_path: str = Query(...)):
    """Return the raw text of one context markdown file."""
    root = os.path.realpath(DB_DIR)
    target = os.path.realpath(os.path.join(root, rel_path))

    # Confine reads to the hermes home, and to markdown only
    if not target.startswith(root + os.sep):
        raise HTTPException(status_code=400, detail="Invalid file path.")
    if not target.lower().endswith(".md"):
        raise HTTPException(status_code=400, detail="Only markdown files can be read.")

    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail="File not found.")

    try:
        with open(target, 'r', encoding='utf-8') as f:
            return {"content": f.read()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {e}")


# --- Agent (profile) Endpoints ---
#
# Each Hermes profile is a self-contained agent: its own config.yaml (model,
# provider, toolsets), SOUL.md, skills/, memories/ and cron/. The default agent
# is HERMES_HOME itself; the rest live under HERMES_HOME/profiles/<name>.

def agent_home(name: str):
    """Resolve an agent name to its home dir, or None if it isn't one.

    Rejects path separators outright so a name can never traverse out of the
    profiles dir.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    if name == DEFAULT_AGENT:
        return DB_DIR
    home = os.path.join(PROFILES_DIR, name)
    if os.path.isdir(home):
        # Belt and braces: the resolved path must still sit inside profiles/
        if os.path.realpath(home).startswith(os.path.realpath(PROFILES_DIR) + os.sep):
            return home
    return None


def agent_names():
    """Every agent on this host: the default, then named profiles A-Z."""
    names = [DEFAULT_AGENT]
    try:
        if os.path.isdir(PROFILES_DIR):
            for entry in sorted(os.listdir(PROFILES_DIR)):
                path = os.path.join(PROFILES_DIR, entry)
                # A profile is a home if it carries a config; skip stray files
                if os.path.isdir(path) and os.path.exists(
                    os.path.join(path, "config.yaml")
                ):
                    names.append(entry)
    except Exception as e:
        print(f"Error listing profiles in {PROFILES_DIR}: {e}")
    return names


def mcp_servers_for(cfg: dict):
    """The MCP servers a profile can reach, described without leaking secrets.

    `env` and `headers` on an MCP server routinely hold credentials — the gmail
    server carries OAuth paths, an HTTP server can carry a bearer token. Only
    the KEY NAMES are returned, never the values, matching the rule the ADK
    parser follows. This endpoint is served on an unauthenticated LAN-reachable
    port, so that is not a stylistic choice.
    """
    servers = cfg.get("mcp_servers")
    if not isinstance(servers, dict):
        return []
    out = []
    for sname, sc in sorted(servers.items()):
        if not isinstance(sc, dict):
            out.append({"name": sname, "transport": "unknown", "enabled": True,
                        "target": None, "env_keys": [], "header_keys": []})
            continue
        url = sc.get("url")
        command = sc.get("command")
        out.append({
            "name": sname,
            # stdio spawns a local process; http/sse talks to a URL. Which one
            # it is changes what "this agent has access to gmail" means.
            "transport": "http" if url else ("stdio" if command else "unknown"),
            # Shown verbatim, placeholders and all: ${HERMES_HOME} resolves
            # inside the gateway container but not for a host-side CLI, and
            # silently expanding it here would hide that discrepancy.
            "target": url or " ".join([str(command)] + [str(a) for a in (sc.get("args") or [])]),
            "enabled": sc.get("enabled", True) is not False,
            # How the server authenticates, verbatim from config ("oauth", or
            # absent). Only an `auth: oauth` server has a token in mcp-tokens/,
            # so without this the Integrations tab cannot tell "never logged in"
            # apart from "needs no login at all" — and would flag both.
            "auth": sc.get("auth") or None,
            "env_keys": sorted(sc["env"]) if isinstance(sc.get("env"), dict) else [],
            "header_keys": sorted(sc["headers"]) if isinstance(sc.get("headers"), dict) else [],
        })
    return out


def profile_meta_for(home: str):
    """Read ``<home>/profile.yaml`` — the profile's role description.

    Hermes keeps this deliberately apart from ``config.yaml``: the config is
    thousands of lines of settings, while this is metadata *about* the profile.
    It is what `hermes profile describe` writes, and what the kanban decomposer
    routes on — so a profile whose description is empty is one the decomposer
    can only distinguish by name.

    Missing or unreadable file means no description, never an error: a corrupt
    profile.yaml on one profile must not blank the whole agents page.
    """
    out = {"description": "", "description_auto": False}
    path = os.path.join(home, "profile.yaml")
    if not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            out["description"] = str(data.get("description") or "").strip()
            out["description_auto"] = bool(data.get("description_auto"))
    except Exception as e:
        print(f"Error reading profile.yaml in {home}: {e}")
    return out


def agent_summary(name: str):
    """Model, provider, toolsets and inventory counts for one agent."""
    home = agent_home(name)
    if home is None:
        return None
    meta = profile_meta_for(home)

    config_file = os.path.join(home, "config.yaml")
    model = provider = ""
    toolsets, disabled_toolsets = [], []
    mcp_servers = []
    try:
        if os.path.exists(config_file):
            with open(config_file, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            model_cfg = cfg.get("model") or {}
            model = model_cfg.get("default") or ""
            provider = model_cfg.get("provider") or ""
            toolsets = cfg.get("toolsets") or []
            disabled_toolsets = (cfg.get("agent") or {}).get("disabled_toolsets") or []
            mcp_servers = mcp_servers_for(cfg)
    except Exception as e:
        print(f"Error reading config for agent {name}: {e}")

    skills = list_skills_for(os.path.join(home, "skills"), config_file)
    memories_dir = os.path.join(home, "memories")
    memory_files = []
    try:
        if os.path.isdir(memories_dir):
            memory_files = sorted(
                f for f in os.listdir(memories_dir) if f.lower().endswith(".md")
            )
    except Exception:
        pass

    # Cron jobs are per-agent too (each home has its own cron/jobs.json)
    cron_count = 0
    try:
        jobs_file = os.path.join(home, "cron", "jobs.json")
        if os.path.exists(jobs_file):
            with open(jobs_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            jobs = data.get("jobs", []) if isinstance(data, dict) else (data or [])
            cron_count = len(jobs)
    except Exception:
        pass

    return {
        "name": name,
        "is_default": name == DEFAULT_AGENT,
        "path": home,
        "model": model,
        "provider": provider,
        "toolsets": toolsets if isinstance(toolsets, list) else [],
        "disabled_toolsets": (
            disabled_toolsets if isinstance(disabled_toolsets, list) else []
        ),
        "mcp_servers": mcp_servers,
        "description": meta["description"],
        "description_auto": meta["description_auto"],
        "has_soul": os.path.exists(os.path.join(home, "SOUL.md")),
        "skills_total": len(skills),
        "skills_available": sum(1 for s in skills if s.get("available")),
        "memory_files": memory_files,
        "context_count": len(list_context_files_for(home)),
        "cron_count": cron_count,
    }


@app.get("/api/agents")
def get_agents():
    """List every agent with its model, provider and inventory counts."""
    return [s for s in (agent_summary(n) for n in agent_names()) if s]


@app.get("/api/agents/{name}/skills")
def get_agent_skills(name: str):
    """Skills belonging to one agent, resolved against that agent's config."""
    home = agent_home(name)
    if home is None:
        raise HTTPException(status_code=404, detail="Unknown agent.")
    return list_skills_for(
        os.path.join(home, "skills"), os.path.join(home, "config.yaml")
    )


@app.get("/api/agents/{name}/context")
def get_agent_context(name: str):
    """Markdown context files inside one agent's home."""
    home = agent_home(name)
    if home is None:
        raise HTTPException(status_code=404, detail="Unknown agent.")
    return list_context_files_for(home)


def read_confined_file(root: str, rel_path: str, suffix: str = None):
    """Read a file under ``root``, refusing anything that escapes it."""
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, rel_path))
    if target != root_real and not target.startswith(root_real + os.sep):
        raise HTTPException(status_code=400, detail="Invalid file path.")
    if suffix and not target.lower().endswith(suffix):
        raise HTTPException(status_code=400, detail=f"Only {suffix} files can be read.")
    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail="File not found.")
    try:
        with open(target, "r", encoding="utf-8") as f:
            return {"content": f.read()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {e}")


@app.get("/api/agents/{name}/content")
def get_agent_file(name: str, rel_path: str = Query(...)):
    """Read one markdown file (SOUL.md, a memory, ...) from an agent's home."""
    home = agent_home(name)
    if home is None:
        raise HTTPException(status_code=404, detail="Unknown agent.")
    return read_confined_file(home, rel_path, suffix=".md")


@app.get("/api/agents/{name}/skills/content")
def get_agent_skill_content(name: str, rel_path: str = Query(...)):
    """Read one agent's SKILL.md."""
    home = agent_home(name)
    if home is None:
        raise HTTPException(status_code=404, detail="Unknown agent.")
    return read_confined_file(
        os.path.join(home, "skills"), os.path.join(rel_path, "SKILL.md")
    )


# --- Chat Send Endpoints ---
#
# Two ways to run the same turn. /api/chat/stream is what the panel uses: it
# proxies the gateway's SSE chat endpoint, so text arrives as it is generated
# and the tool calls and reasoning behind it are visible while they happen.
# /api/chat is the original single-shot POST, kept as the fallback for when the
# stream cannot be opened at all.

# Streaming runs over /v1/chat/completions — the same endpoint the single-shot
# path posts to, with `stream: true` set. The gateway also has a native
# /api/sessions/{id}/chat/stream whose events are richer (it streams reasoning
# deltas and tool *results*), but reaching it means creating the session first,
# and POST /api/sessions persists its `model` column as the advertised virtual
# model name — which the turn then tries to run as a real model and the
# provider rejects. The completions path lets the gateway mint and model the
# session exactly as it does today, and its tool events are better correlated
# anyway: they carry the tool_call id, where the session stream's do not.
#
# The OpenAI chunk format is translated here to a small flat vocabulary so the
# browser side stays a single switch:
#   session   the session this turn belongs to, sent before anything else
#   delta     an assistant text delta
#   thinking  a chunk of the reasoning trace behind the reply
#   tool      a tool call starting or settling
#   approval  a tool is blocked waiting for the user to allow or refuse it
#   error     the turn failed
#   end       the stream is over, however it ended

# The gateway's own names for the events it adds to the stream. Reasoning and
# tool *results* arrive on these; before the gateway carried them the panel
# could only draw a spinner turning into an unconditional green check, and both
# the reasoning and the tool output showed up when the finished turn's
# transcript was re-read — which is why an expanded step sat empty until the
# whole turn ended, and a tool that had failed looked like one that had not.
TOOL_PROGRESS_EVENT = "hermes.tool.progress"
REASONING_EVENT = "hermes.reasoning.delta"
APPROVAL_EVENT = "hermes.approval.request"

# Pending approvals the panel is allowed to answer, keyed by an opaque token.
#
# The gateway resolves an approval by its session key, but that key is not
# something to hand to page JavaScript: it also scopes memory and the session's
# standing approvals, so a script that collected one could answer approvals for
# a turn the user is not looking at. The token is single-purpose, unguessable,
# and forgotten once used — the page can answer the approval it was shown and
# nothing else.
_APPROVAL_TOKENS: "OrderedDict[str, dict]" = OrderedDict()
# Comfortably longer than the gateway's approval timeout, so a token never
# expires while the request it answers is still live; the 409 the gateway
# returns for a lapsed approval is the authority on that, not this.
_APPROVAL_TOKEN_TTL_SECONDS = 900
_APPROVAL_TOKEN_LIMIT = 256


def _prune_approval_tokens(now: float) -> None:
    for token, entry in list(_APPROVAL_TOKENS.items()):
        if now - entry["created"] > _APPROVAL_TOKEN_TTL_SECONDS:
            _APPROVAL_TOKENS.pop(token, None)
    while len(_APPROVAL_TOKENS) > _APPROVAL_TOKEN_LIMIT:
        _APPROVAL_TOKENS.popitem(last=False)


def _remember_approval(session_key: str) -> str:
    """Mint a token the panel can use to answer this one approval."""
    now = time.time()
    token = secrets.token_urlsafe(18)
    _APPROVAL_TOKENS[token] = {"session_key": session_key, "created": now}
    # After the insert, not before: pruning first leaves room for one more and
    # the store settles one entry above the cap forever.
    _prune_approval_tokens(now)
    return token


def _approval_session_key(token: str) -> Optional[str]:
    entry = _APPROVAL_TOKENS.get(token)
    if entry is None:
        return None
    if time.time() - entry["created"] > _APPROVAL_TOKEN_TTL_SECONDS:
        _APPROVAL_TOKENS.pop(token, None)
        return None
    return entry["session_key"]


def _hermes_headers():
    return {
        "Authorization": f"Bearer {API_SERVER_KEY}",
        "Content-Type": "application/json",
    }


def _sse_frame(kind: str, **payload):
    payload["type"] = kind
    return f"data: {json.dumps(payload)}\n\n"


async def _iter_sse(response):
    """Yield (event_name, payload) pairs from an SSE response body."""
    event = None
    data_lines = []
    async for line in response.aiter_lines():
        if line.startswith(":"):
            # A keepalive. The gateway sends these while a long turn runs.
            continue
        if not line:
            if data_lines:
                raw = "\n".join(data_lines)
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {"raw": raw}
                yield event or "message", payload
            event, data_lines = None, []
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())


def _translate_event(name: str, payload: dict):
    """One upstream SSE event -> the frames the panel understands (often none)."""
    if name == TOOL_PROGRESS_EVENT:
        status = payload.get("status") or ""
        tool_name = payload.get("tool") or payload.get("tool_name") or "tool"
        call_id = payload.get("toolCallId") or ""
        if status == "running":
            # `label` is the gateway's own rendering of the call — the same
            # preview its native clients show — so we use it rather than
            # re-deriving one from raw arguments.
            return [_sse_frame(
                "tool", phase="started", tool_name=tool_name, call_id=call_id,
                args=payload.get("label") or "",
            )]
        if status in ("completed", "failed"):
            # `result` is the tool's own output, trimmed by the gateway to keep
            # a chatty tool from stalling the reply text queued behind it. It
            # is what an opened step shows while the turn is still running;
            # the reconcile at the end of the turn replaces it with the full
            # value from the stored transcript.
            detail = payload.get("result") or ""
            if detail and payload.get("resultTruncated"):
                detail = detail + "\n\n… truncated — reopen the session for the full output."
            return [_sse_frame(
                "tool", phase="settled", tool_name=tool_name, call_id=call_id,
                status="error" if status == "failed" else "ok",
                summary=payload.get("label") or "",
                detail=detail,
            )]
        return []

    if name == REASONING_EVENT:
        text = payload.get("delta") or ""
        return [_sse_frame("thinking", text=text)] if text else []

    if name == APPROVAL_EVENT:
        session_key = payload.get("sessionKey") or ""
        if not session_key:
            # Nothing to answer it with; better to say the turn is blocked than
            # to render buttons that cannot resolve anything.
            return [_sse_frame(
                "error",
                message="A tool is waiting for approval, but this turn sent no way to answer it.",
            )]
        choices = [c for c in (payload.get("choices") or []) if isinstance(c, str)]
        return [_sse_frame(
            "approval",
            token=_remember_approval(session_key),
            command=payload.get("command") or "",
            description=payload.get("description") or "",
            choices=choices or ["once", "deny"],
            timeout_seconds=payload.get("timeoutSeconds") or 60,
        )]

    # Anything else on this stream is an OpenAI completion chunk.
    if payload.get("raw") == "[DONE]":
        return []
    choices = payload.get("choices")
    finish_reason = ""
    if isinstance(choices, list) and choices:
        delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
        text = (delta or {}).get("content") if isinstance(delta, dict) else None
        if text:
            return [_sse_frame("delta", text=text)]
        if isinstance(choices[0], dict):
            finish_reason = choices[0].get("finish_reason") or ""
    error = payload.get("error")
    if isinstance(error, dict):
        return [_sse_frame("error", message=error.get("message") or "Hermes reported an error.")]
    # A turn can also fail with no message to show: the gateway attaches
    # `error` only when it has text for it, and the mid-stream crash path emits
    # a bare `finish_reason: "error"` chunk. Both used to land here and be
    # dropped, leaving a reply that simply stopped with nothing said about why.
    if finish_reason and finish_reason != "stop":
        hermes = payload.get("hermes")
        message = (hermes or {}).get("error") if isinstance(hermes, dict) else None
        if not message:
            message = (
                "The reply was cut off — Hermes hit its output limit."
                if finish_reason == "length"
                else "The turn failed before it finished."
            )
        return [_sse_frame("error", message=message)]
    return []


async def _chat_stream_frames(req: ChatRequest):
    headers = _hermes_headers()
    if req.session_id:
        headers["X-Hermes-Session-Id"] = req.session_id
    payload = {
        "model": "hermes",
        "messages": [{"role": "user", "content": req.message}],
        "stream": True,
    }
    # No read timeout: a turn can think and run tools for many minutes, and the
    # gateway sends keepalive frames throughout. Connect and write stay bounded
    # so an unreachable gateway still fails fast.
    timeout = httpx.Timeout(connect=15.0, read=None, write=30.0, pool=15.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", HERMES_API_URL, json=payload, headers=headers
            ) as res:
                if res.status_code != 200:
                    body = (await res.aread()).decode("utf-8", "replace")
                    yield _sse_frame("error", message=f"Hermes API error {res.status_code}: {body[:300]}")
                    return
                # The gateway mints a session for a first turn and names it on
                # the response, exactly as it does for the single-shot path.
                session_id = res.headers.get("X-Hermes-Session-Id") or req.session_id
                if session_id:
                    yield _sse_frame("session", session_id=session_id)
                async for name, event in _iter_sse(res):
                    for frame in _translate_event(name, event):
                        yield frame
    except httpx.RequestError as exc:
        yield _sse_frame("error", message=f"Could not reach Hermes API server: {exc}")
    except Exception as exc:
        yield _sse_frame("error", message=f"Chat stream failed: {exc}")


@app.post("/api/chat/stream")
async def stream_chat_message(req: ChatRequest):
    """Chat, narrated while it happens.

    The gateway has always exposed this; the dashboard simply never asked for
    it, which is why replies used to land in one silent block minutes later.
    """
    async def events():
        async for frame in _chat_stream_frames(req):
            yield frame
        # Always terminates the stream, whichever branch above ended it.
        yield _sse_frame("end")

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ChatApprovalDecision(BaseModel):
    choice: str
    reason: Optional[str] = None

    @field_validator("choice")
    @classmethod
    def _known_choice(cls, v: str) -> str:
        choice = (v or "").strip().lower()
        # Mirrors the gateway's own set. Checked here too so a typo is a 422
        # naming the valid options rather than a 400 relayed from upstream.
        if choice not in {"once", "session", "always", "deny"}:
            raise ValueError("choice must be one of: once, session, always, deny")
        return choice


@app.post("/api/chat/approvals/{token}")
async def decide_chat_approval(token: str, req: ChatApprovalDecision):
    """Answer the approval a streamed turn is parked on.

    The turn is still running while this is called — the tool is blocked on
    the gateway waiting for exactly this, and the SSE stream the panel is
    reading stays open throughout. So the reply to this request is not what
    tells the user what happened; the stream is, and it resumes on its own
    once the decision lands.
    """
    session_key = _approval_session_key(token)
    if session_key is None:
        raise HTTPException(
            status_code=404,
            detail="That approval is no longer answerable — it may have timed out.",
        )
    payload = {"choice": req.choice}
    if req.reason:
        payload["reason"] = req.reason
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post(
                f"{HERMES_API_BASE}/v1/approvals/{session_key}/decision",
                json=payload,
                headers=_hermes_headers(),
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not reach Hermes API server: {exc}"
        )
    if res.status_code == 409:
        # The agent already gave up on it. Retiring the token keeps a second
        # click from re-asking a question that no longer has an answer.
        _APPROVAL_TOKENS.pop(token, None)
        raise HTTPException(
            status_code=409,
            detail="Too late — that approval already expired and was treated as a refusal.",
        )
    if res.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Hermes API error {res.status_code}: {res.text[:300]}",
        )
    # One decision per request; the token has done its job.
    _APPROVAL_TOKENS.pop(token, None)
    return {"ok": True, "choice": req.choice}


@app.post("/api/chat")
async def send_chat_message(req: ChatRequest):
    # Prepare the headers and request for Hermes API Server
    headers = {
        "Authorization": f"Bearer {API_SERVER_KEY}",
        "Content-Type": "application/json"
    }

    # If a session ID is provided, include it in the headers for session continuity
    if req.session_id:
        headers["X-Hermes-Session-Id"] = req.session_id

    # Payload for the completions API
    payload = {
        "model": "hermes",
        "messages": [
            {"role": "user", "content": req.message}
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(HERMES_API_URL, json=payload, headers=headers)
            
            if response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail=f"Hermes API error: {response.text}")
                
            data = response.json()
            
            # Extract returned session ID from headers to allow continuing the thread
            returned_session_id = response.headers.get("X-Hermes-Session-Id", req.session_id)
            
            # Return response alongside the session_id
            return {
                "response": data["choices"][0]["message"]["content"],
                "session_id": returned_session_id
            }
            
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Could not reach Hermes API server: {exc}")

_integrations_cache = {"at": 0.0, "payload": None}
# Long enough that the 7s approvals poll does not re-parse the workflows source
# on every tick, short enough that the 30s integrations poll always gets fresh
# numbers.
_INTEGRATIONS_TTL = 10.0


def integrations_payload(max_age: float = 0.0):
    """Sources and their grants, assembled from config and real call history.

    `max_age` lets a caller accept a slightly stale answer. The Integrations tab
    passes 0 and always recomputes; the approvals queue, polled far more often
    and only needing the producer's grant list, accepts the cached one.
    """
    if max_age:
        cached = _integrations_cache["payload"]
        if cached is not None and (time.time() - _integrations_cache["at"]) < max_age:
            return cached
    agents = [s for s in (agent_summary(n) for n in agent_names()) if s]
    payload = integrations.build(
        agents=agents,
        db_dir=DB_DIR,
        src_dir=adk_live.WORKFLOWS_SRC_DIR,
        state_db=STATE_DB,
        call_log_dir=INTEGRATION_CALL_LOG_DIR,
        config_path=INTEGRATIONS_CONFIG,
    )
    _integrations_cache["payload"] = payload
    _integrations_cache["at"] = time.time()
    return payload


@app.get("/api/integrations")
def get_integrations():
    """Can each part of the system still reach what it needs, and when did it last?

    Grouped by source system rather than by protocol. MCP versus direct API
    client is an implementation detail that hides the thing being asked: a
    single source commonly has several grants, held by different consumers,
    with different scopes and independent failure. One endpoint, because the
    rollup on a source row is only meaningful against all of its grants.
    """
    return integrations_payload()


# --- Messaging channels ---
# Read and write, both proxied to Hermes's own messaging-platform API. See
# backend/channels.py for why this dashboard holds a client rather than a
# second copy of the env schema those writes go through.


class ChannelUpdate(BaseModel):
    enabled: Optional[bool] = None
    # Only the keys that changed. The catalog hands back secrets redacted
    # ("8818...CJYE"), so sending the whole form would write the redaction
    # over the real token — the frontend sends a key only when the operator
    # typed into it, and clear_env for one they emptied.
    env: dict = {}
    clear_env: List[str] = []


@app.get("/api/channels")
async def get_channels():
    """The channels this dashboard exposes, with their real configuration."""
    try:
        return await channels.list_channels(DB_DIR)
    except channels.ChannelsUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.put("/api/channels/{channel_id}")
async def put_channel(channel_id: str, body: ChannelUpdate):
    """Save one channel: enable/disable it and set or clear its env vars.

    Takes effect on the gateway's next start, which is why the frontend offers
    a restart straight after a successful save.
    """
    try:
        result = await channels.update_channel(
            channel_id, body.enabled, body.env, body.clear_env
        )
    except channels.ChannelsUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "result": result}


@app.post("/api/channels/restart")
async def post_channels_restart():
    """Restart the gateway so saved channel changes come up."""
    try:
        return await channels.restart_gateway()
    except channels.ChannelsUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# --- MCP connections (default profile) ---
# The write side of what the chat sidebar's Connections list shows. Proxied to
# Hermes's own MCP API for the same reason channels are — see
# backend/mcp_servers.py. Deliberately the *default* profile only: this is the
# profile the chat on this dashboard talks to, and a settings page that could
# silently be editing the worker profile's connections while the sidebar beside
# it shows the default's is a page you cannot trust.
#
# /api/integrations stays the status report over the same servers. This is the
# control surface; that one is read-only on purpose, and the two agree because
# both ultimately read the same config.


class MCPServerCreate(BaseModel):
    name: str
    # http servers carry a url; stdio servers a command (+args). Hermes decides
    # which it is from whichever is present, and rejects a request with neither.
    url: Optional[str] = None
    command: Optional[str] = None
    args: List[str] = []
    env: dict = {}
    auth: Optional[str] = None
    # Provisioning input only. Hermes writes it to the profile's .env and keeps
    # a ${VAR} reference in the header, so it is never read back by the list.
    bearer_token: Optional[str] = None


class MCPServerEdit(BaseModel):
    """Fields deep-merged onto an existing server's config entry.

    Narrow on purpose. A merge cannot delete a key, so this can change where a
    connection points and add or update env values, but removing an env var —
    or converting an http server to stdio — is remove-and-re-add, which is what
    the page offers for those.
    """

    url: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[dict] = None


class MCPServerEnabled(BaseModel):
    enabled: bool


@app.get("/api/mcp/servers")
async def get_mcp_servers():
    """The default profile's MCP connections, with auth state, secrets redacted."""
    try:
        return await mcp_servers.list_servers(DB_DIR)
    except mcp_servers.MCPUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/mcp/servers")
async def post_mcp_server(body: MCPServerCreate):
    """Add a connection to the default profile."""
    if not body.url and not body.command:
        raise HTTPException(
            status_code=400,
            detail="A connection needs either a URL (http) or a command (stdio).",
        )
    try:
        result = await mcp_servers.add_server(
            name=body.name,
            url=body.url,
            command=body.command,
            args=body.args,
            env=body.env,
            auth=body.auth,
            bearer_token=body.bearer_token,
        )
    except mcp_servers.MCPUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    _integrations_cache["at"] = 0.0
    return result


@app.put("/api/mcp/servers/{name}")
async def put_mcp_server(name: str, body: MCPServerEdit):
    """Edit an existing connection's target or env, leaving its secrets alone."""
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    try:
        result = await mcp_servers.edit_server(name, fields)
    except mcp_servers.MCPUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    _integrations_cache["at"] = 0.0
    return result


@app.put("/api/mcp/servers/{name}/enabled")
async def put_mcp_server_enabled(name: str, body: MCPServerEnabled):
    """Turn one connection on or off, keeping its settings either way."""
    try:
        result = await mcp_servers.set_enabled(name, body.enabled)
    except mcp_servers.MCPNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except mcp_servers.MCPUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    _integrations_cache["at"] = 0.0
    return result


@app.delete("/api/mcp/servers/{name}")
async def delete_mcp_server(name: str):
    """Remove a connection from the default profile."""
    try:
        result = await mcp_servers.remove_server(name)
    except mcp_servers.MCPNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except mcp_servers.MCPUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    _integrations_cache["at"] = 0.0
    return result


@app.get("/api/settings/integrations")
def get_settings_integrations():
    """The read-only three-quarters of the integrations settings page.

    Workflow API access, automation output targets and email identities. The
    fourth quarter — the assistant's MCP connections — is /api/mcp/servers,
    which is the only one of the four this dashboard can write: those are
    Hermes configuration reachable through Hermes's own API, while these are
    another container's compose environment.

    Never cached. It is read when a settings section opens, and the failure it
    has to report honestly — the workflows service being down — is exactly the
    state a stale cache would paper over.
    """
    return settings_integrations.build(
        db_dir=DB_DIR,
        src_dir=adk_live.WORKFLOWS_SRC_DIR,
        workflows_url=ADK_WORKFLOWS_URL,
    )


@app.post("/api/mcp/servers/{name}/test")
async def post_mcp_server_test(name: str):
    """Connect to one server, list its tools, disconnect.

    A failed probe comes back 200 with ``ok: false`` and the reason — that is
    the answer the button asks for, not an error in asking. Only a broken link
    to Hermes itself is a 502.
    """
    try:
        return await mcp_servers.test_server(name)
    except mcp_servers.MCPNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except mcp_servers.MCPUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/integrations/consumer/{consumer}")
def get_consumer_integrations(consumer: str, origin: Optional[str] = Query(None)):
    """The same grants, read from one consumer's end.

    The chat sidebar and the review queue both need "what can this actor
    reach", which is the Integrations tab's grouping inverted. Derived from the
    same payload so the two screens can never disagree.

    `origin=mcp` narrows to connections made over MCP — what the chat sidebar
    asks for, since that is the only way a conversation reaches anything.
    """
    return {
        "consumer": consumer,
        "origin": origin,
        "grants": integrations.grants_for_consumer(
            integrations_payload(), consumer, origin=origin
        ),
    }


# --- ADK Agent Teams & Scorecard ---
# Prefixed /api/adk/ deliberately: /api/agents above is the Hermes *profile*
# inventory, and these are ADK teams. Two different things that both want the
# word "agents".

@app.get("/api/adk/health")
def adk_health():
    """Is the ADK server up, and what does it serve?

    Degrades quietly. The server being down is an ordinary state — teams whose
    root the live endpoint cannot describe still render from parsed source — so
    this reports a status, it never raises.
    """
    if not ADK_WORKFLOWS_URL:
        return {"ok": False, "url": None, "apps": [], "error": "no ADK server configured"}
    try:
        resp = httpx.get(f"{ADK_WORKFLOWS_URL}/list-apps", timeout=1.5)
        resp.raise_for_status()
        return {"ok": True, "url": ADK_WORKFLOWS_URL, "apps": resp.json(), "error": None}
    except Exception as exc:
        return {"ok": False, "url": ADK_WORKFLOWS_URL, "apps": [], "error": str(exc)}


def _adk_teams():
    """The team list. One source of app ids for every endpoint that needs to
    enumerate apps rather than describe one."""
    if not ADK_WORKFLOWS_URL:
        return []
    return adk_live.fetch_teams(ADK_WORKFLOWS_URL, "workflows")


@app.get("/api/adk/teams")
def adk_teams():
    """Every ADK team this host runs.

    One server now, but still two techniques, because neither covers every app:

    * Where the root is an LlmAgent, the team is read from the server's own
      app-info. Its instructions and model come from imported constants and a
      branching factory in app/config.py, which a non-executing parser would
      have to become an interpreter to follow.
    * Where the root is a SequentialAgent/LoopAgent, app-info 400s and the app
      is omitted from the routing root's listing, so the team is parsed from
      the bind-mounted source instead — see adk_live.fetch_teams.

    Each team carries `source` so the UI can say which it is looking at, and
    what that implies about freshness.
    """
    teams = _adk_teams()
    return {
        "workflows_url": ADK_WORKFLOWS_URL,
        "count": len(teams),
        "apps": teams,
    }


@app.get("/api/adk/scorecard")
def adk_scorecard_endpoint(app: str = Query(...), days: int = Query(30)):
    """Aggregate eval / utilization / self-report stats across runs.

    `app` is required. It used to default to the one team that wrote traces;
    with that team gone there is no app a default could name without guessing,
    and a scorecard attributed to the wrong app is worse than an error.

    Drift now reads the workflows source rather than the retired /opt/adk/apps
    layout. Where that source is not mounted the sha is None and aggregate()
    reports drift as unknown — never as "no drift".
    """
    days = max(1, min(days, 3650))
    runs = adk_scorecard.load_runs(ADK_STATE_DIR, app, days)
    sha = adk_live.app_sha(app)
    return adk_scorecard.aggregate(runs, app, days, ADK_STATE_DIR, sha)


@app.get("/api/adk/fleet")
def adk_fleet(days: int = Query(3650)):
    """Headline health for every app at once, for the fleet view.

    The full scorecard is per-app and expensive; the fleet view needs four
    numbers per app and nothing else, so it gets `summarize` rather than N
    calls to `aggregate`. Apps with no traces still appear, with a zero run
    count — an app that has never run is a fact worth showing, not an omission.
    """
    days = max(1, min(days, 3650))
    apps = []
    for team in _adk_teams():
        name = team.get("app")
        if not name:
            continue
        # A routing root is not a workflow: it has no runs of its own and its
        # health is whatever the workflows it routes to are doing. Giving it a
        # row would report a permanent zero next to the real ones.
        if team.get("router"):
            continue
        runs = adk_scorecard.load_runs(ADK_STATE_DIR, name, days)
        apps.append(adk_scorecard.summarize(runs, name))
    return {"window_days": days, "count": len(apps), "apps": apps}


@app.get("/api/adk/runs")
def adk_runs(app: str = Query(...), limit: int = Query(50)):
    """Recent run traces, newest first."""
    runs = adk_scorecard.load_runs(ADK_STATE_DIR, app, 3650)
    return {"app": app, "count": len(runs), "runs": runs[:max(1, min(limit, 500))]}


# --- Metrics store ---
# System-wide, unlike /api/adk/* above: these read every producer at once —
# ADK runs, Hermes chat, Hermes automations — through one grain. See
# metrics_store for why cost is reported per class and never totalled.


def _metrics_call(fn, *args, **kwargs):
    """Run a store query, degrading to 503 rather than 500.

    The store reads databases another process is writing and JSONL that may be
    mid-append. None of that should be able to take the whole dashboard down,
    and the Metrics tab can render an unavailable state — the same contract
    `_graph_call` below already established for the graph backend.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Metrics store unavailable: {exc}")


@app.get("/api/metrics/health")
def metrics_health():
    """What the store can see: which profiles were found, and row counts."""
    return _metrics_call(metrics_store.health)


@app.get("/api/metrics/cost")
def metrics_cost(days: int = Query(30)):
    """Spend and usage per cost class.

    Returns one line per class and no total, deliberately. `metered` is money;
    `included` is subscription usage whose marginal cost is zero but whose
    volume is real; `unpriced` is local inference with no rate. Summing them
    would produce a figure that is neither spend nor capacity.
    """
    return _metrics_call(metrics_store.cost_summary, max(1, min(days, 3650)))


@app.get("/api/metrics/models")
def metrics_models(days: int = Query(30)):
    return {"models": _metrics_call(metrics_store.by_model, max(1, min(days, 3650)))}


@app.get("/api/metrics/activity")
def metrics_activity(days: int = Query(30)):
    """Activity counts by kind and source.

    `succeeded`/`failed` are null wherever `outcome_known` is 0: Hermes sessions
    record how they stopped, not whether they worked, and a zero there would be
    a verdict nobody issued.
    """
    return {"activity": _metrics_call(metrics_store.activity_summary, max(1, min(days, 3650)))}


@app.get("/api/metrics/timeseries")
def metrics_timeseries(days: int = Query(30)):
    return {"days": _metrics_call(metrics_store.timeseries, max(1, min(days, 3650)))}


@app.get("/api/metrics/outputs")
def metrics_outputs(days: int = Query(30)):
    """What the fleet produced and what it read, by kind.

    `produced` counts side effects that actually happened, in the shared
    vocabulary from workflows/app/run_metrics.py — so `draft_email` (created in
    a mailbox, delivered to nobody), `approved_email` (sent, because a human
    approved it) and `auto_email` (sent unattended) are separate numbers and
    stay that way. `unattended_sends` is lifted out, and counts `auto_email`
    alone, because it is the one figure that says how much left the building
    with nobody looking — an approved send had someone looking by definition.

    Note that mail is counted where it becomes real, which is not where it was
    decided: the agent that queues a reply produces a `review_item`, and the
    review executor produces the `draft_email` or `approved_email` later.
    """
    return _metrics_call(metrics_store.outputs, max(1, min(days, 3650)))


@app.get("/api/metrics/flow")
def metrics_flow(days: int = Query(30)):
    """Where mail went, as flows rather than totals.

    Two diagrams, and they are separate on purpose: filing a message and
    drafting a reply to it are orthogonal, so chaining them into one flow would
    count the drafts twice. See review_flow.py for why, and for why the review
    diagram carries `Backlog at start` and `Still pending` nodes — without them
    an asynchronous queue cannot balance across a window boundary.
    """
    return _metrics_call(
        review_flow.flow,
        ADK_STATE_DIR,
        {state: path for state, path in REVIEW_STATE_DIRS},
        max(1, min(days, 3650)),
    )


@app.get("/api/metrics/agents")
def metrics_agents(app: str = Query(None), days: int = Query(30)):
    """Per agent element, with measured and claimed scores side by side.

    `checkpoint_pass_rate` is measured from what the stages recorded;
    `self_score` is a model's opinion of its own turn. They are never averaged
    together — see metrics_store.agent_scorecard.
    """
    return {"agents": _metrics_call(
        metrics_store.agent_scorecard, app, max(1, min(days, 3650)))}


@app.get("/api/metrics/evals")
def metrics_evals(days: int = Query(30)):
    """Eval outcomes, from the durable JSONL the graders append to.

    agents-cli's own per-case detail goes to `artifacts/`, which is not a
    mounted volume and dies with the container — so this is the only record
    that survives long enough to answer whether a pipeline is getting better.
    """
    return _metrics_call(metrics_store.evals, max(1, min(days, 3650)))


@app.get("/api/metrics/automations")
def metrics_automations(days: int = Query(30)):
    """Scheduled-job executions, which are counted separately from sessions.

    An execution and a chat session are different populations — most scheduled
    runs never open a model session — so these numbers are not addable to the
    activity counts above and are returned on their own route to keep that
    obvious at the point of use.
    """
    return {"jobs": _metrics_call(metrics_store.automation_executions, max(1, min(days, 3650)))}


class WikiSearchRequest(BaseModel):
    query: str
    limit: int = 25


# --- Wiki memory ---
# Read views over the markdown the workflows service writes. This replaced two
# backends — the Graphiti REST proxy and a direct Neo4j connection for entity
# browsing — with a directory read. See wiki_api for why there are no mutation
# routes on this unauthenticated port.


def _wiki_call(fn, *args, **kwargs):
    """Run a wiki_api call, mapping unavailability onto 503.

    The wiki is a bind mount rather than a service, so the failure it guards
    against is a missing or unreadable directory — which should say so rather
    than surface as a 500 with no explanation.
    """
    try:
        return fn(*args, **kwargs)
    except wiki_api.WikiUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Wiki unavailable: {exc}")


@app.get("/api/wiki/health")
def wiki_health():
    """Document and fact counts, for the tab's empty state."""
    return wiki_api.health()


@app.get("/api/wiki/documents")
def wiki_documents(q: Optional[str] = Query(None), limit: int = Query(100)):
    """Documents newest-first, optionally filtered by title or key."""
    docs = _wiki_call(wiki_api.documents, limit=limit, q=q or "")
    return {"count": len(docs), "documents": docs}


@app.get("/api/wiki/document/{slug}")
def wiki_document(slug: str):
    """One document: raw markdown, parsed sections, and both link directions."""
    doc = _wiki_call(wiki_api.document, slug)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@app.post("/api/wiki/search")
def wiki_search(req: WikiSearchRequest):
    """Full-text search over facts."""
    facts = _wiki_call(wiki_api.search, req.query, limit=req.limit)
    return {"count": len(facts), "facts": facts}


@app.get("/api/wiki/backlinks/{slug}")
def wiki_backlinks(slug: str):
    """Documents linking here — the one-hop neighbourhood."""
    return {"backlinks": _wiki_call(wiki_api.backlinks, slug)}


@app.get("/api/wiki/graph")
def wiki_graph(slug: Optional[str] = Query(None), depth: int = Query(1)):
    """The wikilink graph, whole or cut to one document's neighbourhood.

    `slug` is optional and `depth` only means anything alongside it — the
    unfocused form is the entire store, where a hop count has nothing to count
    from. Both are query parameters rather than path segments so that the one
    route serves /memory/graph and /memory/graph/<slug> alike.
    """
    return _wiki_call(wiki_api.graph, focus=slug or "", depth=depth)


# --- Stack health ---


@app.get("/api/health/services")
async def health_services(fresh: bool = Query(False)):
    """Every service in this stack, red/amber/green.

    Cached for a few seconds by default because the frontend asks on its shared
    poll and several browser tabs may be open; `fresh=1` bypasses that, which is
    what the modal's refresh control sends after someone has restarted
    something and wants to watch it come back.

    Never raises. This is the endpoint a reader turns to when things are
    already broken, and one unreachable service must not take the report down
    with it — every failure is a status on a row instead.
    """
    return await health.snapshot(max_age=0 if fresh else None)


# --- Tab deep links ---
# The frontend is a single page that keeps its active tab in the URL path
# (/chat, /cron, ...). Those paths are client-side routes with no file behind
# them, so serve index.html for each and let the app pick the tab. Registered
# before the static mount so they take precedence; enumerated explicitly rather
# than using a catch-all so real files under /app/frontend still resolve.
FRONTEND_DIR = "/app/frontend"
FRONTEND_INDEX = os.path.join(FRONTEND_DIR, "index.html")
# "skills", "context" and "teams" are retained as routes even though the Agents
# tab absorbed all three views — old links keep resolving instead of 404ing. The
# frontend's TAB_ALIASES redirects them to /agents once the shell boots.
#
# "agents" is listed here but has no button in the nav: it is the drill-in for a
# single automation's scorecards, reached from a row on /automations. The route
# has to keep working for that link and for bookmarks. "cron" now aliases to
# /automations rather than /agents — it was always the schedule question.
#
# "settings" is not a tab — it is an overlay the frontend opens over whichever
# tab you were on — but it owns a path for the same reason the tabs do: it is
# somewhere you can be sent a link to, and Back has to leave it.
#
# "health" is the other such overlay. Note the shape: the *page* is /health and
# the JSON behind it is /api/health/services, so the two never collide — every
# API route on this service is under /api/, and this list is only consulted for
# paths that are not.
TAB_PATHS = [
    "metrics", "chat", "kanban", "cron", "automations", "agents",
    "integrations",
    # "review" is what the tab is now called; "approvals" is kept because links
    # to it are already written down in messages and docs. The frontend folds
    # the old name onto the new tab and rewrites the address bar, so an old
    # link lands in the right place and stops being an old link.
    "review", "approvals",
    # "memory" is what the tab is called and what its paths are built from;
    # "graph" is the name it shipped under, kept for the same reason
    # "approvals" is. The frontend folds it onto /memory on arrival. Note that
    # /memory/graph is a *page inside* the memory tab, not this old name — the
    # rename freed the word up to mean the thing it actually describes.
    "memory", "graph",
    "settings",
    "health",
    "skills", "context", "teams",
]


def serve_frontend_index():
    """Return the SPA shell for a client-side tab route."""
    if not os.path.exists(FRONTEND_INDEX):
        raise HTTPException(status_code=404, detail="Frontend not built")
    # no-store: the shell is tiny and pulls live data on boot; a stale cached
    # copy is the usual cause of "my fix isn't showing up" after a rebuild.
    return FileResponse(FRONTEND_INDEX, headers={"Cache-Control": "no-store"})


for _tab in TAB_PATHS:
    app.add_api_route(
        f"/{_tab}",
        serve_frontend_index,
        methods=["GET"],
        include_in_schema=False,
        response_class=FileResponse,
    )


# The Metrics tab has a second page, /metrics/system, and it is the one worth
# sending to someone — "here is what the fleet is costing" is a link. Without
# this route it 404s on a cold load or a refresh, which is exactly when a pasted
# URL gets opened. The segment is the frontend's to interpret: an unknown one
# lands on the outcomes page rather than erroring.
def serve_frontend_metrics_view(view: str):
    """SPA shell for /metrics/<view>."""
    return serve_frontend_index()


app.add_api_route(
    "/metrics/{view}",
    serve_frontend_metrics_view,
    methods=["GET"],
    include_in_schema=False,
    response_class=FileResponse,
)


# The Memory tab addresses two things below its own root: one document
# (/memory/document/<slug>) and the graph, either whole (/memory/graph) or
# centred on a document (/memory/graph/<slug>). Both segments are the
# frontend's to interpret — an unknown view lands on the document list — but
# both need the shell served on a cold load, which is precisely when a link
# someone was sent gets opened.
#
# A document slug is an email address or an Attio record id, so it can contain
# dots and plus signs but never a slash: two segments is the whole shape, and
# the second is optional rather than a deeper path.
def serve_frontend_memory(view: str):
    """SPA shell for /memory/<view>."""
    return serve_frontend_index()


def serve_frontend_memory_item(view: str, item: str):
    """SPA shell for /memory/<view>/<slug>."""
    return serve_frontend_index()


for _memory_path, _memory_handler in (
    ("/memory/{view}", serve_frontend_memory),
    ("/memory/{view}/{item}", serve_frontend_memory_item),
):
    app.add_api_route(
        _memory_path,
        _memory_handler,
        methods=["GET"],
        include_in_schema=False,
        response_class=FileResponse,
    )


# A settings section is a page inside the overlay (/settings/channels), so it
# needs the shell too. Same reasoning as the agent paths below: the segment is
# the frontend's to interpret, and an unknown one lands on the first section
# rather than 404ing.
def serve_frontend_settings(section: str):
    """SPA shell for /settings/<section>."""
    return serve_frontend_index()


app.add_api_route(
    "/settings/{section}",
    serve_frontend_settings,
    methods=["GET"],
    include_in_schema=False,
    response_class=FileResponse,
)


# A conversation is addressable: /chat/<session id>. Same reasoning as the
# settings sections — the segment is the frontend's to interpret, and a session
# that no longer exists is a thing the app says so about, not a 404 from here.
def serve_frontend_chat_session(session_id: str):
    """SPA shell for /chat/<session id>."""
    return serve_frontend_index()


app.add_api_route(
    "/chat/{session_id}",
    serve_frontend_chat_session,
    methods=["GET"],
    include_in_schema=False,
    response_class=FileResponse,
)


# A task is addressable too: /kanban/<task id>. It is the unit of work people
# refer to by id in chat, and it was the one thing on the board with no link of
# its own. A task that no longer exists is the app's to say so about, same as a
# missing session — this route only hands back the shell.
def serve_frontend_kanban_task(task_id: str):
    """SPA shell for /kanban/<task id>."""
    return serve_frontend_index()


app.add_api_route(
    "/kanban/{task_id}",
    serve_frontend_kanban_task,
    methods=["GET"],
    include_in_schema=False,
    response_class=FileResponse,
)


# A review item is addressable: /review/<item id>. Same contract as the task and
# session routes — the shell only. Whether that id still names something
# pending, something already sent, or nothing at all is the app's to say, and it
# asks /api/review/item/<id>, which answers for every state rather than 404ing
# the moment a decision is made.
#
# The /approvals/<id> spelling is registered too, so a link written before the
# rename still opens the item rather than dropping its second segment on the
# floor and landing on the bare queue.
def serve_frontend_review_item(item_id: str):
    """SPA shell for /review/<item id>."""
    return serve_frontend_index()


for _review_path in ("/review/{item_id}", "/approvals/{item_id}"):
    app.add_api_route(
        _review_path,
        serve_frontend_review_item,
        methods=["GET"],
        include_in_schema=False,
        response_class=FileResponse,
    )


# An automation is addressable: /automations/<job id>. It is the page you send
# someone when a scheduled thing broke, so it has to survive being pasted into
# a message and opened cold. Same contract as the routes above — the shell
# only; whether that id still names a job is the app's to say, and it asks
# /api/automations/<id>, which 404s for a job that no longer exists.
def serve_frontend_automation(job_id: str):
    """SPA shell for /automations/<job id>."""
    return serve_frontend_index()


app.add_api_route(
    "/automations/{job_id}",
    serve_frontend_automation,
    methods=["GET"],
    include_in_schema=False,
    response_class=FileResponse,
)


# Deeper than a tab: the Agents tab can address one ADK agent's scorecard, so
# those paths need the shell too. Enumerated for the same reason as the tabs —
# a catch-all here would shadow real files under /app/frontend. The app and
# agent segments are read by the frontend, never by this route; it only has to
# return the shell rather than a 404.
def serve_frontend_agent_scorecard(app_name: str, agent_name: str):
    """SPA shell for /agents/scorecard/<app>/<agent>.

    The segments are declared only because FastAPI requires a path parameter to
    appear in the handler signature — they are the frontend's to interpret, and
    validating them here would mean this route knew the ADK roster.
    """
    return serve_frontend_index()


app.add_api_route(
    "/agents/scorecard",
    serve_frontend_index,
    methods=["GET"],
    include_in_schema=False,
    response_class=FileResponse,
)
app.add_api_route(
    "/agents/scorecard/{app_name}/{agent_name}",
    serve_frontend_agent_scorecard,
    methods=["GET"],
    include_in_schema=False,
    response_class=FileResponse,
)


# A Hermes profile is addressable the same way an ADK agent is:
# /agents/hermes/<profile>, optionally naming one of its cron jobs. Same
# reasoning as above — the segments belong to the frontend, and this route only
# has to hand back the shell instead of 404ing on a client-side path.
def serve_frontend_hermes_agent(profile: str, job_id: str = None):
    """SPA shell for /agents/hermes/<profile>[/<job id>]."""
    return serve_frontend_index()


app.add_api_route(
    "/agents/hermes/{profile}",
    serve_frontend_hermes_agent,
    methods=["GET"],
    include_in_schema=False,
    response_class=FileResponse,
)
app.add_api_route(
    "/agents/hermes/{profile}/{job_id}",
    serve_frontend_hermes_agent,
    methods=["GET"],
    include_in_schema=False,
    response_class=FileResponse,
)

@app.get("/js/{name}", include_in_schema=False)
def serve_frontend_script(name: str):
    """One of the app's JSX files, never cached.

    The shell is already served no-store for this reason; the app moved out of
    it into /js, so the same rule has to follow. StaticFiles would serve these
    with an ETag and let the browser hold a heuristically-fresh copy — meaning
    a deploy could leave a reader running last week's tab against this week's
    shell, which is worse than either version alone.

    Explicit route rather than a mount so it wins over the catch-all below.
    The name is a single path segment and must look like a file, so it cannot
    climb out of the directory.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.jsx", name or ""):
        raise HTTPException(status_code=404, detail="Not found")
    path = os.path.join(FRONTEND_DIR, "js", name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Not found")
    # text/babel, not text/javascript: the browser must not try to run this as
    # a script on its own — @babel/standalone fetches and compiles it.
    return FileResponse(
        path,
        media_type="text/babel",
        headers={"Cache-Control": "no-store"},
    )


# Serve the compiled frontend
if os.path.exists("/app/frontend"):
    app.mount("/", StaticFiles(directory="/app/frontend", html=True), name="frontend")
