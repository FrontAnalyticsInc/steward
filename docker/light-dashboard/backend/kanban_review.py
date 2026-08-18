"""Kanban ``needs_input`` blocks, as items in the review queue.

A worker that finishes code and cannot self-certify it calls ``kanban_block``
with ``kind="needs_input"``. That is the correct thing for it to do — but until
now it was a dead end: the task went to ``blocked``, the dashboard's kanban API
was read-only apart from archive, and the only way out was
``hermes kanban unblock`` in a terminal. A queue nobody can reach is not a
review gate, it is a leak.

The Review tab already is the place where a typed thing is put in front of a
human with a fixed set of legal actions. So this module does not build a second
one: it presents blocked-on-a-human tasks *as* review items, with the same
shape, through the same routes, and lets the existing type table in
``review_types.py`` say what may be done with them.

Two things make this different from the file-backed items:

* The store is ``kanban.db``, not ``approvals/``. Decisions are SQL, not a file
  move, and the state machine belongs to ``hermes_cli/kanban_db.py`` — which
  does not exist in this container. Every write below mirrors a specific
  function there and names it. Read that function before changing this one; the
  invariants (parent gating, the run pointer, the recurrence counter) are load
  bearing and are documented at their source, not here.
* The evidence is code. "Needs human code review" is unanswerable without the
  code, so the item carries the changed files' contents, read from the
  read-only source mounts. Paths come from the worker's comment, which is
  untrusted text, so they are resolved against an allow-list of roots and
  checked after symlink resolution.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from typing import Iterable, Optional

# ``needs_input`` is the only block kind a human can clear by deciding. The
# others describe the world, not a missing decision: ``dependency`` clears when
# a parent lands, ``capability`` needs a credential or a tool that isn't there,
# and ``transient`` is retried by the dispatcher. Putting them in this queue
# would fill it with rows whose buttons do nothing.
REVIEWABLE_BLOCK_KINDS = ("needs_input",)

REVIEW_TYPE = "code_review"

# Where a path in a worker's comment is allowed to point. The container mounts
# ``workflows/app`` and ``workflows/tests`` read-only for exactly this. Anything
# outside is dropped rather than read: the comment is written by an agent whose
# input includes arbitrary repository text.
#
# ``/opt/data/scripts`` is named specifically rather than as ``/opt/data``. The
# deployed wrapper scripts genuinely are part of what a worker changes — that
# directory is the live copy, and a change that never reached it is a change
# that never shipped — but /opt/data is also the gateway's databases and its
# auth material, and a review pane is a page that renders whatever it is
# pointed at.
DEFAULT_SOURCE_ROOTS = ("/opt/workflows", "/opt/data/scripts")

# A file is shown to be read. Past a point it is not read, it is scrolled past,
# and the cost is a response big enough to slow the queue for every other item.
MAX_FILE_BYTES = 60_000
MAX_FILES = 25

# Paths as they appear in a comment: backticked, bulleted, or in bare prose.
# Requires a directory separator and a source extension, so ordinary sentences
# do not produce phantom files.
#
# The lookbehind is the part that was wrong first time and looked right: without
# it, `tests/unit/test_x.py` matches from the inner slash and yields
# `/unit/test_x.py` — an absolute path that does not exist, listed to the
# reviewer as a changed file that could not be read. Anchoring on "not preceded
# by a path character" makes the match start where the path starts.
_PATH_RE = re.compile(
    r"(?<![\w./-])(/?[\w.-]+(?:/[\w.-]+)+\.(?:py|jsx?|tsx?|json|ya?ml|toml|md|sql|sh))\b"
)

_EXT_LANG = {
    ".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript",
    ".tsx": "tsx", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".md": "markdown", ".sql": "sql", ".sh": "bash",
}


def _connect(db_path: str) -> sqlite3.Connection:
    """Open kanban.db the way its owner does.

    ``busy_timeout`` is not optional. The dispatcher, every running worker and
    this process share one file, and a decision that raises "database is
    locked" because a heartbeat landed in the same millisecond is a decision the
    human has to make twice.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _now() -> int:
    return int(time.time())


def source_roots() -> tuple:
    raw = os.environ.get("KANBAN_REVIEW_SOURCE_ROOTS")
    if raw is None:
        return DEFAULT_SOURCE_ROOTS
    return tuple(p.strip() for p in raw.split(",") if p.strip())


# --- Reading ------------------------------------------------------------------


def _iso(ts) -> Optional[str]:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))


