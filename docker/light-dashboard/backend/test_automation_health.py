"""Tests for the automation-health digest.

Standard library only, matching test_metrics_store.py — see its docstring.

Each test defends a promise about how the number is *derived*, because the
derivations are where a health report goes quietly wrong. A wrong rate does not
look wrong; it looks like a healthy system, which is worse than no report:

  * a job nobody has run is not reported as a job that never fails
  * a job that has never once succeeded is visible before its alarm would fire
  * repair time counts the run where the work happened, not just the run that
    closed the card
  * a loop that has never filed anything has no close rate, not a 0% one
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from . import automation_health as AH


def _write_store(data_dir: str, jobs: list[dict], executions: list[tuple]) -> None:
    """Lay out one profile's cron store: jobs.json plus executions.db."""
    cron_dir = os.path.join(data_dir, "cron")
    os.makedirs(cron_dir, exist_ok=True)
    with open(os.path.join(cron_dir, "jobs.json"), "w", encoding="utf-8") as fh:
        json.dump({"jobs": jobs}, fh)

    conn = sqlite3.connect(os.path.join(cron_dir, "executions.db"))
    conn.execute(
        "CREATE TABLE executions (id INTEGER PRIMARY KEY, job_id TEXT, "
        "status TEXT, error TEXT, claimed_at TEXT, finished_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO executions (job_id, status, error, claimed_at, finished_at) "
        "VALUES (?, ?, ?, ?, ?)",
        executions,
    )
    conn.commit()
    conn.close()


def _write_kanban(path: str, tasks: list[tuple], runs: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, "
        "assignee TEXT, created_by TEXT, created_at INTEGER, completed_at INTEGER)"
    )
    conn.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, "
        "outcome TEXT, started_at INTEGER, ended_at INTEGER)"
    )
    conn.executemany(
        "INSERT INTO tasks (id, title, status, assignee, created_by, created_at, "
        "completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        tasks,
    )
    conn.executemany(
        "INSERT INTO task_runs (task_id, outcome, started_at, ended_at) "
        "VALUES (?, ?, ?, ?)",
        runs,
    )
    conn.commit()
    conn.close()


