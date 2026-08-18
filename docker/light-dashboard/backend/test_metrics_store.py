"""Tests for the system-wide metrics store.

Standard library only, matching test_adk_live.py — see its docstring for why.

Every test here defends one of the store's promises rather than a mechanism,
because the mechanisms (a view, a union, a cast) are free to change and the
promises are not:

  * a field a producer had not yet started writing reads as unknown, not zero
  * a cost nobody priced is not reported as free
  * an outcome nobody recorded is not reported as success
  * a run that was retried is counted once
  * a call that two producers both saw is counted once

The fixtures are deliberately hand-built rather than copied from the live host:
each one exists to put the store in a state that the real data does not
currently reach, which is the only way to test the failure the real data will
eventually produce.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest

try:
    import duckdb  # noqa: F401
    HAVE_DUCKDB = True
except ImportError:  # pragma: no cover - the image always has it
    HAVE_DUCKDB = False

from . import metrics_store as M


SESSIONS_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT, model TEXT,
    title TEXT, parent_session_id TEXT, started_at REAL NOT NULL, ended_at REAL,
    end_reason TEXT, message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
    pricing_version TEXT
)
"""

USAGE_DDL = """
CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL, model TEXT NOT NULL, billing_provider TEXT DEFAULT '',
    billing_base_url TEXT DEFAULT '', billing_mode TEXT DEFAULT '', task TEXT DEFAULT '',
    api_call_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0, reasoning_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0, actual_cost_usd REAL DEFAULT 0,
    cost_status TEXT, cost_source TEXT, first_seen REAL, last_seen REAL
)
"""

EXECUTIONS_DDL = """
CREATE TABLE executions (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
    process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER,
    status TEXT NOT NULL, claimed_at TEXT NOT NULL, started_at TEXT,
    finished_at TEXT, error TEXT
)
"""

NOW = 1786000000.0


class StoreCase(unittest.TestCase):
    """Builds a throwaway Hermes data directory and points the store at it."""

    def setUp(self):
        if not HAVE_DUCKDB:
            self.skipTest("duckdb not installed")
        self.root = tempfile.mkdtemp(prefix="metrics-test-")
        self.adk = os.path.join(self.root, "adk")
        os.makedirs(self.adk, exist_ok=True)

        self._saved = (M.DB_DIR, M.ADK_STATE_DIR, M.STORE_PATH)
        M.DB_DIR = self.root
        M.ADK_STATE_DIR = self.adk
        M.STORE_PATH = os.path.join(self.root, "metrics.duckdb")
        M.reset()

    def tearDown(self):
        M.reset()
        M.DB_DIR, M.ADK_STATE_DIR, M.STORE_PATH = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    # --- fixture builders ---

    def hermes(self, profile=None, sessions=(), usage=()):
        root = self.root if profile is None else os.path.join(self.root, "profiles", profile)
        os.makedirs(root, exist_ok=True)
        con = sqlite3.connect(os.path.join(root, "state.db"))
        con.execute(SESSIONS_DDL)
        con.execute(USAGE_DDL)
        for s in sessions:
            cols = ", ".join(s)
            marks = ", ".join("?" * len(s))
            con.execute(f"INSERT INTO sessions ({cols}) VALUES ({marks})", tuple(s.values()))
        for u in usage:
            cols = ", ".join(u)
            marks = ", ".join("?" * len(u))
            con.execute(
                f"INSERT INTO session_model_usage ({cols}) VALUES ({marks})", tuple(u.values())
            )
        con.commit()
        con.close()

    def cron(self, profile=None, rows=()):
        root = self.root if profile is None else os.path.join(self.root, "profiles", profile)
        os.makedirs(os.path.join(root, "cron"), exist_ok=True)
        con = sqlite3.connect(os.path.join(root, "cron", "executions.db"))
        con.execute(EXECUTIONS_DDL)
        for r in rows:
            cols = ", ".join(r)
            marks = ", ".join("?" * len(r))
            con.execute(f"INSERT INTO executions ({cols}) VALUES ({marks})", tuple(r.values()))
        con.commit()
        con.close()

    def traces(self, app, records, day="2026-08-06"):
        d = os.path.join(self.adk, "traces", app)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{day}.jsonl"), "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

    def gateway(self, rows, day="2026-08-06"):
        d = os.path.join(self.adk, "usage")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{day}.jsonl"), "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def q(self, sql):
        return M.connect().execute(sql).fetchall()


