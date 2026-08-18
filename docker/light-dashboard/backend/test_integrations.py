"""Tests for the grant model behind the Integrations screen.

Written against the standard library alone, from a time when the dashboard had
no test runner of its own. It has one now — `pytest` from docker/light-dashboard/
runs this file among the rest — and these stay stdlib because pytest collects a
TestCase unchanged, so rewriting them would buy nothing. `python3 -m unittest
backend.test_integrations` still works too.

What is asserted here is the screen's honesty, not its layout: that a call with
no recorded outcome never reads as working, that a source shows its worst
grant, that "never used" is not an alarm, and that a missing database or a
malformed config costs one row rather than the page.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest

from . import integrations as I


def call(at_offset, ok=None, consumer="cron", tool="messages.list", error=None):
    return {
        "source_key": "gmail", "consumer": consumer, "tool": tool,
        "at": time.time() - at_offset, "ok": ok, "error": error, "profile": None,
    }


class GrantStatus(unittest.TestCase):
    def test_no_calls_is_never(self):
        status, basis, _ = I._grant_status([], None)
        self.assertEqual((status, basis), ("never", I.BASIS_CONFIG))

    def test_outcome_absent_is_unverified_not_working(self):
        """The whole point: activity is not evidence of success."""
        status, basis, _ = I._grant_status([call(60)], None)
        self.assertEqual(status, "unverified")
        self.assertEqual(basis, I.BASIS_ACTIVITY)

    def test_last_call_succeeded_is_working(self):
        status, basis, _ = I._grant_status([call(60, ok=True)], None)
        self.assertEqual((status, basis), ("working", I.BASIS_USAGE))

    def test_last_call_failed_is_failed_and_carries_the_error(self):
        rows = [call(60, ok=False, error="HTTP 401"), call(7200, ok=True)]
        status, _, failure = I._grant_status(rows, None)
        self.assertEqual(status, "failed")
        self.assertEqual(failure["error"], "HTTP 401")

    def test_failure_outranks_a_recent_success(self):
        """Newest-first ordering decides; an older success does not heal it."""
        rows = [call(10, ok=False, error="HTTP 403"), call(20, ok=True)]
        self.assertEqual(I._grant_status(rows, None)[0], "failed")

    def test_overdue_success_is_stale(self):
        rows = [call(6 * 86400, ok=True)]
        self.assertEqual(I._grant_status(rows, 86400)[0], "stale")

    def test_stale_needs_a_declared_interval(self):
        """With no expectation, nothing can be overdue."""
        rows = [call(60 * 86400, ok=True)]
        self.assertEqual(I._grant_status(rows, None)[0], "working")

    def test_stale_falls_back_to_activity_when_no_outcome_is_logged(self):
        status, basis, _ = I._grant_status([call(6 * 86400)], 86400)
        self.assertEqual((status, basis), ("stale", I.BASIS_ACTIVITY))


class Rollup(unittest.TestCase):
    def _g(self, status):
        return {"status": status}

    def test_source_shows_its_worst_grant(self):
        self.assertEqual(
            I._rollup([self._g("working"), self._g("failed"), self._g("never")]),
            "failed",
        )

    def test_never_used_is_not_an_alarm(self):
        """A source with one working grant and one unused one is working."""
        self.assertEqual(I._rollup([self._g("working"), self._g("never")]), "working")

    def test_unverified_outranks_working(self):
        """A weaker claim must not be hidden behind a stronger one."""
        self.assertEqual(I._rollup([self._g("working"), self._g("unverified")]),
                         "unverified")


class Parsing(unittest.TestCase):
    def test_mcp_tool_names(self):
        self.assertEqual(I._split_mcp_tool("mcp__gmail__send_email"),
                         ("gmail", "send_email"))
        # Built-in tools live in the same table and are not integrations.
        self.assertIsNone(I._split_mcp_tool("read_file"))
        self.assertIsNone(I._split_mcp_tool("mcp__gmail"))
        self.assertIsNone(I._split_mcp_tool(""))

    def test_intervals(self):
        self.assertEqual(I._parse_interval("6h"), 21600)
        self.assertEqual(I._parse_interval("3d"), 259200)
        self.assertEqual(I._parse_interval(900), 900)
        for bad in (None, "", "soon", "d", 0):
            self.assertIsNone(I._parse_interval(bad), bad)

    def test_capability_matching_uses_the_bare_tool_name(self):
        cfg = {"sources": {"gmail": {"capabilities": [
            {"name": "send", "match": ["send_*"]},
            {"name": "read", "match": "read_*"},
        ]}}}
        self.assertEqual(I._capability_for(cfg, "gmail", "send_email"), "send")
        self.assertEqual(I._capability_for(cfg, "gmail", "read_email"), "read")
        self.assertIsNone(I._capability_for(cfg, "gmail", "modify_email"))
        self.assertIsNone(I._capability_for({}, "gmail", "send_email"))


class DefensiveReads(unittest.TestCase):
    def test_missing_config_is_not_an_error(self):
        cfg = I.load_config("/nonexistent/integrations.json")
        self.assertEqual(cfg["grants"], [])
        self.assertIsNone(cfg.get("error"))

    def test_malformed_config_degrades_to_a_reported_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json")
            path = fh.name
        try:
            cfg = I.load_config(path)
            self.assertTrue(cfg["error"])
            self.assertEqual(cfg["grants"], [])
        finally:
            os.unlink(path)

    def test_missing_state_db_yields_no_calls(self):
        self.assertEqual(I.mcp_calls("/nonexistent/state.db"), [])

    def test_torn_call_log_line_does_not_discard_the_day(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "2026-08-06.jsonl"), "w") as fh:
                fh.write(json.dumps({"at": time.time(), "source": "gmail",
                                     "consumer": "triage", "ok": True}) + "\n")
                fh.write('{"at": 123, "source": "gmail"\n')  # crashed mid-append
                fh.write(json.dumps({"at": time.time(), "source": "graphiti",
                                     "consumer": "enrichment", "ok": False}) + "\n")
            rows = I.adk_calls(d)
        self.assertEqual(sorted(r["source_key"] for r in rows), ["gmail", "graphiti"])

    def test_call_log_entries_without_a_source_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "2026-08-06.jsonl"), "w") as fh:
                fh.write(json.dumps({"at": time.time(), "ok": True}) + "\n")
            self.assertEqual(I.adk_calls(d), [])


class EndToEnd(unittest.TestCase):
    """A whole payload, built from a throwaway session database."""

    def _state_db(self, path):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, profile_name TEXT)")
        conn.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT,
                        tool_name TEXT, timestamp REAL)""")
        conn.execute("INSERT INTO sessions VALUES ('s1', 'api_server', NULL)")
        now = time.time()
        for i, tool in enumerate(["mcp__gmail__send_email", "mcp__gmail__read_email",
                                  "read_file"]):
            conn.execute("INSERT INTO messages (session_id, tool_name, timestamp) VALUES (?,?,?)",
                         ("s1", tool, now - i))
        conn.commit()
        conn.close()

    def test_payload_groups_by_source_and_splits_capabilities(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "state.db")
            self._state_db(db)
            config = os.path.join(d, "integrations.json")
            with open(config, "w") as fh:
                json.dump({"sources": {"gmail": {"label": "Gmail", "capabilities": [
                    {"name": "send", "match": ["send_*"]},
                    {"name": "read", "match": ["read_*"]},
                ]}}}, fh)

            payload = I.build(agents=[], db_dir=d, src_dir=os.path.join(d, "nope"),
                              state_db=db, call_log_dir=os.path.join(d, "calls"),
                              config_path=config)

        self.assertEqual([s["key"] for s in payload["sources"]], ["gmail"])
        gmail = payload["sources"][0]
        self.assertEqual(gmail["label"], "Gmail")
        # One source, one consumer, two capabilities — and the built-in
        # read_file call is not an integration.
        self.assertEqual(
            sorted((g["consumer_label"], g["capability"]) for g in gmail["grants"]),
            [("chat", "read"), ("chat", "send")],
        )
        self.assertTrue(all(g["status"] == "unverified" for g in gmail["grants"]))
        ids = {g["id"] for g in payload["gaps"]}
        self.assertIn("mcp-outcomes", ids)
        self.assertIn("workflows-src", ids)

    def test_grants_for_consumer_inverts_the_grouping(self):
        payload = {"sources": [{
            "key": "gmail", "label": "Gmail", "grants": [
                {"consumer": "cron", "capability": "read", "status": "working",
                 "status_basis": "usage", "last_used_at": 1.0, "operations": [],
                 "last_error": None, "origins": ["mcp"]},
                {"consumer": "api_server", "capability": "send", "status": "failed",
                 "status_basis": "usage", "last_used_at": 2.0, "operations": [],
                 "last_error": "HTTP 401", "origins": ["mcp"]},
            ]}]}
        grants = I.grants_for_consumer(payload, "api_server")
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["capability"], "send")
        self.assertEqual(grants[0]["last_error"], "HTTP 401")

    def test_origin_filter_scopes_the_chat_sidebar_to_mcp(self):
        """Chat reaches the world over MCP; a workflow's client is not its to use."""
        payload = {"sources": [{
            "key": "gmail", "label": "Gmail", "grants": [
                {"consumer": "api_server", "capability": "read", "status": "working",
                 "status_basis": "usage", "last_used_at": 1.0, "operations": [],
                 "last_error": None, "origins": ["mcp"]},
                {"consumer": "api_server", "capability": "modify", "status": "working",
                 "status_basis": "usage", "last_used_at": 2.0, "operations": [],
                 "last_error": None, "origins": ["workflow"]},
            ]}]}
        mcp = I.grants_for_consumer(payload, "api_server", origin="mcp")
        self.assertEqual([g["capability"] for g in mcp], ["read"])
        # The review queue asks without a filter: what the agent could reach is
        # the whole answer, however it reached it.
        self.assertEqual(len(I.grants_for_consumer(payload, "api_server")), 2)

    def test_calls_are_tagged_with_where_the_evidence_came_from(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "2026-08-06.jsonl"), "w") as fh:
                fh.write(json.dumps({"at": time.time(), "source": "gmail",
                                     "consumer": "triage", "ok": True}) + "\n")
            self.assertEqual(I.adk_calls(d)[0]["origin"], "workflow")