def extract_paths(text: str, workspace: Optional[str] = None) -> list:
    """Source paths mentioned in a worker's comment, in order, made absolute.

    Deliberately dumb. It is a *hint* about which files to show, and every hit
    is re-checked against the allow-list before anything is opened, so a false
    positive costs a dropped path and never a read outside the mounts.

    Relative paths are resolved against the task's workspace, because that is
    how workers actually write them: a worker that ran ``pytest`` in
    ``/opt/workflows`` reports ``tests/unit/test_x.py``, and dropping those
    would leave exactly the tests — half of what there is to review — off the
    page.
    """
    seen = []
    for match in _PATH_RE.finditer(text or ""):
        path = match.group(1).rstrip(".,;:)")
        if not path.startswith("/"):
            if not workspace:
                continue
            path = os.path.normpath(os.path.join(workspace, path))
        if path not in seen:
            seen.append(path)
    return seen


def _within_roots(path: str, roots: Iterable[str]) -> bool:
    """True when `path` really is inside one of `roots`.

    ``realpath`` first: the check has to be on where the path *lands*, not on
    how it is spelled. A symlink under the mount pointing at ``/opt/data`` would
    otherwise pass a prefix test and read the gateway's databases into a page.
    """
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return False
    for root in roots:
        root_real = os.path.realpath(root)
        if resolved == root_real or resolved.startswith(root_real + os.sep):
            return True
    return False


def read_source(path: str, roots: Iterable[str]) -> dict:
    """One changed file, as much of it as is worth showing.

    Never raises. A file that moved, or that lives on a mount this container
    does not have, still belongs in the list — the reviewer needs to know it was
    touched, and "not readable here" is a fact about the deployment they can act
    on, not an error that should cost them the rest of the item.
    """
    entry = {
        "path": path,
        "language": _EXT_LANG.get(os.path.splitext(path)[1], ""),
        "content": None,
        "truncated": False,
        "bytes": None,
        "unavailable": None,
    }
    if not _within_roots(path, roots):
        entry["unavailable"] = "outside the source mounts this dashboard can read"
        return entry
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(MAX_FILE_BYTES)
        entry["bytes"] = size
        entry["content"] = content
        entry["truncated"] = size > MAX_FILE_BYTES
    except FileNotFoundError:
        entry["unavailable"] = "not found at this path"
    except IsADirectoryError:
        entry["unavailable"] = "is a directory"
    except OSError as exc:
        entry["unavailable"] = f"unreadable: {type(exc).__name__}"
    return entry