class ProfileDiscovery(StoreCase):
    def test_finds_default_and_nested_profiles(self):
        """A profile added later must not be silently omitted from cost.

        Hermes keeps the default profile at the root and the rest under
        profiles/; a store that only read one of those would under-report spend
        without ever failing, which is the worst way to be wrong about money.
        """
        self.hermes(sessions=[dict(id="a", source="cli", started_at=NOW)])
        self.hermes(profile="dev", sessions=[dict(id="b", source="kanban", started_at=NOW)])
        self.hermes(profile="worker", sessions=[dict(id="c", source="cron", started_at=NOW)])
        self.assertEqual([p[0] for p in M._profiles()], ["default", "dev", "worker"])
        self.assertEqual(self.q("SELECT COUNT(*) FROM fact_activity")[0][0], 3)

    def test_each_profile_reports_its_own_rows_and_only_its_own(self):
        """One profile's spend must never be attributed to another.

        Every profile is a separate sqlite database with identical table names.
        Reading two of them in one statement, where each branch also joins
        usage to sessions inside its own catalog, made DuckDB 1.5 resolve the
        second branch against the first catalog — so the second profile
        silently returned the first profile's rows.

        Nothing downstream could catch that: the counts look plausible, every
        column is populated, and the profile label is right because it comes
        from a literal. It would just move one profile's cost onto another and
        never say so. Hence one statement per profile, and hence this test.
        """
        self.hermes(
            sessions=[dict(id="d1", source="cli", started_at=NOW),
                      dict(id="d2", source="cli", started_at=NOW),
                      dict(id="d3", source="cli", started_at=NOW)],
            usage=[dict(session_id=f"d{i}", model="default-model",
                        input_tokens=100, last_seen=NOW) for i in (1, 2, 3)],
        )
        self.hermes(
            profile="worker",
            sessions=[dict(id="w1", source="kanban", started_at=NOW)],
            usage=[dict(session_id="w1", model="worker-model",
                        input_tokens=7, last_seen=NOW)],
        )
        rows = dict(self.q("""
            SELECT profile, SUM(input_tokens) FROM fact_llm_usage GROUP BY 1
        """))
        self.assertEqual(rows, {"default": 300, "worker": 7})
        # And the models stay with their own profile rather than being copied.
        models = dict(self.q("SELECT profile, string_agg(DISTINCT model, ',') "
                             "FROM fact_llm_usage GROUP BY 1"))
        self.assertEqual(models, {"default": "default-model", "worker": "worker-model"})

    def test_empty_host_yields_empty_views_not_errors(self):
        """With no producers at all the store answers zero, not 500.

        This is the state of a fresh deploy, and the tab has to render on it.
        """
        self.assertEqual(self.q("SELECT COUNT(*) FROM fact_activity")[0][0], 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM fact_llm_usage")[0][0], 0)
        self.assertEqual(M.cost_summary(30)["classes"], [])


class CostClasses(StoreCase):
    def setUp(self):
        super().setUp()
        self.hermes(
            sessions=[
                dict(id="s_sub", source="cli", started_at=NOW),
                dict(id="s_local", source="cli", started_at=NOW),
                dict(id="s_paid", source="cli", started_at=NOW),
                dict(id="s_free", source="cli", started_at=NOW),
            ],
            usage=[
                # Subscription: real usage, zero marginal cost.
                dict(session_id="s_sub", model="gpt-5.5", cost_status="included",
                     cost_source="none", input_tokens=100, last_seen=NOW),
                # Local model: nobody has a rate for it.
                dict(session_id="s_local", model="gemma4", cost_status="unknown",
                     cost_source="none", input_tokens=200, last_seen=NOW),
                # Metered: a published rate produced a real charge.
                dict(session_id="s_paid", model="gemini-3.6-flash", cost_status="ok",
                     cost_source="table", estimated_cost_usd=1.25,
                     input_tokens=300, last_seen=NOW),
                # Priced, and the price is zero. Distinct from unpriced.
                dict(session_id="s_free", model="local-hosted", cost_status="ok",
                     cost_source="table", estimated_cost_usd=0.0,
                     input_tokens=400, last_seen=NOW),
            ],
        )

    def _classes(self):
        return {r["cost_class"]: r for r in M.cost_summary(3650)["classes"]}

    def test_three_classes_are_reported_separately(self):
        classes = self._classes()
        self.assertEqual(set(classes), {"included", "unpriced", "metered"})

    def test_only_metered_carries_a_dollar_figure(self):
        """Spend is the metered class alone.

        The other two have token counts and no cost, so a caller cannot
        accidentally add subscription volume into a spend number.
        """
        classes = self._classes()
        self.assertEqual(classes["metered"]["cost_usd"], 1.25)
        self.assertIsNone(classes["included"]["cost_usd"])
        self.assertIsNone(classes["unpriced"]["cost_usd"])

    def test_priced_at_zero_is_not_the_same_as_unpriced(self):
        """$0.00 measured must not render like "no rate known".

        Both are zero on the wire; only `cost_source` distinguishes them, and
        this is the assertion that keeps that distinction from being optimised
        away by someone simplifying the CASE expression.
        """
        rows = dict(self.q("""
            SELECT model, cost_class FROM fact_llm_usage
            WHERE model IN ('local-hosted', 'gemma4')
        """))
        self.assertEqual(rows["local-hosted"], "metered")
        self.assertEqual(rows["gemma4"], "unpriced")

    def test_subscription_billing_mode_wins_over_an_unknown_cost_status(self):
        """The two columns answer different questions and can disagree.

        Hermes records `billing_mode='subscription_included'` with
        `cost_status='unknown'` for subscription traffic whose price was never
        worked out. Reading only the status filed that under `unpriced`, so the
        same model appeared twice on the page — once as subscription usage and
        once as though nobody had a rate for it.
        """
        self.hermes(
            profile="sub",
            sessions=[dict(id="x1", source="cli", started_at=NOW)],
            usage=[dict(session_id="x1", model="gpt-5.5",
                        billing_provider="openai-codex",
                        billing_mode="subscription_included",
                        cost_status="unknown", cost_source="none",
                        input_tokens=26765, last_seen=NOW)],
        )
        rows = self.q("""
            SELECT cost_class, COUNT(*) FROM fact_llm_usage
            WHERE profile = 'sub' GROUP BY 1
        """)
        self.assertEqual(rows, [("included", 1)])
        # And the model no longer straddles two classes, which is the symptom
        # that made this visible on the page in the first place.
        classes = {r[0] for r in self.q(
            "SELECT DISTINCT cost_class FROM fact_llm_usage WHERE model = 'gpt-5.5'")}
        self.assertEqual(classes, {"included"})

    def test_no_total_is_offered(self):
        """The API must not grow a grand total by accident."""
        summary = M.cost_summary(3650)
        self.assertNotIn("total", summary)
        self.assertNotIn("total_cost_usd", summary)


class OutcomeHonesty(StoreCase):
    def test_hermes_sessions_have_no_outcome(self):
        """`end_reason` is a disposition, not a verdict.

        'cli_close' and 'session_reset' say how a session stopped. Reporting
        them as anything other than unknown would invent a success rate for a
        population that has none.
        """
        self.hermes(sessions=[
            dict(id="a", source="cli", started_at=NOW, end_reason="cli_close"),
            dict(id="b", source="cron", started_at=NOW, end_reason="cron_complete"),
        ])
        self.assertEqual(
            self.q("SELECT COUNT(*) FROM fact_activity WHERE outcome IS NOT NULL")[0][0], 0)
        for row in M.activity_summary(3650):
            self.assertEqual(row["outcome_known"], 0)
            self.assertIsNone(row["succeeded"])
            self.assertIsNone(row["failed"])

    def test_adk_runs_do_have_an_outcome(self):
        self.traces("app.a", [
            dict(run_id="r1", app="app.a", started_at="2026-08-06T01:00:00+00:00",
                 status="ok", duration_ms=10),
            dict(run_id="r2", app="app.a", started_at="2026-08-06T02:00:00+00:00",
                 status="failed", duration_ms=10),
        ])
        summary = [r for r in M.activity_summary(3650) if r["kind"] == "workflow_run"][0]
        self.assertEqual(summary["outcome_known"], 2)
        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(summary["failed"], 1)


class TraceEvolution(StoreCase):
    def test_field_absent_before_instrumentation_reads_null_not_zero(self):
        """The store's central promise, expressed at the schema level.

        A v2 trace predates per-agent model capture. Its agent ran on *some*
        model; the trace just cannot say which. NULL is that statement. An empty
        string or a zero would be the store answering a question it was never
        told the answer to.
        """
        self.traces("app.a", [
            # v2: agents carry no model field at all.
            dict(run_id="old", app="app.a", started_at="2026-08-06T01:00:00+00:00",
                 status="ok", duration_ms=5, trace_version=2,
                 agents=[dict(name="stage_one", turns=2, function_calls=1)]),
            # v3: it does.
            dict(run_id="new", app="app.a", started_at="2026-08-06T03:00:00+00:00",
                 status="ok", duration_ms=5, trace_version=3,
                 agents=[dict(name="stage_one", turns=2, function_calls=1,
                              model="gemini-3.6-flash")]),
        ])
        rows = dict(self.q("SELECT activity_id, model FROM fact_run_agent"))
        self.assertIsNone(rows["old"])
        self.assertEqual(rows["new"], "gemini-3.6-flash")

    def test_column_missing_from_every_trace_does_not_break_the_view(self):
        """`trigger` arrives with a later producer version.

        Referencing a column that appears in no file at all is a hard binder
        error, not a NULL — so the store has to inspect before it projects. This
        is the regression test for that, and it fails loudly if someone writes
        the obvious `SELECT trigger` instead.
        """
        self.traces("app.a", [
            dict(run_id="r1", app="app.a", started_at="2026-08-06T01:00:00+00:00",
                 status="ok", duration_ms=5),
        ])
        self.assertEqual(self.q("SELECT trigger FROM fact_activity")[0][0], None)

    def test_retried_run_is_counted_once(self):
        """invoke_workflow writes a line per attempt and one on the failure path.

        The newest line describes how the run actually ended, so it wins and the
        run contributes a single activity — otherwise a flaky pipeline would
        look busier and more successful than a reliable one.
        """
        self.traces("app.a", [
            dict(run_id="same", app="app.a", started_at="2026-08-06T01:00:00+00:00",
                 status="failed", attempt=1, duration_ms=5),
            dict(run_id="same", app="app.a", started_at="2026-08-06T01:00:09+00:00",
                 status="ok", attempt=2, duration_ms=5),
        ])
        rows = self.q("SELECT activity_id, outcome FROM fact_activity")
        self.assertEqual(rows, [("same", "ok")])


class DoubleCounting(StoreCase):
    """The failure the gateway introduces, and the rule that prevents it."""

    def setUp(self):
        super().setUp()
        self.hermes(
            sessions=[dict(id="s1", source="cli", started_at=NOW)],
            usage=[dict(session_id="s1", model="gpt-5.5", cost_status="included",
                        input_tokens=1000, api_call_count=5, last_seen=NOW)],
        )

    def test_self_reported_usage_wins_over_the_gateway(self):
        """Hermes bills its own calls; the proxy also sees them.

        Without precedence, turning the gateway on would silently double every
        Hermes number overnight — the kind of regression that looks like growth.
        """
        self.gateway([dict(activity_id="s1", component="main", model="gpt-5.5",
                           input_tokens=1000, api_call_count=5,
                           observed_by="gateway", cost_status="included")])
        rows = self.q("SELECT observed_by, input_tokens FROM fact_llm_usage")
        self.assertEqual(rows, [("self", 1000)])

    def test_gateway_fills_the_gap_where_nothing_self_reports(self):
        """Graphiti reports nothing, so the proxy is its only record.

        Suppressing gateway rows outright would trade a double count for a
        blind spot, which is why the rule is per-activity rather than global.
        """
        self.gateway([dict(activity_id="graph_1", component="graphiti",
                           model="gemma4", input_tokens=77, observed_by="gateway",
                           cost_status="unknown")])
        rows = dict(self.q("SELECT activity_id, input_tokens FROM fact_llm_usage"))
        self.assertEqual(rows["graph_1"], 77)
        self.assertEqual(rows["s1"], 1000)


class TypedOutputs(StoreCase):
    """The vocabulary that makes nine pipelines comparable."""

    def setUp(self):
        super().setUp()
        self.traces("app.a", [
            dict(run_id="r1", app="app.a", started_at="2026-08-06T01:00:00+00:00",
                 status="ok", duration_ms=5, trace_version=3,
                 metrics=dict(touched={"email": 40}, produced={"draft_email": 2},
                              extra={"pages_discovered": 9})),
            dict(run_id="r2", app="app.a", started_at="2026-08-06T02:00:00+00:00",
                 status="ok", duration_ms=5, trace_version=3,
                 metrics=dict(touched={"email": 10, "contact": 3},
                              produced={"draft_email": 1, "auto_email": 4},
                              extra={})),
        ])

    def test_produced_kinds_sum_across_runs(self):
        rows = {r["kind"]: r["total"] for r in M.outputs(3650)["produced"]}
        self.assertEqual(rows["draft_email"], 3)
        self.assertEqual(rows["auto_email"], 4)

    def test_touched_and_produced_are_never_merged(self):
        """Input volume and side effects are different quantities.

        40 emails read and 2 drafts written is a ratio worth knowing; 42 is not
        a number that means anything.
        """
        out = M.outputs(3650)
        self.assertEqual({r["kind"] for r in out["touched"]}, {"email", "contact"})
        self.assertEqual({r["kind"] for r in out["produced"]}, {"draft_email", "auto_email"})

    def test_unattended_sends_are_surfaced_separately(self):
        """The one number that says how much went out with no human in the loop.

        If drafts and auto-sends were ever folded into a single "emails" metric
        this assertion is what fails, which is the entire reason it exists.
        """
        self.assertEqual(M.outputs(3650)["unattended_sends"], 4)

    def test_daily_series_are_per_day_per_kind(self):
        """What the outcome charts draw: one row per day per kind."""
        daily = {(r["day"].isoformat(), r["kind"]): r["total"]
                 for r in M.outputs(3650)["daily_produced"]}
        self.assertEqual(daily[("2026-08-06", "draft_email")], 3)
        self.assertEqual(daily[("2026-08-06", "auto_email")], 4)

    def test_a_day_with_no_run_is_absent_rather_than_zero(self):
        """A gap is not a zero.

        Zero-filling would assert that a pipeline ran and produced nothing on a
        day it may simply not have run at all — and the chart draws a break for
        the first and a point on the axis for the second.
        """
        self.traces("app.b", [dict(
            run_id="later", app="app.b", started_at="2026-08-09T01:00:00+00:00",
            status="ok", duration_ms=5, trace_version=3,
            metrics=dict(touched={}, produced={"draft_email": 1}, extra={}))])
        days = sorted({r["day"].isoformat()
                       for r in M.outputs(3650)["daily_produced"]})
        # 07 and 08 saw no runs, so they are simply not there.
        self.assertEqual(days, ["2026-08-06", "2026-08-09"])

    def test_extra_is_not_aggregated_into_the_shared_vocabulary(self):
        """`pages_discovered` is real but not comparable to anything.

        It stays out of both lists so nothing downstream can add a
        pipeline-specific counter to a fleet-wide one.
        """
        kinds = {r["kind"] for r in M.outputs(3650)["produced"]}
        kinds |= {r["kind"] for r in M.outputs(3650)["touched"]}
        self.assertNotIn("pages_discovered", kinds)


class AgentScorecard(StoreCase):
    def test_measured_and_claimed_stay_in_separate_columns(self):
        """A model's self-score must never move a measured number.

        The fixture is the case that matters: a stage that failed both its
        checkpoints while rating itself 0.9. The measured column has to keep
        saying 0.0.
        """
        self.traces("app.a", [
            dict(run_id="r1", app="app.a", started_at="2026-08-06T01:00:00+00:00",
                 status="ok", duration_ms=5, trace_version=3,
                 agents=[dict(name="draft", turns=3, function_calls=1, model="m1")],
                 self_assessment=dict(
                     score=0.0,
                     checkpoints=[dict(stage="draft", ok=False, detail=None),
                                  dict(stage="draft", ok=False, detail=None)],
                     self_reports=[dict(agent="draft", score=0.9,
                                        went_well="wrote it", could_improve="needed the calendar")],
                 )),
        ])
        row = [r for r in M.agent_scorecard("app.a", 3650) if r["agent"] == "draft"][0]
        self.assertEqual(row["checkpoint_pass_rate"], 0.0)
        self.assertEqual(row["self_score"], 0.9)
        self.assertEqual(row["checkpoints"], 2)
        self.assertEqual(row["self_scored_runs"], 1)
        self.assertEqual(row["could_improve"], "needed the calendar")

    def test_agent_that_declined_to_score_itself_has_no_score(self):
        """Absent is not zero, one level further down.

        A stage that offered no opinion is not a stage that rated itself badly.
        """
        self.traces("app.a", [
            dict(run_id="r1", app="app.a", started_at="2026-08-06T01:00:00+00:00",
                 status="ok", duration_ms=5, trace_version=3,
                 agents=[dict(name="fetch", turns=1, function_calls=2, model="m1")],
                 self_assessment=dict(score=1.0,
                                      checkpoints=[dict(stage="fetch", ok=True, detail=None)],
                                      self_reports=[])),
        ])
        row = [r for r in M.agent_scorecard("app.a", 3650) if r["agent"] == "fetch"][0]
        self.assertEqual(row["checkpoint_pass_rate"], 1.0)
        self.assertIsNone(row["self_score"])


class AdkUsageInTheLedger(StoreCase):
    """Workflow spend has to land in the same ledger as chat spend."""

    def _trace(self, run_id="r1", agents=None, **kw):
        rec = dict(run_id=run_id, app="app.a", status="ok", duration_ms=5,
                   trace_version=3, agents=agents or [], **kw)
        rec.setdefault("started_at", "2026-08-06T01:00:00+00:00")
        self.traces("app.a", [rec])

    def test_adk_agents_appear_in_the_usage_ledger(self):
        """Before this, fact_llm_usage was Hermes-only.

        Every workflow that ever ran was missing from the fleet's token volume,
        which the page reported as though it were the whole system.
        """
        self._trace(agents=[
            dict(name="draft", turns=2, function_calls=0, model="ollama_chat/gemma4:12b",
                 api_call_count=2, prompt_tokens=100, completion_tokens=20,
                 cache_read_tokens=80, reasoning_tokens=7),
        ])
        rows = self.q("""
            SELECT component, model, input_tokens, output_tokens,
                   cache_read_tokens, reasoning_tokens, api_call_count, profile
            FROM fact_llm_usage WHERE activity_id = 'r1'
        """)
        self.assertEqual(
            rows, [("draft", "ollama_chat/gemma4:12b", 100, 20, 80, 7, 2, "adk")])

    def test_provider_comes_from_the_model_prefix_or_stays_null(self):
        """LiteLLM encodes the provider in the name; nothing else does.

        Guessing a provider for a bare model name would put local inference
        under whichever vendor happened to be the default.
        """
        self._trace(run_id="r1", agents=[
            dict(name="a", turns=1, function_calls=0, model="ollama_chat/gemma4",
                 prompt_tokens=1),
        ])
        self.traces("app.b", [dict(
            run_id="r2", app="app.b", started_at="2026-08-06T02:00:00+00:00",
            status="ok", duration_ms=5, trace_version=3,
            agents=[dict(name="b", turns=1, function_calls=0,
                         model="gemini-3.6-flash", prompt_tokens=1)])])
        rows = dict(self.q("SELECT activity_id, billing_provider FROM fact_llm_usage"))
        self.assertEqual(rows["r1"], "ollama_chat")
        self.assertIsNone(rows["r2"])

    def test_stages_that_never_called_a_model_are_not_in_the_ledger(self):
        """Most pipeline stages are plain code that authors events.

        `emit_result` and `fetch_events` take turns without ever reaching an
        LLM. Listing them as model usage would invent API calls that never
        happened and make every pipeline look chattier than it is.
        """
        self._trace(agents=[
            dict(name="draft", turns=1, function_calls=0, model="m1", prompt_tokens=10),
            dict(name="emit_result", turns=1, function_calls=0),
        ])
        components = [r[0] for r in self.q(
            "SELECT component FROM fact_llm_usage WHERE activity_id = 'r1'")]
        self.assertEqual(components, ["draft"])
        # It is still an agent for utilization purposes — just not a spender.
        agents = [r[0] for r in self.q(
            "SELECT agent FROM fact_run_agent WHERE activity_id = 'r1'")]
        self.assertIn("emit_result", agents)

    def test_adk_usage_is_unpriced_not_free(self):
        """No rate exists for a local model, and none is invented.

        `unpriced` keeps these tokens visible as volume while keeping them out
        of any dollar figure — the distinction the whole cost model rests on.
        """
        self._trace(agents=[dict(name="a", turns=1, function_calls=0,
                                 model="ollama_chat/gemma4", prompt_tokens=10)])
        classes = {r["cost_class"]: r for r in M.cost_summary(3650)["classes"]}
        self.assertEqual(set(classes), {"unpriced"})
        self.assertIsNone(classes["unpriced"]["cost_usd"])

    def test_a_retried_run_does_not_double_count_its_tokens(self):
        """The bug that deduplicating only fact_activity left behind.

        A retried run wrote two trace lines. Activity counted it once, but its
        agents, checkpoints, produced items — and now its tokens — were counted
        once per attempt, so a flaky pipeline looked busier and more productive
        than a reliable one.
        """
        self.traces("app.a", [
            dict(run_id="same", app="app.a", started_at="2026-08-06T01:00:00+00:00",
                 status="failed", attempt=1, duration_ms=5, trace_version=3,
                 agents=[dict(name="draft", turns=1, function_calls=0,
                              model="m1", prompt_tokens=100)],
                 metrics=dict(touched={}, produced={"draft_email": 1}, extra={}),
                 self_assessment=dict(score=0.0, checkpoints=[
                     dict(stage="draft", ok=False, detail=None)], self_reports=[])),
            dict(run_id="same", app="app.a", started_at="2026-08-06T01:00:09+00:00",
                 status="ok", attempt=2, duration_ms=5, trace_version=3,
                 agents=[dict(name="draft", turns=1, function_calls=0,
                              model="m1", prompt_tokens=100)],
                 metrics=dict(touched={}, produced={"draft_email": 1}, extra={}),
                 self_assessment=dict(score=1.0, checkpoints=[
                     dict(stage="draft", ok=True, detail=None)], self_reports=[])),
        ])
        self.assertEqual(
            self.q("SELECT SUM(input_tokens) FROM fact_llm_usage")[0][0], 100)
        self.assertEqual(self.q("SELECT COUNT(*) FROM fact_run_agent")[0][0], 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM fact_run_checkpoint")[0][0], 1)
        self.assertEqual(
            [r["total"] for r in M.outputs(3650)["produced"]], [1])
        # And the surviving line is the one that says how the run ended.
        self.assertEqual(
            self.q("SELECT ok FROM fact_run_checkpoint")[0][0], True)

    def test_hermes_and_adk_usage_coexist_without_collision(self):
        self.hermes(
            sessions=[dict(id="s1", source="cli", started_at=NOW)],
            usage=[dict(session_id="s1", model="gpt-5.5", cost_status="included",
                        input_tokens=500, last_seen=NOW)],
        )
        self._trace(agents=[dict(name="a", turns=1, function_calls=0,
                                 model="ollama_chat/gemma4", prompt_tokens=100)])
        by_profile = dict(self.q("""
            SELECT profile, SUM(input_tokens) FROM fact_llm_usage GROUP BY 1
        """))
        self.assertEqual(by_profile, {"default": 500, "adk": 100})


class EvalResults(StoreCase):
    def evals(self, rows, day="2026-08-06"):
        d = os.path.join(self.adk, "eval-results")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{day}.jsonl"), "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def test_eval_cases_survive_and_aggregate(self):
        """The whole point: results that outlive the container.

        agents-cli writes per-case detail to an unmounted `artifacts/`, so a
        recreate erased it and "is this pipeline improving" could not be asked.
        """
        base = dict(at="2026-08-06T01:00:00+00:00", metric="schema_conformance",
                    threshold=1.0, eval_set="enrich_contact", app="enrich_contact")
        self.evals([
            dict(base, case_id="c1", score=1.0, passed=True, category="normal",
                 explanation="pass [normal]"),
            dict(base, case_id="c2", score=1.0, passed=True, category="thin",
                 explanation="pass [thin]"),
            dict(base, case_id="c3", score=0.0, passed=False, category="injection",
                 explanation="BEHAVIOUR FAIL [injection] - obeyed injected instruction"),
        ])
        out = M.evals(3650)
        self.assertEqual(out["sets"][0]["cases"], 3)
        self.assertEqual(out["sets"][0]["passed"], 2)
        self.assertEqual(out["sets"][0]["pass_rate"], 0.667)

    def test_failing_cases_carry_the_graders_reason(self):
        """A count is not actionable; the grader's sentence is.

        "12 failed" tells nobody what to fix. The explanation names the
        behaviour that broke, which is the thing a human acts on.
        """
        self.evals([dict(
            at="2026-08-06T01:00:00+00:00", metric="schema_conformance",
            eval_set="enrich_contact", app="enrich_contact", case_id="c3",
            score=0.0, threshold=1.0, passed=False, category="injection",
            explanation="BEHAVIOUR FAIL [injection] - obeyed injected instruction")])
        failing = M.evals(3650)["failing"]
        self.assertEqual(len(failing), 1)
        self.assertIn("injected", failing[0]["explanation"])

    def test_no_eval_files_is_empty_not_an_error(self):
        self.assertEqual(M.evals(3650)["sets"], [])


class Concurrency(StoreCase):
    def test_overlapping_queries_do_not_clobber_each_others_results(self):
        """The bug that only appeared once it was deployed.

        A DuckDB connection holds the result of the last statement run on it,
        FastAPI serves these endpoints from a threadpool, and the Metrics tab
        polls six routes at once — so without a lock spanning execute *and*
        fetch, one request's `COUNT(*)` comes back as None because another
        request's execute replaced it mid-flight. Single-threaded tests pass
        happily; this one runs the queries the way the server does.
        """
        import threading

        self.hermes(
            sessions=[dict(id=f"s{i}", source="cli", started_at=NOW) for i in range(20)],
            usage=[dict(session_id=f"s{i}", model="m1", input_tokens=10, last_seen=NOW)
                   for i in range(20)],
        )
        M.connect()

        errors: list = []
        results: list = []

        def hammer():
            try:
                for _ in range(15):
                    results.append(M.health()["counts"]["fact_llm_usage"])
                    results.append(len(M.cost_summary(3650)["classes"]))
                    results.append(len(M.by_model(3650)))
                    results.append(len(M.activity_summary(3650)))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"concurrent queries raised: {errors[:3]}")
        # Every count must be the real number, never None and never an error
        # string — a silently wrong answer is what this failure mode produces.
        self.assertTrue(all(r is not None for r in results))
        self.assertEqual(
            {r for r in results if isinstance(r, int) and r > 5}, {20},
            "row counts must be stable under concurrent access")


class EngineBudget(StoreCase):
    """The other bug that only appeared once it was deployed.

    DuckDB defaults `threads` to the host core count and then splits the memory
    budget between them. On a 16-core host that left each thread ~19 MiB of the
    320 MB pool, and operators that allocate in indivisible 32 MiB blocks threw
    before the first one was satisfied — so half the Metrics routes 503'd while
    the other half answered normally, on twenty-row results. Nothing about the
    data was large; only the divisor was wrong.
    """

    def test_connect_pins_the_thread_count(self):
        con = M.connect()
        threads = con.execute("SELECT current_setting('threads')").fetchone()[0]
        self.assertEqual(int(threads), M.THREADS)
        # Guards the real invariant rather than the literal: whatever THREADS is
        # set to, each thread must still be able to claim one 32 MiB block from
        # MEMORY_LIMIT. This is what a bare `SET threads = os.cpu_count()`
        # violates, and it fails here on any host big enough to trigger it.
        limit_mib = _as_mib(con.execute("SELECT current_setting('memory_limit')").fetchone()[0])
        self.assertGreaterEqual(
            limit_mib / M.THREADS, 32.0,
            f"{M.THREADS} threads sharing {limit_mib:.0f} MiB leaves "
            f"{limit_mib / M.THREADS:.0f} MiB each, under the 32 MiB block size")

    def test_every_route_answers_under_the_pinned_budget(self):
        """The widest window the UI offers, which is where this first broke."""
        self.hermes(
            sessions=[dict(id=f"s{i}", source="cli", started_at=NOW) for i in range(40)],
            usage=[dict(session_id=f"s{i}", model="m1", input_tokens=10, last_seen=NOW)
                   for i in range(40)],
        )
        for fn in (M.activity_summary, M.timeseries, M.outputs, M.cost_summary):
            with self.subTest(route=fn.__name__):
                fn(3650)  # must not raise OutOfMemoryError


def _as_mib(setting: str) -> float:
    """DuckDB reports memory_limit as a human string, e.g. '305.1 MiB'."""
    value, unit = setting.split()
    return float(value) * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}[unit]


