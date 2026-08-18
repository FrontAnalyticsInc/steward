"""Tests for /api/automations/<job id> — one automation, whole.

Standard library only, matching test_adk_live.py — see its docstring for why.

The route exists because the answer to "why did that fail" was split across
three pages and the errors were on none of them. What it has to keep promising:

  * a job's configuration here is the same object the list serves, never a
    second description of it that can drift
  * an execution's error text reaches the caller intact
  * an ADK trace is attached as evidence about a window, and a run with no
    trace says so rather than borrowing a neighbour's
  * an id no profile schedules is a 404, not an empty page

The ADK match is the part worth pinning hardest: nothing links a cron execution
to a trace by id, so the join is by time, and a join by time is exactly the kind
of thing that silently starts attaching the wrong run.
"""

from __future__ import annotations

import datetime
import os
import unittest

from . import main as M


def execution(started, finished=None, claimed=None):
    """A store row, in the shape `metrics_store.automation_runs` returns.

    Naive UTC datetimes, because that is what DuckDB hands back — the traces
    carry an offset, and reconciling the two is the behaviour under test.
    """
    parse = lambda s: datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S") if s else None
    return {
        "claimed_at": parse(claimed or started),
        "started_at": parse(started),
        "finished_at": parse(finished),
    }


def trace(started, run_id="r1"):
    return {"run_id": run_id, "started_at": started, "status": "ok"}


class AdkRunMatching(unittest.TestCase):
    def test_a_trace_inside_the_window_is_attached(self):
        ex = execution("2026-08-07T12:00:00", "2026-08-07T12:10:00")
        run = trace("2026-08-07T12:00:30+00:00")
        self.assertIs(M._adk_run_for(ex, [run]), run)

    def test_a_trace_from_another_run_is_not(self):
        """The failure this join must not have.

        A workflow that runs every ten minutes produces traces on both sides of
        any given execution. Attaching the neighbouring one would put a healthy
        run's numbers under a failed execution — a wrong answer that reads
        exactly like a right one.
        """
        ex = execution("2026-08-07T12:00:00", "2026-08-07T12:10:00")
        self.assertIsNone(M._adk_run_for(ex, [trace("2026-08-07T12:30:00+00:00")]))
        self.assertIsNone(M._adk_run_for(ex, [trace("2026-08-07T11:30:00+00:00")]))

    def test_startup_slack_is_allowed_on_both_ends(self):
        """The wrapper starts before it opens a trace, and writes after it exits.

        Both gaps are real on this host, so a strictly-inside test would drop
        the trace for a perfectly ordinary run.
        """
        ex = execution("2026-08-07T12:00:00", "2026-08-07T12:10:00")
        self.assertIsNotNone(M._adk_run_for(ex, [trace("2026-08-07T11:59:00+00:00")]))
        self.assertIsNotNone(M._adk_run_for(ex, [trace("2026-08-07T12:11:00+00:00")]))
        # Well outside it, though, is a different run.
        self.assertIsNone(M._adk_run_for(ex, [trace("2026-08-07T11:55:00+00:00")]))

    def test_an_execution_that_never_started_matches_on_claimed_at(self):
        """The crash cases have no start time, and are the ones worth reading."""
        ex = execution(None, None, claimed="2026-08-07T12:00:00")
        self.assertIsNotNone(M._adk_run_for(ex, [trace("2026-08-07T12:00:30+00:00")]))

    def test_no_timestamp_at_all_matches_nothing(self):
        self.assertIsNone(M._adk_run_for({}, [trace("2026-08-07T12:00:30+00:00")]))

    def test_an_unparseable_trace_timestamp_is_skipped_not_fatal(self):
        """A torn or hand-edited trace line must not take the page down."""
        ex = execution("2026-08-07T12:00:00", "2026-08-07T12:10:00")
        good = trace("2026-08-07T12:00:30+00:00", run_id="good")
        self.assertIs(M._adk_run_for(ex, [{"run_id": "bad", "started_at": "nonsense"}, good]), good)

    def test_the_earliest_candidate_wins(self):
        """Ambiguity is resolved deterministically rather than by list order.

        Two traces inside one window means a wrapper fired twice, which this
        cannot describe — so it picks the first and stays stable across polls
        instead of flapping between them.
        """
        ex = execution("2026-08-07T12:00:00", "2026-08-07T12:10:00")
        late = trace("2026-08-07T12:05:00+00:00", run_id="late")
        early = trace("2026-08-07T12:01:00+00:00", run_id="early")
        self.assertEqual(M._adk_run_for(ex, [late, early])["run_id"], "early")


