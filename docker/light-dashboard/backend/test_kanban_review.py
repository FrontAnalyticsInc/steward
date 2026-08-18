"""Tests for kanban `needs_input` blocks presented as review items.

The bug this path exists to fix was not a crash: a worker blocked a task saying
"needs human code review", and there was no human-reachable way to answer. So
the first thing under test is simply that such a task reaches the queue, with
the code attached — a review item that cannot show what is being reviewed is the
same dead end in a nicer font.

After that, three things fail badly enough to be worth pinning:

**The action gate.** Same rule as the file-backed items: the legal actions come
from the type table, never from the item. A worker that blocked its own task
must not be able to talk the queue into completing it in a way the table does
not allow.

**The state machine.** The writes are transcriptions of `kanban_db.py`, which
lives in another image and cannot be imported here. That makes drift silent, so
the invariants are asserted directly: an unblock re-gates on undone parents
(setting `ready` unconditionally hands the dispatcher a task whose parents have
not finished), leaves `block_recurrences` alone (it is what detects a
block/unblock loop), and closes a dangling run pointer.

**Path handling.** The file list comes from a comment written by an agent whose
input includes arbitrary repository text. It is a hint, and everything it names
is checked against the source mounts — including after symlink resolution,
because a link inside the mount pointing at /opt/data would otherwise read the
gateway's databases into a web page.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from . import kanban_review, main


# Only the columns these routes touch. The production schema is far wider;
# reproducing it would make this a test of the schema rather than the gate.
_SCHEMA = """
CREATE TABLE tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT,
    workspace_path       TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    result               TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    last_failure_error   TEXT,
    current_run_id       INTEGER,
    branch_name          TEXT,
    session_id           TEXT,
    block_kind           TEXT,
    block_recurrences    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL);
CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL, payload TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL, profile TEXT, status TEXT NOT NULL,
    claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER,
    started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT,
    summary TEXT, metadata TEXT, error TEXT
);
"""


def _task(con, task_id, status="blocked", block_kind="needs_input", **kw):
    con.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, created_at, started_at, "
        "workspace_path, block_kind, block_recurrences, current_run_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            kw.get("title", "Implement the thing"),
            kw.get("body", "Build it and register it."),
            kw.get("assignee", "dev"),
            status,
            1000,
            1000,
            kw.get("workspace_path", "/opt/workflows"),
            block_kind,
            kw.get("block_recurrences", 1),
            kw.get("current_run_id"),
        ),
    )


@pytest.fixture()
def board(tmp_path, monkeypatch):
    """A board with one task blocked on a human, and a source tree to review."""
    src = tmp_path / "workflows" / "app" / "agents" / "thing"
    src.mkdir(parents=True)
    (src / "agent.py").write_text("root_agent = 1\n", encoding="utf-8")

    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    _task(con, "t_review")
    con.execute(
        "INSERT INTO task_runs (id, task_id, profile, status, started_at, ended_at, outcome, summary) "
        "VALUES (88, 't_review', 'dev', 'blocked', 1000, 1200, 'blocked', "
        "'review-required: needs human code review before marking done.')"
    )
    con.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
        (
            "t_review",
            "dev",
            f"Changed files:\n- `{src / 'agent.py'}`\n- `{src / 'missing.py'}`\n"
            "Verification: 30 passed.",
            1100,
        ),
    )
    con.commit()
    con.close()

    monkeypatch.setattr(main, "KANBAN_DB", str(db))
    monkeypatch.setenv("KANBAN_REVIEW_SOURCE_ROOTS", str(tmp_path / "workflows"))
    return {"db": str(db), "src": src, "root": tmp_path / "workflows"}


@pytest.fixture()
def client(board, tmp_path, monkeypatch):
    # The approvals side has to exist: the queue route reads both stores, and
    # this suite is about the kanban half not disturbing the other one.
    root = tmp_path / "approvals"
    dirs = {}
    for state in ("pending", "approved", "rejected", "executing", "executed", "failed"):
        path = root / state
        path.mkdir(parents=True)
        dirs[state] = str(path)
        monkeypatch.setattr(main, f"APPROVAL_{state.upper()}_DIR", str(path))
    monkeypatch.setattr(main, "REVIEW_STATE_DIRS", list(dirs.items()))
    monkeypatch.setattr(main, "APPROVAL_ROOT", str(root))
    monkeypatch.setattr(main, "APPROVAL_HEALTH_FILE", str(root / "health.json"))
    # No live integrations lookup in a unit test.
    monkeypatch.setattr(main, "_with_producer_access", lambda item: item)
    return TestClient(main.app)


def _post(client, payload):
    """A decision, with the header the write routes require."""
    return client.post("/api/review/decision", json=payload, headers={"X-Review-Confirm": "1"})


def row(db, task_id):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    con.close()
    return out


# --- the item reaches a human, with the code --------------------------------

def test_blocked_task_appears_in_the_review_queue(client, board):
    pending = client.get("/api/review/queue").json()["pending"]
    ids = [i["id"] for i in pending]
    assert "t_review" in ids
    item = next(i for i in pending if i["id"] == "t_review")
    assert item["review_type"] == "code_review"
    assert item["review_type_label"] == "Code review"
    # The worker's own reason for stopping is the question being asked.
    assert "review-required" in item["summary"]


def test_the_queue_lists_the_files_without_reading_them(client, board):
    """The queue is polled; the item page is not.

    Reading every file of every blocked task on a timer puts megabytes on the
    wire to render a list that shows a count.
    """
    item = next(
        i for i in client.get("/api/review/queue").json()["pending"] if i["id"] == "t_review"
    )
    assert len(item["changed_files"]) == 2
    assert all("content" not in f for f in item["changed_files"])


def test_the_item_carries_the_code(client, board):
    item = client.get("/api/review/item/t_review").json()
    by_path = {f["path"]: f for f in item["changed_files"]}
    shown = by_path[str(board["src"] / "agent.py")]
    assert shown["content"] == "root_agent = 1\n"
    assert shown["language"] == "python"
    # A file named in the comment but missing on disk is still listed. The
    # reviewer needs to know it was claimed to have changed.
    missing = by_path[str(board["src"] / "missing.py")]
    assert missing["content"] is None
    assert "not found" in missing["unavailable"]


def test_only_needs_input_blocks_are_reviewable(client, board):
    """A dependency block has no decision to make, so it is not a review item."""
    con = sqlite3.connect(board["db"])
    _task(con, "t_dep", block_kind="dependency")
    con.commit()
    con.close()
    ids = [i["id"] for i in client.get("/api/review/queue").json()["pending"]]
    assert "t_dep" not in ids
    assert client.get("/api/review/item/t_dep").status_code == 404


def test_an_unreadable_board_does_not_take_the_queue_down(client, board, monkeypatch):
    monkeypatch.setattr(main, "KANBAN_DB", "/nonexistent/kanban.db")
    res = client.get("/api/review/queue")
    assert res.status_code == 200
    assert res.json()["pending"] == []


# --- approve ----------------------------------------------------------------

def test_approve_completes_the_task(client, board):
    res = _post(client, {"id": "t_review", "decision": "approve", "action": "approve_done"})
    assert res.status_code == 200, res.text
    assert res.json()["task_status"] == "done"
    after = row(board["db"], "t_review")
    assert after["status"] == "done"
    assert after["completed_at"]
    # Cleared on completion and only on completion.
    assert after["block_kind"] is None
    assert after["block_recurrences"] == 0


def test_approve_leaves_a_comment_and_an_event(client, board):
    _post(client, {"id": "t_review", "decision": "approve", "action": "approve_done"})
    con = sqlite3.connect(board["db"])
    authors = [r[0] for r in con.execute("SELECT author FROM task_comments WHERE task_id='t_review'")]
    kinds = [r[0] for r in con.execute("SELECT kind FROM task_events WHERE task_id='t_review'")]
    con.close()
    assert "reviewer" in authors
    assert "review_approved" in kinds


def test_a_decided_task_still_answers_at_its_url(client, board):
    """The reload after pressing the button must not 404."""
    _post(client, {"id": "t_review", "decision": "approve", "action": "approve_done"})
    item = client.get("/api/review/item/t_review").json()
    assert item["state"] != "pending"
    assert item["decision"] == "approve"


def test_deciding_twice_is_a_conflict_not_a_second_completion(client, board):
    assert _post(client, {"id": "t_review", "decision": "approve"}).status_code == 200
    res = _post(client, {"id": "t_review", "decision": "approve"})
    assert res.status_code == 409
    assert res.json()["code"] == "conflict"


# --- request changes ---------------------------------------------------------

def test_reject_returns_the_task_to_the_board_with_the_reason(client, board):
    res = _post(client, {
        "id": "t_review", "decision": "reject",
        "rejection_reason": "The state file is written before the errors are checked.",
    })
    assert res.status_code == 200, res.text
    after = row(board["db"], "t_review")
    assert after["status"] == "ready"
    con = sqlite3.connect(board["db"])
    body = con.execute(
        "SELECT body FROM task_comments WHERE task_id='t_review' AND author='reviewer'"
    ).fetchone()[0]
    con.close()
    # What the worker reads when it picks the task back up. Without it the task
    # returns to the board with no idea what to change.
    assert "state file is written" in body


def test_reject_without_a_reason_is_refused(client, board):
    res = _post(client, {"id": "t_review", "decision": "reject", "rejection_reason": "   "})
    assert res.status_code == 400
    assert row(board["db"], "t_review")["status"] == "blocked"


def test_the_recurrence_counter_survives_an_unblock(client, board):
    """It is what detects a block -> unblock -> same block loop."""
    _post(client, {"id": "t_review", "decision": "reject", "rejection_reason": "again"})
    assert row(board["db"], "t_review")["block_recurrences"] == 1


def test_unblock_re_gates_on_undone_parents(client, board):
    """`ready` with an unfinished parent hands the dispatcher a task too early."""
    con = sqlite3.connect(board["db"])
    _task(con, "t_parent", status="running", block_kind=None)
    con.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('t_parent', 't_review')")
    con.commit()
    con.close()
    _post(client, {"id": "t_review", "decision": "reject", "rejection_reason": "wait"})
    assert row(board["db"], "t_review")["status"] == "todo"


def test_a_dangling_run_pointer_is_closed_on_unblock(client, board):
    con = sqlite3.connect(board["db"])
    con.execute(
        "INSERT INTO task_runs (id, task_id, profile, status, started_at) "
        "VALUES (99, 't_review', 'dev', 'running', 1000)"
    )
    con.execute("UPDATE tasks SET current_run_id = 99 WHERE id = 't_review'")
    con.commit()
    con.close()
    _post(client, {"id": "t_review", "decision": "reject", "rejection_reason": "again"})
    con = sqlite3.connect(board["db"])
    con.row_factory = sqlite3.Row
    run = con.execute("SELECT * FROM task_runs WHERE id = 99").fetchone()
    con.close()
    assert run["status"] == "reclaimed"
    assert run["ended_at"]
    assert row(board["db"], "t_review")["current_run_id"] is None


# --- the action gate ---------------------------------------------------------

def test_an_action_from_another_type_is_refused(client, board):
    """`send` belongs to email drafts. A task must not be able to reach it."""
    res = _post(client, {"id": "t_review", "decision": "approve", "action": "send"})
    assert res.status_code == 422
    assert row(board["db"], "t_review")["status"] == "blocked"


def test_the_legal_actions_are_what_the_item_advertises(client, board):
    item = client.get("/api/review/item/t_review").json()
    assert [a["id"] for a in item["actions"]] == ["approve_done"]
    assert item["reject_action"]["requires_reason"] is True


def test_the_write_route_still_requires_the_confirm_header(client, board):
    res = client.post("/api/review/decision", json={"id": "t_review", "decision": "approve"})
    assert res.status_code == 403
    assert row(board["db"], "t_review")["status"] == "blocked"


# --- paths from an untrusted comment -----------------------------------------

def test_a_path_outside_the_source_mounts_is_not_read(board, tmp_path):
    secret = tmp_path / "secret.py"
    secret.write_text("token = 'hunter2'\n", encoding="utf-8")
    entry = kanban_review.read_source(str(secret), [str(board["root"])])
    assert entry["content"] is None
    assert "outside" in entry["unavailable"]


def test_a_symlink_out_of_the_mount_is_not_followed(board, tmp_path):
    secret = tmp_path / "secret.py"
    secret.write_text("token = 'hunter2'\n", encoding="utf-8")
    link = board["src"] / "innocent.py"
    link.symlink_to(secret)
    entry = kanban_review.read_source(str(link), [str(board["root"])])
    assert entry["content"] is None
    assert "outside" in entry["unavailable"]


def test_prose_is_not_mistaken_for_a_path():
    paths = kanban_review.extract_paths(
        "I ran the tests and everything passed. See /opt/workflows/app/x.py, and note "
        "that ratios like 1/2 and words like and/or are not files."
    )
    assert paths == ["/opt/workflows/app/x.py"]


def test_a_relative_path_is_resolved_against_the_workspace():
    """How workers actually write them, having run pytest from the workspace."""
    paths = kanban_review.extract_paths(
        "uv run pytest tests/unit/test_thing.py -q -> 30 passed", "/opt/workflows"
    )
    assert paths == ["/opt/workflows/tests/unit/test_thing.py"]


def test_a_relative_path_does_not_become_a_bogus_absolute_one():
    """The bug this pattern was written to have and then not have.

    Matching from the inner slash turns `tests/unit/test_x.py` into
    `/unit/test_x.py`: a path that does not exist, shown to the reviewer as a
    changed file that could not be read.
    """
    assert "/unit/test_x.py" not in kanban_review.extract_paths(
        "ran tests/unit/test_x.py", "/opt/workflows"
    )
    # With no workspace to resolve against, it is dropped rather than guessed.
    assert kanban_review.extract_paths("ran tests/unit/test_x.py") == []