class Reconciliation(StoreCase):
    def test_usage_totals_match_the_source_rows(self):
        """The store must not lose or duplicate a row on the way through.

        Cheap to assert and the first thing to check after any change to the
        union: a join that fans out is invisible in a rate but obvious here.
        """
        self.hermes(
            sessions=[dict(id="s1", source="cli", started_at=NOW),
                      dict(id="s2", source="cli", started_at=NOW)],
            usage=[
                dict(session_id="s1", model="m1", input_tokens=10, last_seen=NOW),
                dict(session_id="s1", model="m2", input_tokens=20, last_seen=NOW,
                     task="compression"),
                dict(session_id="s2", model="m1", input_tokens=30, last_seen=NOW),
            ],
        )
        rows, total = self.q("SELECT COUNT(*), SUM(input_tokens) FROM fact_llm_usage")[0]
        self.assertEqual((rows, total), (3, 60))

    def test_compression_stays_a_separate_component(self):
        """Overhead has to remain visible rather than folded into the work.

        Hermes bills compression against the session that triggered it; if that
        collapsed into the main turn, the cost of keeping context would become
        invisible at exactly the moment it started to matter.
        """
        self.hermes(
            sessions=[dict(id="s1", source="cli", started_at=NOW)],
            usage=[
                dict(session_id="s1", model="m1", input_tokens=10, last_seen=NOW, task=""),
                dict(session_id="s1", model="m1", input_tokens=20, last_seen=NOW,
                     task="compression"),
            ],
        )
        rows = dict(self.q("SELECT component, input_tokens FROM fact_llm_usage"))
        self.assertEqual(rows, {"main": 10, "compression": 20})


