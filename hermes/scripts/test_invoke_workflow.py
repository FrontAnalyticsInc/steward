"""Tests for the two things that let a broken pipeline fail in silence.

Both regressions were observed in production on 2026-08-07, on the ten-minute
`worker-gmail-inbox-triage` job:

  * the run died with `[Errno 111] Connection refused` because the ADK service
    was hot-reloading an edited agent, and the retry ladder was shorter than a
    reload; and
  * the health task that failure filed was swallowed, because a `done` task
    from the previous outbreak still matched the idempotency key.

Together they produced the worst possible outcome: a job failing every slot and
a board with nothing on it.
"""

import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import invoke_workflow as iw  # noqa: E402


# --- connection-refused is not an ordinary transient fault -------------------


def test_refused_connection_is_service_down(monkeypatch):
    """ECONNREFUSED must be distinguishable from a 5xx or a timeout."""

    def boom(*_a, **_kw):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(iw.urllib.request, "urlopen", boom)
    with pytest.raises(iw.ServiceDownError):
        iw._request("GET", "/list-apps")


def test_other_transport_errors_stay_generic(monkeypatch):
    """A DNS or reset failure is still the ordinary retryable case."""

    def boom(*_a, **_kw):
        raise urllib.error.URLError("nodename nor servname provided")

    monkeypatch.setattr(iw.urllib.request, "urlopen", boom)
    with pytest.raises(iw.TransientError) as caught:
        iw._request("GET", "/list-apps")
    assert not isinstance(caught.value, iw.ServiceDownError)


def test_wait_for_service_returns_when_port_answers(monkeypatch):
    """The common case: a reload finishes and we stop waiting immediately."""
    calls = {"n": 0}

    def flaky(_method, _path, **_kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise iw.ServiceDownError("connection refused")
        return {"apps": []}

    monkeypatch.setattr(iw, "_request", flaky)
    monkeypatch.setattr(iw.time, "sleep", lambda _s: None)
    assert iw.wait_for_service(budget_s=60) is True
    assert calls["n"] == 3


def test_wait_for_service_gives_up_on_budget(monkeypatch):
    """A server that never comes back must not block a cron slot forever."""
    monkeypatch.setattr(
        iw, "_request", lambda *_a, **_kw: (_ for _ in ()).throw(iw.ServiceDownError("x"))
    )
    monkeypatch.setattr(iw.time, "sleep", lambda _s: None)
    assert iw.wait_for_service(budget_s=0) is False


def test_wait_for_service_treats_any_answer_as_up(monkeypatch):
    """A 500 means the process is running. Waiting on it would be pointless."""

    def five_hundred(*_a, **_kw):
        raise iw.TransientError("HTTP 500")

    monkeypatch.setattr(iw, "_request", five_hundred)
    assert iw.wait_for_service(budget_s=60) is True


def test_reload_window_does_not_burn_the_retry_budget(monkeypatch):
    """The regression itself: a reload must not consume the run's attempts.

    The server refuses twice while restarting, then serves the run. Before the
    fix those two refusals ate two of three attempts; now they are waits, and
    the run still gets its full budget.
    """
    seen = {"runs": 0}

    def run_once(*_a, **_kw):
        seen["runs"] += 1
        if seen["runs"] <= 2:
            raise iw.ServiceDownError("connection refused")
        return [{"content": {"parts": [{"text": '{"status": "ok"}'}]}}]

    monkeypatch.setattr(iw, "_run_once", run_once)
    monkeypatch.setattr(iw, "wait_for_service", lambda *_a, **_kw: True)
    monkeypatch.setattr(iw, "find_completed_trace", lambda *_a, **_kw: None)
    monkeypatch.setattr(iw, "extract_result", lambda _e: {"status": "ok"})
    monkeypatch.setattr(iw, "validate_result", lambda *_a, **_kw: [])
    monkeypatch.setattr(iw, "write_trace", lambda *_a, **_kw: None)

    result = iw.invoke_workflow("app.agents.x", {}, "run-1")
    assert result["status"] == "ok"
    assert seen["runs"] == 3


def test_permanently_dead_service_still_fails_the_run(monkeypatch):
    """Resilience is not denial: a server that stays down is a failed run."""
    monkeypatch.setattr(
        iw, "_run_once", lambda *_a, **_kw: (_ for _ in ()).throw(iw.ServiceDownError("x"))
    )
    monkeypatch.setattr(iw, "wait_for_service", lambda *_a, **_kw: False)
    monkeypatch.setattr(iw, "find_completed_trace", lambda *_a, **_kw: None)
    monkeypatch.setattr(iw, "write_trace", lambda *_a, **_kw: None)

    with pytest.raises(SystemExit):
        iw.invoke_workflow("app.agents.x", {}, "run-1")


# --- a closed task must not hide a recurrence --------------------------------


BAD = {
    "score": 0.0,
    "checkpoints_total": 1,
    "checkpoints_passed": 0,
    "checkpoints": [{"stage": "invocation", "ok": False, "detail": "refused"}],
    "failed_stages": ["invocation"],
    "errors": ["boom"],
    "self_reports": [],
}


class _Board:
    """Stands in for the `hermes kanban` CLI."""

    def __init__(self, tasks):
        self.tasks = tasks
        self.created = []

    def __call__(self, cmd, **_kw):
        if "list" in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.tasks), "")
        self.created.append(cmd)
        new_id = f"t_new{len(self.created)}"
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"id": new_id}), "")


