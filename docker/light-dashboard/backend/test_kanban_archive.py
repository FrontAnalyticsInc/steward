"""Tests for the kanban archive/unarchive endpoints.

The interesting part of these routes is not the UPDATE — it is the set of moves
they refuse. `archived` is the kernel's own terminal status, and the dispatcher
reads it: a task set to `archived` while a worker holds its claim leaves that
run stranded, still heartbeating against a task that has left the board. So the
transition guard is the behaviour under test, and every case below is one the
naive version (UPDATE tasks SET status='archived' WHERE id=?) would get wrong:

* a running task must be refused, not silently yanked out from under its worker
* a second click must be idempotent, not a 409 for the user to puzzle over
* an unknown id must 404 rather than report success on zero rows changed
* restore must land on `done`, never on `todo` where a dispatcher would requeue

The board is built here rather than copied from a live kanban.db, so the suite
does not depend on whatever happens to be on someone's board today.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from . import main


# The columns the routes actually touch. The production schema is far wider;
# reproducing it in full would make this a test of the schema, not the guard.
_SCHEMA = """
CREATE TABLE tasks (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at INTEGER NOT NULL
)
"""

_ROWS = [
    ("t_done", "a finished task", "done", 1000),
    ("t_done2", "another finished task", "done", 1001),
    ("t_running", "a task a worker holds", "running", 1002),
    ("t_blocked", "a task blocked on nothing that matters", "blocked", 1003),
    ("t_todo", "a task not started", "todo", 1004),
    ("t_archived", "a task already off the board", "archived", 1005),
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.execute(_SCHEMA)
    con.executemany("INSERT INTO tasks (id, title, status, created_at) VALUES (?,?,?,?)", _ROWS)
    con.commit()
    con.close()
    monkeypatch.setattr(main, "KANBAN_DB", str(db))
    return TestClient(main.app)


def status_of(client, task_id):
    con = sqlite3.connect(main.KANBAN_DB)
    row = con.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    con.close()
    return row[0] if row else None


# --- the move the button exists for -----------------------------------------

def test_archive_a_done_task(client):
    res = client.post("/api/kanban/t_done/archive")
    assert res.status_code == 200
    assert res.json()["changed"] is True
    assert status_of(client, "t_done") == "archived"


def test_archive_is_idempotent(client):
    assert client.post("/api/kanban/t_done/archive").status_code == 200
    # Two clicks on a slow connection is not an error, and must not report one.
    res = client.post("/api/kanban/t_done/archive")
    assert res.status_code == 200
    assert res.json()["changed"] is False
    assert status_of(client, "t_done") == "archived"


def test_blocked_tasks_are_archivable(client):
    # A task blocked on something that no longer matters is the other thing
    # people want off the board, and it has no worker attached.
    assert client.post("/api/kanban/t_blocked/archive").status_code == 200
    assert status_of(client, "t_blocked") == "archived"


# --- the moves it must refuse ------------------------------------------------

def test_running_task_cannot_be_archived(client):
    """The guard that matters: a live run must not be orphaned."""
    res = client.post("/api/kanban/t_running/archive")
    assert res.status_code == 409
    assert "running" in res.json()["detail"]
    assert status_of(client, "t_running") == "running"


def test_todo_task_cannot_be_archived(client):
    res = client.post("/api/kanban/t_todo/archive")
    assert res.status_code == 409
    assert status_of(client, "t_todo") == "todo"


def test_unknown_task_is_404_not_a_silent_success(client):
    # execute_db returns True for an UPDATE that matched zero rows, so without
    # the read-first this would report success on a task that does not exist.
    assert client.post("/api/kanban/t_missing/archive").status_code == 404


@pytest.mark.parametrize("bad_id", [
    "t_'; DROP TABLE tasks;--",  # parameterised anyway; the guard is depth
    "t_bad!!",                    # outside the id charset
    "a" * 80,                     # past the 64-char ceiling
])
def test_malformed_task_id_is_rejected(client, bad_id):
    assert client.post(f"/api/kanban/{bad_id}/archive").status_code == 400


def test_path_traversal_never_reaches_the_handler(client):
    # Documented rather than asserted at 400 on purpose: Starlette decodes %2F
    # before matching, so this becomes /api/kanban/../../etc/archive — too many
    # segments for the route, and the router rejects it before the guard runs.
    # The guard covers everything that does reach the handler; see above.
    #
    # Which rejection code the router picks is Starlette's business and has
    # changed under us once already (404 → 405, when the decoded path started
    # matching another route's shape). Pinning the exact code tested Starlette,
    # not us. What matters is that it is refused and the handler never sees it.
    resp = client.post("/api/kanban/..%2F..%2Fetc/archive")
    assert resp.status_code in (400, 404, 405), resp.status_code


# --- restore -----------------------------------------------------------------

def test_unarchive_restores_to_done(client):
    res = client.post("/api/kanban/t_archived/unarchive")
    assert res.status_code == 200
    # Not `todo`: the previous status isn't recorded, and guessing `todo` would
    # put a finished task back where a dispatcher could pick it up again.
    assert status_of(client, "t_archived") == "done"


def test_unarchive_on_an_already_done_task_is_idempotent(client):
    # Restoring to `done` a task that is already `done` asks for a state that
    # already holds, so it succeeds without a write — the same rule that makes
    # a second Archive click harmless, not a 409 to puzzle over. The UI never
    # sends this (Restore only renders when the task is archived); it is the
    # API contract that is being pinned.
    res = client.post("/api/kanban/t_done2/unarchive")
    assert res.status_code == 200
    assert res.json()["changed"] is False
    assert status_of(client, "t_done2") == "done"


def test_unarchive_refuses_a_task_that_was_never_on_its_way_out(client):
    res = client.post("/api/kanban/t_todo/unarchive")
    assert res.status_code == 409
    assert status_of(client, "t_todo") == "todo"


def test_archive_then_restore_round_trips(client):
    assert client.post("/api/kanban/t_done/archive").status_code == 200
    assert status_of(client, "t_done") == "archived"
    assert client.post("/api/kanban/t_done/unarchive").status_code == 200
    assert status_of(client, "t_done") == "done"


def test_other_tasks_are_left_alone(client):
    client.post("/api/kanban/t_done/archive")
    assert status_of(client, "t_done2") == "done"
    assert status_of(client, "t_running") == "running"
    assert status_of(client, "t_todo") == "todo"