class Automations(StoreCase):
    def test_executions_and_sessions_are_separate_populations(self):
        """Most scheduled runs never open a model session.

        On the live host that ratio is 162 executions to 13 sessions, so adding
        the two would badly misstate both how often automations run and what
        they cost. They are kept on separate routes for that reason, and this
        pins the behaviour.
        """
        self.cron(rows=[
            dict(id="e1", job_id="job_a", source="builtin", process_id="p", pid=1,
                 status="completed", claimed_at="2026-08-06T01:00:00+00:00",
                 started_at="2026-08-06T01:00:00+00:00"),
            dict(id="e2", job_id="job_a", source="builtin", process_id="p", pid=1,
                 status="failed", claimed_at="2026-08-06T02:00:00+00:00",
                 started_at="2026-08-06T02:00:00+00:00"),
            dict(id="e3", job_id="job_a", source="builtin", process_id="p", pid=1,
                 status="completed", claimed_at="2026-08-06T03:00:00+00:00",
                 started_at="2026-08-06T03:00:00+00:00"),
        ])
        # Only one of the three executions ever opened a session.
        self.hermes(sessions=[
            dict(id="cron_job_a_20260806_010000", source="cron", started_at=NOW,
                 title="nightly · Aug 06 01:00"),
        ])
        self.assertEqual(
            self.q("SELECT COUNT(*) FROM fact_activity WHERE kind='automation_run'")[0][0], 1)
        self.assertEqual(sum(r["executions"] for r in M.automation_executions(3650)), 3)

    def test_job_id_is_recovered_from_the_session_id(self):
        """The only link between a session's cost and its schedule.

        Hermes encodes it as `cron_<job_id>_<date>_<time>` and stores it nowhere
        else, so if this parse breaks, automation cost silently detaches from
        the job that caused it.
        """
        self.hermes(sessions=[
            dict(id="cron_07eb48bf4646_20260806_014031", source="cron",
                 started_at=NOW, title="ping12b · Aug 06 01:40"),
        ])
        row = self.q("SELECT job_id, app FROM fact_activity WHERE kind='automation_run'")[0]
        self.assertEqual(row, ("07eb48bf4646", "ping12b"))


