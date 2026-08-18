"""Unit tests for the outbound-call log.

This module is what makes the dashboard's Integrations screen able to say
"working" rather than "we saw traffic", so the properties asserted here are the
ones that screen depends on: an outcome on every record, the calling pipeline
attributed correctly, no payload leaked into a file an unauthenticated
dashboard serves, and — above all — that a logging failure never propagates
into the pipeline it is observing.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from app import integration_log


@pytest.fixture(autouse=True)
def log_dir(tmp_path, monkeypatch):
    """Point the module at a scratch directory for every test."""
    target = tmp_path / "integration-calls"
    monkeypatch.setattr(integration_log, "LOG_DIR", str(target))
    return target


def _records(log_dir) -> list[dict]:
    out = []
    for name in sorted(os.listdir(log_dir)):
        with open(log_dir / name, encoding="utf-8") as fh:
            out.extend(json.loads(line) for line in fh if line.strip())
    return out


def test_records_both_outcomes(log_dir):
    """A failure is as much a record as a success.

    A log that only carried successes could not distinguish "working" from "has
    not run", which is the distinction the whole screen exists to make.
    """
    integration_log.record("gmail", "messages.list", ok=True, capability="read")
    integration_log.record(
        "gmail", "messages.batchModify", ok=False, capability="modify",
        error="HTTP 403: insufficient authentication scopes",
    )
    recs = _records(log_dir)
    assert [r["ok"] for r in recs] == [True, False]
    assert recs[1]["error"].startswith("HTTP 403")
    assert recs[0]["error"] is None
    # The directory did not exist when the first call was made.
    assert os.path.isdir(log_dir)


def test_consumer_scope_attributes_the_pipeline(log_dir):
    integration_log.record("gmail", "messages.list", ok=True)
    with integration_log.consumer_scope("gmail_inbox_triage", run_id="run-1"):
        integration_log.record("gmail", "messages.list", ok=True)
    integration_log.record("gmail", "messages.list", ok=True)

    consumers = [r["consumer"] for r in _records(log_dir)]
    assert consumers == ["workflows", "gmail_inbox_triage", "workflows"]
    assert _records(log_dir)[1]["run_id"] == "run-1"


def test_consumer_scope_restores_on_exception(log_dir):
    with pytest.raises(ValueError):
        with integration_log.consumer_scope("triage"):
            raise ValueError("boom")
    assert integration_log.current_consumer() == "workflows"


def test_scope_survives_the_thread_hop():
    """The stages set the scope, but the API client runs in a worker thread.

    `asyncio.to_thread` copies the context, and this pipeline's attribution
    depends on that being true — assert it rather than trusting it.
    """
    async def main():
        with integration_log.consumer_scope("gmail_inbox_triage"):
            return await asyncio.to_thread(integration_log.current_consumer)

    assert asyncio.run(main()) == "gmail_inbox_triage"


def test_error_is_trimmed_to_one_line(log_dir):
    integration_log.record(
        "gmail", "messages.get", ok=False,
        error="line one\n   line two\n" + "x" * 500,
    )
    error = _records(log_dir)[0]["error"]
    assert "\n" not in error
    assert error.startswith("line one line two")
    assert len(error) <= integration_log.MAX_ERROR_CHARS


def test_never_raises_when_the_log_cannot_be_written(monkeypatch):
    """A bookkeeping failure must not take a triage run down with it."""
    monkeypatch.setattr(integration_log, "LOG_DIR", "/proc/nonexistent/nope")
    integration_log.record("gmail", "messages.list", ok=True)  # must not raise


def test_records_no_payload(log_dir):
    """Only method names and trimmed errors — this file is served to the LAN."""
    with integration_log.consumer_scope("triage"):
        integration_log.record("gmail", "messages.get", ok=True, capability="read")
    assert set(_records(log_dir)[0]) == {
        "at", "source", "consumer", "capability", "operation", "ok", "error", "run_id",
    }