def _comments(conn: sqlite3.Connection, task_id: str) -> list:
    rows = conn.execute(
        "SELECT author, body, created_at FROM task_comments WHERE task_id = ? "
        "ORDER BY created_at ASC, id ASC",
        (task_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _last_run(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT id, profile, status, outcome, summary, error, started_at, ended_at "
        "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return dict(row) if row else None


def _decision_event(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """The most recent decision this queue made about the task, if any.

    What makes a decided item still answerable at its own URL. The file-backed
    items get this by moving between directories; a task has no such thing, so
    the event log is the record.
    """
    row = conn.execute(
        "SELECT kind, payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind IN ('review_approved', 'review_changes_requested') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    return {"kind": row["kind"], "payload": payload, "created_at": row["created_at"]}


def build_item(
    conn: sqlite3.Connection, task: dict, roots: Iterable[str], *, with_code: bool = True
) -> dict:
    """A task, shaped as a review item.

    The generic template in the frontend renders `title`/`summary`/`fields`, so
    those are filled even though a hand-written template exists — an item should
    still read correctly if the template is ever removed.

    ``with_code=False`` lists the changed files without opening them. The queue
    route is polled and the item route is not: reading every file of every
    blocked task on a timer would put megabytes on the wire to render a list
    that shows a count. The paths are still resolved, so the count is right.
    """
    task_id = task["id"]
    comments = _comments(conn, task_id)
    run = _last_run(conn, task_id)
    decision = _decision_event(conn, task_id)

    # Newest first: the reason the task is blocked is in the last thing the
    # worker said, and on a long task that is a long scroll away otherwise.
    worker_comments = [c for c in comments if c["author"] != "reviewer"]
    latest = worker_comments[-1] if worker_comments else None

    workspace = task.get("workspace_path")
    paths = []
    for comment in reversed(worker_comments):
        for path in extract_paths(comment["body"], workspace):
            if path not in paths:
                paths.append(path)
        if paths:
            break
    dropped = max(0, len(paths) - MAX_FILES)
    if with_code:
        files = [read_source(p, roots) for p in paths[:MAX_FILES]]
    else:
        files = [{"path": p} for p in paths[:MAX_FILES]]

    blocked_at = (run or {}).get("ended_at") or task.get("started_at") or task.get("created_at")
    state = "pending" if task.get("status") == "blocked" else "decided"

    item = {
        "id": task_id,
        "source": "kanban",
        "state": state,
        "review_type": REVIEW_TYPE,
        "created_at": _iso(blocked_at),
        "title": task.get("title") or task_id,
        # The worker's own words for why it stopped. This is the question being
        # asked, so it is the summary rather than the task description.
        "summary": (run or {}).get("summary") or "Blocked awaiting a human decision.",
        "body": (latest or {}).get("body") or task.get("body") or "",
        "task": {
            "id": task_id,
            "status": task.get("status"),
            "assignee": task.get("assignee"),
            "block_kind": task.get("block_kind"),
            "block_recurrences": task.get("block_recurrences"),
            "workspace_path": task.get("workspace_path"),
            "branch_name": task.get("branch_name"),
            "body": task.get("body"),
        },
        "changed_files": files,
        "changed_files_dropped": dropped,
        "comments": comments,
        "run": run,
        "fields": [
            {"label": "Task", "value": task_id},
            {"label": "Assignee", "value": task.get("assignee") or "—"},
            {"label": "Workspace", "value": task.get("workspace_path") or "—"},
            {"label": "Files changed", "value": len(files) + dropped},
            {"label": "Blocked at", "value": _iso(blocked_at) or "—"},
        ],
        # Same contract the file-backed producers use, so the provenance panel
        # and the integrations lookup work unchanged.
        "producer": {
            "agent": task.get("assignee") or "kanban worker",
            "stage": (run or {}).get("profile"),
            "consumer": task.get("assignee") or None,
            "at": _iso(blocked_at),
        },
        "reason": (run or {}).get("summary"),
    }
    if decision:
        item["decision"] = (
            "approve" if decision["kind"] == "review_approved" else "reject"
        )
        item["decided_at"] = _iso(decision["created_at"])
        item["decided_by"] = "dashboard"
        item["action"] = decision["payload"].get("action")
        if decision["kind"] == "review_changes_requested":
            item["rejection_reason"] = decision["payload"].get("note")
        item["execution"] = {"state": "not_applicable"}
    return item


def pending_items(db_path: str, roots: Optional[Iterable[str]] = None) -> list:
    """Every task waiting on a human, newest block first."""
    if not os.path.exists(db_path):
        return []
    roots = tuple(roots) if roots is not None else source_roots()
    placeholders = ",".join("?" for _ in REVIEWABLE_BLOCK_KINDS)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM tasks WHERE status = 'blocked' "
            f"AND block_kind IN ({placeholders}) ORDER BY started_at DESC, id DESC",
            REVIEWABLE_BLOCK_KINDS,
        ).fetchall()
        return [build_item(conn, dict(r), roots, with_code=False) for r in rows]
    finally:
        conn.close()


def get_item(db_path: str, task_id: str, roots: Optional[Iterable[str]] = None) -> Optional[dict]:
    """One task as a review item, in whatever state it is now.

    Answers for a task that has already been decided, which is the whole reason
    ``/review/<id>`` can be reloaded after pressing a button.
    """
    if not os.path.exists(db_path):
        return None
    roots = tuple(roots) if roots is not None else source_roots()
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        task = dict(row)
        # A task that was never blocked on a human is not a review item and must
        # not render as one — an approve button on an in-flight task would be a
        # button that completes work nobody reviewed.
        if task.get("status") == "blocked" and task.get("block_kind") not in REVIEWABLE_BLOCK_KINDS:
            return None
        if task.get("status") != "blocked" and not _decision_event(conn, task_id):
            return None
        return build_item(conn, task, roots)
    finally:
        conn.close()


def is_kanban_item(db_path: str, item_id: str) -> bool:
    """Whether this id names a task at all.

    The two id spaces do not collide — approval ids are bare hex, task ids carry
    the ``t_`` prefix — but the routes check the store rather than the spelling,
    because a prefix convention is not something to bet a write on.
    """
    if not item_id or not item_id.startswith("t_") or not os.path.exists(db_path):
        return False
    conn = _connect(db_path)
    try:
        return conn.execute(
            "SELECT 1 FROM tasks WHERE id = ? LIMIT 1", (item_id,)
        ).fetchone() is not None
    finally:
        conn.close()


# --- Writing ------------------------------------------------------------------
#
# Both writes below are transcriptions of kanban_db.py. They are transcriptions
# rather than imports because that module lives in the gateway image and this
# container does not have it; if that ever changes, delete these and import.


def _append_event(conn: sqlite3.Connection, task_id: str, kind: str, payload: Optional[dict]) -> None:
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        (task_id, kind, json.dumps(payload) if payload else None, _now()),
    )


def _add_comment(conn: sqlite3.Connection, task_id: str, author: str, body: str) -> None:
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (task_id, author, body, _now()),
    )


class DecisionConflict(Exception):
    """The task is not in the state the decision assumed."""


def approve(db_path: str, task_id: str, note: Optional[str] = None) -> dict:
    """Accept the work: ``blocked -> done``.

    Mirrors ``kanban_db.complete_task``. ``block_kind`` and
    ``block_recurrences`` are cleared here and only here — the recurrence
    counter deliberately survives an unblock so a block/unblock loop can still
    be detected, and a successful completion is the one event that resets it.
    """
    conn = _connect(db_path)
    try:
        with conn:
            row = conn.execute(
                "SELECT status, block_kind FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if not row:
                raise DecisionConflict(f"No task '{task_id}'.")
            if row["status"] != "blocked" or row["block_kind"] not in REVIEWABLE_BLOCK_KINDS:
                raise DecisionConflict(
                    f"Task '{task_id}' is {row['status']}, not blocked awaiting review. "
                    "It may have been decided in another window."
                )
            result = note.strip() if note and note.strip() else "Approved in review."
            now = _now()
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status            = 'done',
                       result            = ?,
                       completed_at      = ?,
                       claim_lock        = NULL,
                       claim_expires     = NULL,
                       worker_pid        = NULL,
                       current_run_id    = NULL,
                       block_kind        = NULL,
                       block_recurrences = 0,
                       consecutive_failures = 0,
                       last_failure_error   = NULL
                 WHERE id = ? AND status = 'blocked'
                """,
                (result, now, task_id),
            )
            if cur.rowcount != 1:
                raise DecisionConflict(f"Task '{task_id}' changed while being decided.")
            _add_comment(conn, task_id, "reviewer", f"Review approved. {result}")
            _append_event(conn, task_id, "review_approved", {"action": "approve_done", "note": note or None})
            _append_event(conn, task_id, "completed", {"by": "dashboard-review"})
        return {"task_id": task_id, "status": "done"}
    finally:
        conn.close()


def request_changes(db_path: str, task_id: str, note: str) -> dict:
    """Send it back: comment, then ``blocked -> ready`` (or ``todo``).

    Mirrors ``kanban_db.unblock_task``, including the two things that are easy
    to leave out and expensive to miss:

    * a dangling ``current_run_id`` is closed as ``reclaimed`` in the same
      transaction, so the runs invariant holds;
    * the parent gate is re-checked, because setting ``ready`` unconditionally
      hands the dispatcher a task whose parents have not finished.

    ``block_recurrences`` is untouched, for the reason given at its source.

    The comment is written *before* the status flips, inside the same
    transaction. The dispatcher can claim a ``ready`` task within the second, so
    the ordering is what guarantees the worker sees the changes it is being
    asked to make.
    """
    note = (note or "").strip()
    if not note:
        raise ValueError("A note is required when requesting changes.")
    conn = _connect(db_path)
    try:
        with conn:
            row = conn.execute(
                "SELECT status, block_kind, current_run_id FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if not row:
                raise DecisionConflict(f"No task '{task_id}'.")
            if row["status"] != "blocked" or row["block_kind"] not in REVIEWABLE_BLOCK_KINDS:
                raise DecisionConflict(
                    f"Task '{task_id}' is {row['status']}, not blocked awaiting review. "
                    "It may have been decided in another window."
                )
            now = _now()
            if row["current_run_id"]:
                conn.execute(
                    """
                    UPDATE task_runs
                       SET status = 'reclaimed', outcome = 'reclaimed',
                           summary = COALESCE(summary, 'invariant recovery on review unblock'),
                           ended_at = ?, claim_lock = NULL, claim_expires = NULL,
                           worker_pid = NULL
                     WHERE id = ? AND ended_at IS NULL
                    """,
                    (now, int(row["current_run_id"])),
                )
            _add_comment(conn, task_id, "reviewer", f"Changes requested in review:\n\n{note}")
            undone_parents = conn.execute(
                "SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
                "WHERE l.child_id = ? AND p.status != 'done' LIMIT 1",
                (task_id,),
            ).fetchone()
            new_status = "todo" if undone_parents else "ready"
            cur = conn.execute(
                "UPDATE tasks SET status = ?, current_run_id = NULL, "
                "consecutive_failures = 0, last_failure_error = NULL "
                "WHERE id = ? AND status = 'blocked'",
                (new_status, task_id),
            )
            if cur.rowcount != 1:
                raise DecisionConflict(f"Task '{task_id}' changed while being decided.")
            _append_event(
                conn, task_id, "review_changes_requested",
                {"action": "request_changes", "note": note, "status": new_status},
            )
            _append_event(
                conn, task_id, "unblocked",
                {"status": new_status} if new_status != "ready" else None,
            )
        return {"task_id": task_id, "status": new_status}
    finally:
        conn.close()