class AutomationRunHistory(StoreCase):
    """The per-execution view behind one automation's page.

    `automation_executions` above answers how much a job ran and how it ended.
    These defend the other half — which run, and what it said when it broke —
    because a count is where the question starts and the dashboard had no way
    to finish it.
    """

    def test_a_claimed_run_that_never_started_is_still_reported(self):
        """The execution most worth reading is the one with no start time.

        A job the scheduler claimed and then lost has a NULL `started_at`.
        Filtering or ordering on that column drops exactly the crash that
        needs explaining, so the window is judged on `claimed_at`.
        """
        self.hermes()
        self.cron(rows=[
            dict(id="e1", job_id="job_a", source="builtin", process_id="p", pid=1,
                 status="completed", claimed_at="2026-08-06T01:00:00+00:00",
                 started_at="2026-08-06T01:00:00+00:00",
                 finished_at="2026-08-06T01:00:30+00:00"),
            dict(id="e2", job_id="job_a", source="builtin", process_id="p", pid=1,
                 status="failed", claimed_at="2026-08-06T02:00:00+00:00",
                 started_at=None, finished_at=None, error=None),
        ])
        runs = M.automation_runs("job_a", 3650, 50)
        self.assertEqual([r["execution_id"] for r in runs], ["e2", "e1"])
        self.assertIsNone(runs[0]["started_at"])
        # No start and no finish is not a run that took zero milliseconds.
        self.assertIsNone(runs[0]["duration_ms"])
        self.assertEqual(runs[1]["duration_ms"], 30000)

    def test_the_error_text_survives_to_the_caller(self):
        """The whole point of the route: the string that ends the investigation."""
        self.hermes()
        self.cron(rows=[
            dict(id="e1", job_id="job_a", source="direct", process_id="p", pid=1,
                 status="failed", claimed_at="2026-08-06T01:00:00+00:00",
                 started_at="2026-08-06T01:00:00+00:00",
                 finished_at="2026-08-06T01:00:01+00:00",
                 error="Script not found: /opt/data/scripts/gone.py"),
        ])
        runs = M.automation_runs("job_a", 3650, 50)
        self.assertEqual(runs[0]["error"], "Script not found: /opt/data/scripts/gone.py")

    def test_runs_are_scoped_to_the_job_asked_for(self):
        self.hermes()
        self.cron(rows=[
            dict(id="e1", job_id="job_a", source="builtin", process_id="p", pid=1,
                 status="completed", claimed_at="2026-08-06T01:00:00+00:00"),
            dict(id="e2", job_id="job_b", source="builtin", process_id="p", pid=1,
                 status="failed", claimed_at="2026-08-06T02:00:00+00:00"),
        ])
        self.assertEqual([r["job_id"] for r in M.automation_runs("job_a", 3650, 50)], ["job_a"])

    def test_totals_count_the_window_not_the_capped_list(self):
        """"3 of 412 failed" and "3 of the last 25" are different claims.

        The run list is capped so the page stays readable; the tallies beside it
        must not inherit that cap, or the page quietly understates a job that
        has been failing for weeks.
        """
        rows = []
        for i in range(30):
            rows.append(dict(
                id=f"e{i}", job_id="job_a", source="builtin", process_id="p", pid=1,
                status="failed" if i % 2 else "completed",
                claimed_at=f"2026-08-06T{i // 2:02d}:{(i % 2) * 30:02d}:00+00:00",
            ))
        self.hermes()
        self.cron(rows=rows)
        self.assertEqual(len(M.automation_runs("job_a", 3650, 5)), 5)
        totals = M.automation_totals("job_a", 3650)
        self.assertEqual(totals["total"], 30)
        self.assertEqual(totals["by_status"], {"completed": 15, "failed": 15})

    def test_executions_from_every_profile_are_visible(self):
        """A job's runs follow the profile that ran them, not a fixed database.

        Hermes's cron store is per-profile, so a single-database query would
        report a moved job as having no history at all.
        """
        self.hermes()  # the default profile's state.db, so it is discovered
        self.cron(rows=[
            dict(id="e1", job_id="job_a", source="builtin", process_id="p", pid=1,
                 status="completed", claimed_at="2026-08-06T01:00:00+00:00"),
        ])
        self.hermes(profile="worker")
        self.cron(profile="worker", rows=[
            dict(id="e2", job_id="job_a", source="builtin", process_id="p", pid=1,
                 status="failed", claimed_at="2026-08-06T02:00:00+00:00"),
        ])
        runs = M.automation_runs("job_a", 3650, 50)
        self.assertEqual({r["profile"] for r in runs}, {"default", "worker"})


class MetricsRoutes(unittest.TestCase):
    """The system page has to survive being pasted into a message.

    A deep link is opened cold, which is precisely when the SPA shell route
    matters: without it /metrics/system 404s on refresh, and the "view system
    metrics" button produces a URL that only works if you never reload.
    """

    def setUp(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError:  # pragma: no cover
            self.skipTest("fastapi not installed")
        from . import main
        self.main = main
        self.client = TestClient(main.app)

    def test_metrics_sub_page_serves_the_shell(self):
        if not os.path.exists(self.main.FRONTEND_INDEX):
            self.skipTest("frontend not present outside the container")
        self.assertEqual(self.client.get("/metrics/system").status_code, 200)

    def test_the_route_is_registered_even_without_a_built_frontend(self):
        # Outside the container the shell file is absent and the handler 404s
        # on purpose — but it must be a *route*, not an unmatched path, or the
        # container would 404 too. This is the half that catches the real bug.
        paths = {getattr(r, "path", None) for r in self.main.app.routes}
        self.assertIn("/metrics/{view}", paths)


if __name__ == "__main__":
    unittest.main()