class Route(unittest.TestCase):
    """The endpoint over HTTP, with its two data sources stubbed out.

    Through a TestClient rather than by calling the function, so the query
    defaults and the path are the ones FastAPI actually applies — calling it
    directly hands the `Query` objects themselves to the clamps.
    """

    JOB = {"id": "job_a", "name": "nightly", "agent": "default", "adk_app": None}

    def setUp(self):
        from fastapi.testclient import TestClient

        self.client = TestClient(M.app)
        self._jobs = M._enriched_cron_jobs
        self._runs = M.metrics_store.automation_runs
        self._totals = M.metrics_store.automation_totals
        M._enriched_cron_jobs = lambda: [dict(self.JOB)]
        M.metrics_store.automation_runs = lambda job_id, days, limit: []
        M.metrics_store.automation_totals = lambda job_id, days: {
            "total": 0, "by_status": {}, "last_at": None}

    def tearDown(self):
        M._enriched_cron_jobs = self._jobs
        M.metrics_store.automation_runs = self._runs
        M.metrics_store.automation_totals = self._totals

    def test_an_unknown_id_is_a_404(self):
        """A deleted job's executions outlive it, but a page of runs for
        something that no longer exists cannot say what it did."""
        self.assertEqual(self.client.get("/api/automations/nope").status_code, 404)

    def test_the_job_served_is_the_one_the_list_serves(self):
        """One renderer, one description. A field that means something
        different here than in the list is the bug this route replaced."""
        out = self.client.get("/api/automations/job_a").json()
        self.assertEqual(out["job"], self.JOB)

    def test_a_non_adk_job_gets_no_adk_run_key_at_all(self):
        """Absent and null are different claims.

        Null means "this job records traces and none covers this run"; absent
        means the question does not apply, and the page says nothing rather
        than explaining a missing trace nobody expected.
        """
        M.metrics_store.automation_runs = lambda job_id, days, limit: [
            {"execution_id": "e1", "status": "failed", "error": "boom"}]
        out = self.client.get("/api/automations/job_a").json()
        self.assertNotIn("adk_run", out["executions"][0])
        self.assertEqual(out["executions"][0]["error"], "boom")

    def test_the_window_and_limit_are_clamped(self):
        """A hand-edited URL must not ask the store for an unbounded scan."""
        seen = {}

        def spy(job_id, days, limit):
            seen["days"], seen["limit"] = days, limit
            return []

        M.metrics_store.automation_runs = spy
        self.client.get("/api/automations/job_a?days=0&limit=99999")
        self.assertEqual(seen, {"days": 1, "limit": 200})

    def test_the_spa_shell_is_served_for_the_page_itself(self):
        """/automations/<id> has to survive being pasted into a message and
        opened cold, which means the server answers it before the app exists."""
        # Same guard as test_metrics_store's shell test: FRONTEND_DIR is the
        # container path /app/frontend, so outside the image there is no
        # index.html to serve and this can only 404. CI runs the suite on a bare
        # runner, where that is expected rather than a regression.
        if not os.path.exists(M.FRONTEND_INDEX):
            self.skipTest("frontend not present outside the container")
        res = self.client.get("/automations/job_a")
        self.assertEqual(res.status_code, 200)
        self.assertIn("<!DOCTYPE html>", res.text[:200])


if __name__ == "__main__":
    unittest.main()