def _title(app="app.agents.gmail_inbox_triage"):
    return iw.health_task_title(app, ["invocation"])


def test_open_task_suppresses_a_duplicate(monkeypatch):
    """A fault already on the board should not file 144 tasks a day."""
    board = _Board([{"id": "t_open", "title": _title(), "status": "ready"}])
    monkeypatch.setattr(iw.subprocess, "run", board)

    got = iw.file_health_task("app.agents.gmail_inbox_triage", "run-1", BAD)
    assert got == "t_open"
    assert board.created == []


@pytest.mark.parametrize("closed", ["done", "archived"])
def test_closed_task_does_not_suppress_a_recurrence(monkeypatch, closed):
    """The bug. A fixed-then-broken-again pipeline must reappear on the board."""
    board = _Board([{"id": "t_old", "title": _title(), "status": closed}])
    monkeypatch.setattr(iw.subprocess, "run", board)

    got = iw.file_health_task("app.agents.gmail_inbox_triage", "run-1", BAD)
    assert got == "t_new1", f"a {closed} task swallowed a live failure"
    assert len(board.created) == 1


def test_idempotency_key_is_run_scoped(monkeypatch):
    """The CLI's own dedup spans closed tasks, so the key must not be reused."""
    board = _Board([])
    monkeypatch.setattr(iw.subprocess, "run", board)

    iw.file_health_task("app.agents.gmail_inbox_triage", "run-abc", BAD)
    cmd = board.created[0]
    key = cmd[cmd.index("--idempotency-key") + 1]
    assert key.endswith(":run-abc")


def test_title_is_stable_across_differing_scores(monkeypatch):
    """Two reports of one fault must dedup even when the scores differ."""
    assert _title() == iw.health_task_title(
        "app.agents.gmail_inbox_triage", ["invocation"]
    )
    board = _Board([{"id": "t_open", "title": _title(), "status": "running"}])
    monkeypatch.setattr(iw.subprocess, "run", board)

    partial = dict(BAD, score=0.667)
    assert iw.file_health_task("app.agents.gmail_inbox_triage", "r", partial) == "t_open"


def test_unreachable_board_files_rather_than_stays_silent(monkeypatch):
    """If the board cannot be read, err toward visibility."""

    def cli(cmd, **_kw):
        if "list" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "board unreachable")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"id": "t_filed"}), "")

    monkeypatch.setattr(iw.subprocess, "run", cli)
    assert iw.file_health_task("app.agents.x", "r", BAD) == "t_filed"


def test_healthy_run_files_nothing(monkeypatch):
    board = _Board([])
    monkeypatch.setattr(iw.subprocess, "run", board)
    assert iw.file_health_task("app.agents.x", "r", dict(BAD, score=1.0)) is None
    assert board.created == []


def test_unmeasured_run_files_nothing(monkeypatch):
    """No checkpoints means unmeasured, which is not the same as failing."""
    board = _Board([])
    monkeypatch.setattr(iw.subprocess, "run", board)
    assert iw.file_health_task("app.agents.x", "r", dict(BAD, score=None)) is None
    assert board.created == []