class TestJobMeasurement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def test_job_with_no_runs_has_no_failure_rate(self):
        """An unmeasured job must not read as a job with a perfect record."""
        _write_store(
            self.tmp,
            jobs=[{
                "id": "never_ran",
                "name": "Never ran",
                "enabled": True,
                "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                "created_at": "2026-08-01T00:00:00+00:00",
            }],
            executions=[],
        )
        report = AH.scan_jobs(self.tmp)[0]
        self.assertEqual(report["runs"], 0)
        self.assertIsNone(report["failure_rate"])  # NOT 0.0

    def test_never_succeeded_job_is_surfaced_immediately(self):
        """The watchdog's threshold scales with period; this must not.

        A monthly job broken from its first run sits under the staleness alarm
        for roughly three months. The digest is the thing that sees it on day
        one, so it gets its own list rather than being ranked by rate.
        """
        _write_store(
            self.tmp,
            jobs=[{
                "id": "monthly",
                "name": "Monthly random sum example",
                "enabled": True,
                "schedule": {"kind": "cron", "expr": "0 9 1 * *"},
                "created_at": "2026-08-01T00:00:00+00:00",
            }],
            executions=[(
                "monthly", "failed", "Script not found: /opt/data/scripts/x.py",
                "2026-08-01T09:00:00+00:00", "2026-08-01T09:00:01+00:00",
            )],
        )
        digest = AH.digest(self.tmp, os.path.join(self.tmp, "absent.db"))
        self.assertEqual(digest["totals"]["never_succeeded"], 1)
        entry = digest["never_succeeded"][0]
        self.assertEqual(entry["job_id"], "monthly")
        self.assertIn("Script not found", entry["last_error"])

    def test_unmeasured_jobs_are_counted_separately(self):
        _write_store(
            self.tmp,
            jobs=[
                {"id": "a", "name": "a", "enabled": True,
                 "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                 "created_at": "2026-08-01T00:00:00+00:00"},
                {"id": "b", "name": "b", "enabled": True,
                 "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                 "created_at": "2026-08-01T00:00:00+00:00"},
            ],
            executions=[(
                "a", "completed", None,
                "2026-08-02T09:00:00+00:00", "2026-08-02T09:00:05+00:00",
            )],
        )
        totals = AH.digest(self.tmp, os.path.join(self.tmp, "absent.db"))["totals"]
        self.assertEqual(totals["watchable"], 2)
        self.assertEqual(totals["unmeasured"], 1)

    def test_disabled_job_is_not_watchable(self):
        """A job that is off is not a job that is broken."""
        _write_store(
            self.tmp,
            jobs=[{"id": "off", "name": "off", "enabled": False,
                   "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                   "created_at": "2026-08-01T00:00:00+00:00"}],
            executions=[],
        )
        digest = AH.digest(self.tmp, os.path.join(self.tmp, "absent.db"))
        self.assertEqual(digest["totals"]["jobs"], 1)
        self.assertEqual(digest["totals"]["watchable"], 0)
        self.assertEqual(digest["never_succeeded"], [])


class TestSelfHealing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        _write_store(self.tmp, jobs=[], executions=[])
        self.kanban = os.path.join(self.tmp, "kanban.db")

    def test_repair_time_counts_the_run_that_did_the_work(self):
        """The closing run is often instantaneous; the work is in the one before.

        The common repair shape is: a run that investigates and blocks on
        something it cannot reach, then a second run that closes the card the
        moment the blocker clears. Measuring only completed runs reports that
        as a 0-second fix.
        """
        _write_kanban(
            self.kanban,
            tasks=[("t_1", "[cron] x has stopped succeeding", "done", "dev",
                    "cron_watchdog", 1000, 2000)],
            runs=[
                ("t_1", "blocked", 1000, 1900),    # where the work happened
                ("t_1", "completed", 1900, 1900),  # closes instantly
            ],
        )
        report = AH.self_heal_report(self.kanban)
        self.assertEqual(report["median_time_to_fix_seconds"], 900)

    def test_close_rate_is_unknown_when_nothing_was_filed(self):
        _write_kanban(self.kanban, tasks=[], runs=[])
        report = AH.self_heal_report(self.kanban)
        self.assertEqual(report["cards_filed"], 0)
        self.assertIsNone(report["close_rate"])  # NOT 0.0

    def test_only_watcher_filed_cards_are_measured(self):
        """A card a person or an agent opened by hand is delegation, not a loop
        healing itself, and averaging the two would flatter the loop."""
        _write_kanban(
            self.kanban,
            tasks=[
                ("t_auto", "auto", "done", "dev", "cron_watchdog", 1000, 2000),
                ("t_hand", "by hand", "ready", "dev", "worker", 1000, None),
            ],
            runs=[("t_auto", "completed", 1000, 1600)],
        )
        report = AH.self_heal_report(self.kanban)
        self.assertEqual(report["cards_filed"], 1)
        self.assertEqual(report["close_rate"], 1.0)
        self.assertEqual(report["open_cards"], [])

    def test_open_cards_are_listed(self):
        _write_kanban(
            self.kanban,
            tasks=[("t_open", "still broken", "ready", "dev",
                    "cron_watchdog", 1000, None)],
            runs=[],
        )
        report = AH.self_heal_report(self.kanban)
        self.assertEqual(report["cards_closed"], 0)
        self.assertEqual(report["close_rate"], 0.0)
        self.assertEqual(report["open_cards"][0]["id"], "t_open")
        self.assertIsNone(report["median_time_to_fix_seconds"])

    def test_missing_board_is_reported_not_raised(self):
        """The digest must not be the reason the dashboard 500s."""
        report = AH.self_heal_report(os.path.join(self.tmp, "nope.db"))
        self.assertFalse(report["available"])


if __name__ == "__main__":
    unittest.main()