class ModuleDiscovery(unittest.TestCase):
    """A new integration module must reach the screen without a code edit here."""

    @staticmethod
    def _src(d, name, body):
        with open(os.path.join(d, name), "w") as fh:
            fh.write(body)

    def test_a_module_that_logs_calls_is_found_without_being_listed(self):
        with tempfile.TemporaryDirectory() as d:
            self._src(d, "calendar_api.py",
                      '"""Deterministic Calendar access."""\n'
                      "from app import integration_log\n"
                      'X = os.environ.get("GCAL_SERVICE_ACCOUNT_FILE")\n')
            found = I.declared_adk_modules(d)
            self.assertIn("calendar", found)
            self.assertEqual(found["calendar"]["module"], "calendar_api")
            # Derived from the docstring and the source, not restated anywhere.
            self.assertEqual(found["calendar"]["summary"],
                             "Deterministic Calendar access.")
            self.assertIn("GCAL_SERVICE_ACCOUNT_FILE", found["calendar"]["env_keys"])

    def test_a_module_can_name_its_own_source_and_label(self):
        with tempfile.TemporaryDirectory() as d:
            self._src(d, "sheets_client.py",
                      '"""Sheets."""\n'
                      'INTEGRATION_SOURCE = "gsheets"\n'
                      'INTEGRATION_LABEL = "Google Sheets"\n')
            found = I.declared_adk_modules(d)
            self.assertIn("gsheets", found)
            self.assertEqual(found["gsheets"]["label"], "Google Sheets")

    def test_modules_that_reach_nothing_are_not_integrations(self):
        """The curated entries still list (as absent); nothing else is invented."""
        with tempfile.TemporaryDirectory() as d:
            self._src(d, "self_assessment.py", '"""Scoring helper."""\nY = 1\n')
            self._src(d, "integration_log.py", '"""The logger itself."""\n')
            found = I.declared_adk_modules(d)
            self.assertEqual(sorted(found), ["approvals", "gmail"])
            self.assertTrue(all(not e["present"] for e in found.values()))

    def test_curated_labels_still_win_for_modules_that_declare_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self._src(d, "approvals.py",
                      '"""Queue."""\nfrom app import integration_log\n')
            found = I.declared_adk_modules(d)
            self.assertEqual(found["approvals"]["label"], "Human approval queue")

    def test_a_curated_module_the_scan_cannot_see_is_still_listed(self):
        """A module that logs no calls is invisible to the scan, so the fallback
        registry is the only thing that knows it reaches anything."""
        with tempfile.TemporaryDirectory() as d:
            self._src(d, "approvals.py", '"""Writes to the queue."""\n')
            found = I.declared_adk_modules(d)
            self.assertIn("approvals", found)
            self.assertTrue(found["approvals"]["present"])

    def test_a_syntactically_broken_module_costs_one_row_not_the_scan(self):
        with tempfile.TemporaryDirectory() as d:
            self._src(d, "broken_api.py", "def (:\n")
            self._src(d, "calendar_api.py",
                      '"""Cal."""\nfrom app import integration_log\n')
            found = I.declared_adk_modules(d)
            self.assertIn("calendar", found)
            self.assertNotIn("broken", found)


if __name__ == "__main__":
    unittest.main()