# --- the daily cost cap ------------------------------------------------------
#
# The replacement for the per-key budgets the LiteLLM proxy would have enforced.
# It refuses BEFORE dispatch, and the two failure modes worth pinning down are
# opposite in direction: a cap that never fires spends without limit, and a cap
# that fires on its own plumbing failure takes the whole fleet down.


def _cost(**overrides):
    position = {"spent_today_usd": 0.0, "cap_usd": 10.0, "enabled": True,
                "over": False, "remaining_usd": 10.0}
    position.update(overrides)
    return position


def test_under_the_cap_proceeds(monkeypatch):
    monkeypatch.setattr(iw, "_request", lambda *_a, **_kw: _cost(spent_today_usd=1.5))
    assert iw.check_daily_cap()["spent_today_usd"] == 1.5


def test_over_the_cap_refuses(monkeypatch):
    monkeypatch.setattr(
        iw, "_request", lambda *_a, **_kw: _cost(spent_today_usd=12.0, over=True)
    )
    with pytest.raises(iw.DailyCapExceeded) as caught:
        iw.check_daily_cap()
    # The refusal has to say how to lift it, or it reads as an unexplained outage.
    assert "WORKFLOWS_DAILY_COST_CAP_USD" in str(caught.value)


def test_a_disabled_cap_never_refuses(monkeypatch):
    monkeypatch.setattr(
        iw, "_request",
        lambda *_a, **_kw: _cost(cap_usd=0, enabled=False, spent_today_usd=999.0,
                                 remaining_usd=None),
    )
    assert iw.check_daily_cap() is not None


@pytest.mark.parametrize(
    "failure",
    [iw.ServiceDownError("refused"), iw.TransientError("500"),
     iw.InvocationError("404 — an older service with no /cost route")],
)
def test_an_unanswerable_cost_check_does_not_block_the_run(monkeypatch, failure):
    """The cap guards runaway spend; it is not an authorization gate.

    Turning "the cost endpoint is unreachable" into "no workflow runs today"
    would be a worse outage than the one being prevented — and the 404 case is
    real, because a gateway running a newer invoker than the ADK image is the
    normal state during a rolling update.
    """

    def boom(*_a, **_kw):
        raise failure

    monkeypatch.setattr(iw, "_request", boom)
    assert iw.check_daily_cap() is None


def test_a_nonsense_answer_does_not_refuse(monkeypatch):
    """A proxy or error page returning something un-dict-shaped is not a cap hit."""
    monkeypatch.setattr(iw, "_request", lambda *_a, **_kw: "<html>502</html>")
    assert iw.check_daily_cap() == "<html>502</html>"


def test_an_idempotent_replay_is_never_refused(monkeypatch):
    """Replaying a completed run costs nothing, so the cap has no say in it.

    Ordering test: `check_daily_cap` must sit after the trace lookup. If it ran
    first, hitting the cap would make every already-completed run start failing
    retroactively, which is both wrong and alarming.
    """
    monkeypatch.setattr(
        iw, "find_completed_trace",
        lambda _app, _run: {"status": "ok", "output_summary": "done earlier"},
    )

    def refuse(*_a, **_kw):
        raise AssertionError("the cap was consulted for a replay")

    monkeypatch.setattr(iw, "check_daily_cap", refuse)
    got = iw.invoke_workflow("app.agents.x", {}, "run-1")
    assert got["idempotent_hit"] is True


def test_a_refusal_is_traced_and_exits(monkeypatch):
    """A refusal must land where every other run outcome lands.

    A cap hit that only appeared on cron stderr would look identical to the
    workflow having silently stopped being scheduled.
    """
    written = []
    monkeypatch.setattr(iw, "find_completed_trace", lambda *_a: None)
    monkeypatch.setattr(iw, "write_trace", lambda app, rec: written.append((app, rec)))
    monkeypatch.setattr(iw, "agent_py_sha", lambda _app: None)

    def refuse():
        raise iw.DailyCapExceeded("daily model spend cap reached: $12.00 of $10.00")

    monkeypatch.setattr(iw, "check_daily_cap", refuse)

    with pytest.raises(SystemExit) as caught:
        iw.invoke_workflow("app.agents.x", {}, "run-1", trigger="cron")

    assert "REFUSED" in str(caught.value)
    (app, record), = written
    assert app == "app.agents.x"
    assert record["status"] == "failed"
    # Zero, not None: nothing was dispatched, so the run genuinely spent nothing.
    # A None here would read as "unmeasured" and be excluded from utilization.
    assert record["attempt"] == 0
    assert record["total_tokens"] == 0
    assert record["trigger"] == "cron"
    assert "DailyCapExceeded" in record["error"]


