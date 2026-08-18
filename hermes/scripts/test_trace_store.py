"""Tests for the reader the scheduled review sits on.

The corpus this reads is the only durable account of what the fleet did, so the
failure mode that matters is not a crash — it is a report that looks calm while
the fleet is down. Each test below pins one way that could happen.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import trace_store as ts  # noqa: E402


def write(tmp_path, app, day, records):
    d = tmp_path / app
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{day}.jsonl").open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def run(app, started, status="ok", error=None, **kw):
    rec = {"app": app, "run_id": f"{app}:{started}", "started_at": started,
           "status": status, "error": error}
    rec.update(kw)
    return rec


# --- reading ----------------------------------------------------------------


def test_a_torn_append_does_not_cost_the_other_runs(tmp_path):
    """A run interrupted mid-write leaves half a line. Failing the whole read
    over it would blind the review at exactly the moment something crashed."""
    d = tmp_path / "app.a"
    d.mkdir()
    (d / "2026-08-11.jsonl").write_text(
        json.dumps(run("app.a", "2026-08-11T10:00:00+00:00")) + "\n"
        + '{"run_id": "torn", "star\n'
        + json.dumps(run("app.a", "2026-08-11T11:00:00+00:00")) + "\n"
    )
    got = ts.load_runs(days=3650, traces_dir=str(tmp_path))
    assert len(got) == 2


def test_runs_come_back_newest_first(tmp_path):
    write(tmp_path, "app.a", "2026-08-11", [
        run("app.a", "2026-08-11T01:00:00+00:00"),
        run("app.a", "2026-08-11T09:00:00+00:00"),
    ])
    got = ts.load_runs(days=3650, traces_dir=str(tmp_path))
    assert [r["started_at"][11:13] for r in got] == ["09", "01"]


def test_naming_an_app_overrides_the_exclusion(tmp_path):
    """Excluded by default, but asking for it by name is unambiguous."""
    app = "app.agents.intentional_failure_demo"
    write(tmp_path, app, "2026-08-11", [run(app, "2026-08-11T01:00:00+00:00")])
    assert ts.load_runs(days=3650, traces_dir=str(tmp_path)) == []
    assert len(ts.load_runs(app=app, days=3650, traces_dir=str(tmp_path))) == 1


# --- error clustering -------------------------------------------------------


def test_one_outbreak_reads_as_one_cluster():
    """The August outage produced 72 unique error strings because each carried
    its own session id. Uncollapsed, no count ever rises above 1 and the largest
    outage in the corpus is invisible next to a single flake."""
    runs = [
        run("app.a", f"2026-08-10T0{i}:00:00+00:00", status="failed",
            error=f"ServiceDownError: connection refused on POST "
                  f"/apps/app.agents.x/users/hermes-worker/sessions/s_{i}abc{i} [Errno 111]")
        for i in range(1, 8)
    ]
    clusters = ts.error_clusters(runs)
    assert len(clusters) == 1
    assert clusters[0]["count"] == 7


def test_distinct_faults_stay_distinct():
    clusters = ts.error_clusters([
        run("app.a", "2026-08-10T01:00:00+00:00", status="failed",
            error="ServiceDownError: connection refused"),
        run("app.a", "2026-08-10T02:00:00+00:00", status="failed",
            error="TransientError: timeout on POST /run"),
    ])
    assert len(clusters) == 2


def test_a_failure_with_no_error_string_is_still_clustered():
    """The infrastructure failures carried an error and no stages; workflow
    faults carry stages and no error. Dropping either half hides a class."""
    clusters = ts.error_clusters([
        run("app.a", "2026-08-10T01:00:00+00:00", status="failed",
            self_assessment={"failed_stages": ["write"]}),
    ])
    assert len(clusters) == 1
    assert "write" in clusters[0]["shape"]


# --- absence of success -----------------------------------------------------


def test_cadence_is_read_from_quiet_stretches_not_bursts():
    """A daily job that retried three times in five minutes has a median gap of
    minutes and a real period of a day. Keying staleness off the median calls it
    sick an hour after a healthy run."""
    starts = ["2026-08-08T09:00:00+00:00", "2026-08-08T09:02:00+00:00",
              "2026-08-08T09:04:00+00:00", "2026-08-09T09:00:00+00:00",
              "2026-08-10T09:00:00+00:00"]
    assert ts._cadence_hours(starts) > 20


def test_a_job_within_its_own_cadence_is_not_stale():
    runs = [run("app.a", "2026-08-11T09:00:00+00:00"),
            run("app.a", "2026-08-10T09:00:00+00:00")]
    assert ts.stale_apps(runs, missed_runs=3.0) == []


def test_a_partial_run_is_not_an_outage():
    """`partial` means the stages worked and the yield was short. Counting it
    as silence files healthy pipelines as dead ones."""
    runs = [run("app.a", "2026-08-11T09:00:00+00:00", status="partial"),
            run("app.a", "2026-08-10T09:00:00+00:00", status="partial")]
    assert ts.stale_apps(runs, missed_runs=3.0) == []


def test_an_app_that_never_succeeded_is_reported():
    runs = [run("app.a", "2026-08-11T09:00:00+00:00", status="failed"),
            run("app.a", "2026-08-10T09:00:00+00:00", status="failed")]
    stale, = ts.stale_apps(runs, missed_runs=3.0)
    assert stale["app"] == "app.a"
    assert stale["hours_since_success"] is None


def test_a_single_run_has_no_cadence_to_judge():
    """One run is not evidence of a schedule, so it is not called late."""
    assert ts.stale_apps([run("app.a", "2026-08-11T09:00:00+00:00", status="failed")]) == []


# --- claims vs measurements -------------------------------------------------


def test_dishonest_runs_reads_the_written_field():
    """The derivation belongs to invoke_workflow at write time. Recomputing it
    here would let the reader and the writer disagree about one run."""
    runs = [
        run("app.a", "2026-08-11T09:00:00+00:00", self_report_accurate=False,
            self_reported_status="ok", self_assessment={"score": 0.5, "failed_stages": ["w"]}),
        run("app.a", "2026-08-11T08:00:00+00:00", self_report_accurate=True),
        run("app.a", "2026-08-11T07:00:00+00:00"),  # pre-derivation record
    ]
    got = ts.dishonest_runs(runs)
    assert len(got) == 1
    assert got[0]["claimed"] == "ok"


def test_records_written_before_the_derivation_are_not_called_liars():
    assert ts.dishonest_runs([run("app.a", "2026-08-11T09:00:00+00:00")]) == []


# --- summary ----------------------------------------------------------------


def test_summary_survives_an_empty_corpus(tmp_path):
    """A review that crashes on a quiet week is a review that stops running."""
    s = ts.summary(days=7, traces_dir=str(tmp_path))
    assert s["total_runs"] == 0
    assert s["fail_rate"] is None
    assert s["stale_apps"] == []


def test_summary_counts_unrecorded_triggers_separately(tmp_path):
    """Runs predating the trigger fix carry None. Filing them under a real
    trigger would misreport how much of the fleet runs unattended."""
    write(tmp_path, "app.a", "2026-08-11", [
        run("app.a", "2026-08-11T09:00:00+00:00", trigger="cron"),
        run("app.a", "2026-08-11T08:00:00+00:00"),
    ])
    s = ts.summary(days=3650, traces_dir=str(tmp_path))
    assert s["trigger_counts"] == {"cron": 1, "unrecorded": 1}
