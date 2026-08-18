"""Tests for the cron staleness watchdog.

The bug it exists for: a job whose `script` path is wrong dies in the cron
runner before Python starts, so `invoke_workflow`'s health-task hook — the
thing that would normally file a card — never executes. The job fails every
slot against an empty board.

So the cases that matter most here are the ones no in-process reporter can
reach: a job that has never once succeeded, and a job that stopped running
entirely without recording a failure at all.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

from . import cron_watchdog as cw

HOUR = 3600
DAY = 86400
NOW = 1_800_000_000.0


def iso(epoch: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc
    ).isoformat()


def job(**kw):
    base = {
        "id": "abc123",
        "name": "worker-gmail-inbox-triage",
        "enabled": True,
        "state": "scheduled",
        "schedule": {"kind": "interval", "minutes": 10},
        "created_at": iso(NOW - 30 * DAY),
    }
    base.update(kw)
    return base


def execs(*specs):
    """`specs` are (status, seconds_ago, error) tuples, any order."""
    out = []
    for status, ago, error in specs:
        stamp = iso(NOW - ago)
        out.append(
            {"status": status, "error": error, "claimed_at": stamp,
             "finished_at": stamp}
        )
    return sorted(out, key=lambda r: r["claimed_at"], reverse=True)


# --- what counts as watchable ------------------------------------------------


@pytest.mark.parametrize("mutation", [
    {"enabled": False},
    {"paused_at": iso(NOW - DAY)},
    {"state": "completed"},
    {"schedule": {"kind": "once", "run_at": iso(NOW - DAY)}},
])
def test_jobs_that_are_off_are_not_broken(mutation):
    """A retired or one-shot job must not be reported forever."""
    assert cw.is_watchable(job(**mutation)) is False


def test_a_live_recurring_job_is_watchable():
    assert cw.is_watchable(job()) is True


# --- period inference --------------------------------------------------------


def test_interval_schedule_states_its_own_period():
    assert cw.expected_period_seconds(
        job(schedule={"kind": "interval", "minutes": 10}), []
    ) == 600


@pytest.mark.parametrize("expr,expected", [
    ("0 9 * * *", DAY),          # daily at a fixed time
    ("0 7 * * *", DAY),
    ("0 9 * * 1", 7 * DAY),      # weekly — day-of-week pins it
    ("0 0 1 * *", 30 * DAY),     # monthly
    ("*/5 * * * *", 300),        # sub-hourly — the minute field sets cadence
    ("* * * * *", 60),
    ("0 */4 * * *", 4 * HOUR),   # every four hours
])
def test_cron_period_is_read_from_the_expression(expr, expected):
    assert cw.cron_period_seconds(expr) == expected


@pytest.mark.parametrize("expr", ["", "nonsense", "0 9 * *", "a b c d e f"])
def test_unreadable_cron_expressions_fall_back(expr):
    assert cw.expected_period_seconds(
        job(schedule={"kind": "cron", "expr": expr}), []
    ) == cw.FALLBACK_PERIOD_SECONDS


def test_failure_bursts_do_not_shrink_a_daily_period():
    """The bug found against production data.

    `daily-random-contact-outreach` is scheduled `0 9 * * *`. Measuring the
    gaps between all executions gave 463s, because a job in a failure loop
    retries in bursts — so a daily job got a 23-minute alert threshold. The
    schedule is the authority; execution history is not.
    """
    burst = execs(*[("failed", i * 60, "Script not found") for i in range(1, 30)])
    period = cw.expected_period_seconds(
        job(schedule={"kind": "cron", "expr": "0 9 * * *"}), burst
    )
    assert period == DAY


def test_history_fallback_measures_only_successes():
    """Successes follow the schedule; failures follow whatever broke."""
    history = execs(
        ("completed", 0, None),
        ("failed", 60, "x"),
        ("failed", 120, "x"),
        ("completed", DAY, None),
        ("completed", 2 * DAY, None),
    )
    period = cw.expected_period_seconds(
        job(schedule={"kind": "unknown-kind"}), history
    )
    assert period == pytest.approx(DAY, rel=0.05)


# --- the healthy case --------------------------------------------------------


def test_a_recently_succeeding_job_is_silent():
    history = execs(("completed", 60, None), ("completed", 660, None))
    assert cw.evaluate_job("default", job(), history, NOW) is None


def test_one_failure_after_a_recent_success_is_not_an_alert():
    """A single miss is a restart or a blip. Crying on those gets you muted."""
    history = execs(("failed", 60, "boom"), ("completed", 660, None))
    assert cw.evaluate_job("default", job(), history, NOW) is None


# --- the cases an in-process reporter cannot reach ---------------------------


def test_a_job_that_never_succeeded_is_caught():
    """The exact shape of `Script not found`: broken from its first run.

    There is no success to be stale relative to, so the measurement runs from
    the job's creation instead. Without that, this case never trips.
    """
    history = execs(*[("failed", i * 600, "Script not found: /x.py")
                      for i in range(1, 20)])
    stale = cw.evaluate_job(
        "default", job(created_at=iso(NOW - 2 * HOUR)), history, NOW
    )
    assert stale is not None
    assert stale.last_success_at is None
    assert "Script not found" in stale.last_error


def test_a_job_that_stopped_running_entirely_is_caught():
    """No failures recorded at all — nothing ran. Failure-watching is blind here."""
    history = execs(("completed", 5 * HOUR, None))
    stale = cw.evaluate_job("default", job(), history, NOW)
    assert stale is not None
    assert stale.last_error is None
    assert stale.consecutive_failures == 0


def test_persistent_failure_is_caught_with_its_error():
    history = execs(
        *[("failed", i * 600, "TransientError: connection refused")
          for i in range(1, 12)],
        ("completed", 4 * HOUR, None),
    )
    stale = cw.evaluate_job("default", job(), history, NOW)
    assert stale is not None
    assert stale.consecutive_failures == 11
    assert "connection refused" in stale.last_error


def test_an_error_from_before_the_last_success_is_not_reported():
    """Found against production data.

    `daily-random-contact-outreach` failed with "Script not found" at 00:53,
    someone fixed it, and it succeeded at 00:54 and every run since. Scanning
    the whole history for the newest failure reported that resolved error as
    current, describing a healthy job as broken.
    """
    history = execs(
        ("completed", 60, None),
        ("failed", 600, "Script not found: /gone.py"),
    )
    assert cw.latest_error(history) is None


def test_an_error_after_the_last_success_is_reported():
    history = execs(
        ("failed", 60, "Script not found: /gone.py"),
        ("completed", 600, None),
        ("failed", 1200, "some older, already-fixed thing"),
    )
    assert cw.latest_error(history) == "Script not found: /gone.py"


def test_threshold_scales_with_cadence():
    """A daily job must not be called sick after thirty minutes."""
    daily = job(schedule={"kind": "cron", "expr": "0 7 * * *"})
    history = execs(
        ("completed", 2 * HOUR, None),
        ("completed", DAY + 2 * HOUR, None),
        ("completed", 2 * DAY + 2 * HOUR, None),
    )
    assert cw.evaluate_job("default", daily, history, NOW) is None


def test_min_staleness_protects_fast_jobs():
    """A one-minute job should not alert three minutes after a hiccup."""
    fast = job(schedule={"kind": "interval", "minutes": 1})
    history = execs(("completed", 5 * 60, None))
    assert cw.evaluate_job("default", fast, history, NOW) is None
    stale = cw.evaluate_job("default", fast, history, NOW + cw.MIN_STALENESS_SECONDS)
    assert stale is not None


# --- multi-profile discovery -------------------------------------------------


def test_scan_covers_every_profile(tmp_path):
    """Cron is per-profile; watching only `default` leaves the rest blind."""
    for profile, cron_dir in (
        ("default", tmp_path / "cron"),
        ("dev", tmp_path / "profiles" / "dev" / "cron"),
        ("worker", tmp_path / "profiles" / "worker" / "cron"),
    ):
        cron_dir.mkdir(parents=True)
        (cron_dir / "jobs.json").write_text(json.dumps(
            {"jobs": [job(id=f"job-{profile}", name=f"{profile}-job",
                          created_at=iso(NOW - 2 * HOUR))]}
        ))

    found = cw.scan(str(tmp_path), now=NOW)
    assert {j.profile for j in found} == {"default", "dev", "worker"}


def test_unreadable_store_is_not_fatal(tmp_path):
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text("{ not json")
    assert cw.scan(str(tmp_path), now=NOW) == []


# --- filing the card ---------------------------------------------------------


@pytest.fixture
def kanban(tmp_path):
    path = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT,
            assignee TEXT, status TEXT NOT NULL, priority INTEGER DEFAULT 0,
            created_by TEXT, created_at INTEGER NOT NULL,
            workspace_kind TEXT DEFAULT 'scratch', idempotency_key TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            run_id INTEGER, kind TEXT NOT NULL, payload TEXT,
            created_at INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    return str(path)


def _stale(**kw):
    base = dict(
        profile="default", job_id="abc123", name="worker-gmail-inbox-triage",
        period_seconds=600, threshold_seconds=1800, stale_for_seconds=7200,
        last_success_at=NOW - 7200, last_error="Script not found: /x.py",
        consecutive_failures=12,
    )
    base.update(kw)
    return cw.StaleJob(**base)


def _rows(kanban, query, args=()):
    conn = sqlite3.connect(kanban)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(query, args).fetchall()]
    finally:
        conn.close()


def test_card_is_filed_with_the_error_in_the_body(kanban):
    task_id = cw.file_card(kanban, _stale(), now=NOW)
    assert task_id
    row = _rows(kanban, "SELECT * FROM tasks")[0]
    assert row["status"] == "ready"
    assert "Script not found" in row["body"]
    assert "worker-gmail-inbox-triage" in row["title"]


def test_creation_event_is_recorded(kanban):
    task_id = cw.file_card(kanban, _stale(), now=NOW)
    events = _rows(kanban, "SELECT * FROM task_events WHERE task_id=?", (task_id,))
    assert [e["kind"] for e in events] == ["created"]
    assert json.loads(events[0]["payload"])["source"] == "cron_watchdog"


def test_an_open_card_suppresses_a_second(kanban):
    first = cw.file_card(kanban, _stale(), now=NOW)
    second = cw.file_card(kanban, _stale(stale_for_seconds=99999), now=NOW + 600)
    assert second == first
    assert len(_rows(kanban, "SELECT id FROM tasks")) == 1


@pytest.mark.parametrize("closed", ["done", "archived"])
def test_a_closed_card_does_not_suppress_a_recurrence(kanban, closed):
    """The bug this whole thread started from, guarded at the new call site."""
    first = cw.file_card(kanban, _stale(), now=NOW)
    conn = sqlite3.connect(kanban)
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (closed, first))
    conn.commit()
    conn.close()

    second = cw.file_card(kanban, _stale(), now=NOW + DAY)
    assert second != first
    assert len(_rows(kanban, "SELECT id FROM tasks")) == 2


def test_title_is_stable_across_worsening_conditions(kanban):
    """Dedup identity must not include numbers that move between checks."""
    a = _stale(stale_for_seconds=7200, consecutive_failures=12)
    b = _stale(stale_for_seconds=90000, consecutive_failures=300)
    assert a.title == b.title


def test_missing_database_is_not_fatal(tmp_path):
    assert cw.file_card(str(tmp_path / "nope.db"), _stale(), now=NOW) is None


def test_body_names_the_absent_scheduler_when_nothing_failed(kanban):
    cw.file_card(kanban, _stale(last_error=None, consecutive_failures=0), now=NOW)
    body = _rows(kanban, "SELECT body FROM tasks")[0]["body"]
    assert "not running at all" in body


def test_run_once_files_for_every_sick_job(tmp_path, kanban):
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text(json.dumps({"jobs": [
        job(id="a", name="job-a", created_at=iso(NOW - 5 * HOUR)),
        job(id="b", name="job-b", created_at=iso(NOW - 5 * HOUR)),
    ]}))

    results = cw.run_once(str(tmp_path), kanban, now=NOW)
    assert len(results) == 2
    assert all(task_id for _, task_id in results)
    assert len(_rows(kanban, "SELECT id FROM tasks")) == 2