# --- a claim is not a measurement -------------------------------------------
#
# `measured_passed`, `self_reported_status` and `self_report_accurate` were on
# the trace record from the start, read straight off the result payload. Nothing
# ever put them there — they are optional fields on a schema ADK does not
# enforce, so every model skipped all three. One run in 439 carried them. The
# field whose whole job is catching a workflow that claims success while failing
# was itself failing silently, which is the exact shape this file is about.
#
# The fix is to stop asking the subject to report its own measurement: the
# measured half is derived from the checkpoints, and only the claim comes from
# the payload.


def test_claiming_ok_while_stages_failed_is_caught():
    """The signal the whole field exists for."""
    got = iw.derive_honesty(
        {"status": "ok", "self_assessment": {"score": 0.5, "failed_stages": ["write"]}}
    )
    assert got["self_reported_status"] == "ok"
    assert got["measured_passed"] is False
    assert got["self_report_accurate"] is False


def test_an_honest_run_is_cleared():
    got = iw.derive_honesty({"status": "ok", "self_assessment": {"score": 1.0}})
    assert got["measured_passed"] is True
    assert got["self_report_accurate"] is True


def test_an_honest_failure_is_also_accurate():
    """Accuracy is agreement, not success. A run that says it failed and did
    is reporting correctly, and must not be filed as a liar."""
    got = iw.derive_honesty({"status": "failed", "self_assessment": {"score": 0.0}})
    assert got["measured_passed"] is False
    assert got["self_report_accurate"] is True


def test_unmeasured_is_not_judged():
    """No stage declared a checkpoint: unmeasured, not dishonest. Same rule
    `file_health_task` applies to a null score."""
    got = iw.derive_honesty({"status": "ok", "self_assessment": {"score": None}})
    assert got["measured_passed"] is None
    assert got["self_report_accurate"] is None


def test_partial_is_outside_what_checkpoints_can_adjudicate():
    """Checkpoints measure whether the stages worked, not how much the run
    yielded. A `partial` with every stage healthy — three of ten items
    extracted — is honest, and scoring it against them would file honest runs
    as dishonest ones."""
    got = iw.derive_honesty({"status": "partial", "self_assessment": {"score": 1.0}})
    assert got["measured_passed"] is True
    assert got["self_report_accurate"] is None


def test_an_explicit_self_report_beats_the_emitted_status():
    got = iw.derive_honesty(
        {"status": "ok", "self_reported_status": "failed",
         "self_assessment": {"score": 1.0}}
    )
    assert got["self_reported_status"] == "failed"
    assert got["self_report_accurate"] is False


def test_honesty_fields_reach_the_written_trace(monkeypatch):
    """Derivation is worth nothing if it does not land on the record the
    review reads."""
    written = []
    monkeypatch.setattr(iw, "find_completed_trace", lambda *_a: None)
    monkeypatch.setattr(iw, "write_trace", lambda app, rec: written.append(rec))
    monkeypatch.setattr(iw, "check_daily_cap", lambda: None)
    monkeypatch.setattr(iw, "app_source_dir", lambda _app: None)
    monkeypatch.setattr(iw, "agent_py_sha", lambda _app: None)
    monkeypatch.setattr(iw, "_run_once", lambda *_a: [{"content": {"parts": [
        {"functionResponse": {"name": "emit_result", "response": {
            "status": "ok", "items": [], "needs_review": [], "errors": [],
            "metrics": {"touched": 0, "produced": 0, "extra": {}},
            "self_assessment": {"score": 0.5, "failed_stages": ["write"]},
        }}}]}}])

    iw.invoke_workflow("app.agents.x", {}, "run-1", trigger="cron")

    record, = written
    assert record["trigger"] == "cron"
    assert record["self_reported_status"] == "ok"
    assert record["measured_passed"] is False
    assert record["self_report_accurate"] is False
