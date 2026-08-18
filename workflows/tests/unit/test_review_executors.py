"""What happens after a human approves, and what must never happen twice.

The executor is the first thing in this system that acts on the queue rather
than filling it, so these tests are mostly about restraint:

  - a send is attempted at most once, across races and restarts
  - a failure whose outcome is unknown is never marked retryable
  - the draft path and the send path build the same message

Everything runs against fake service objects. Nothing here reaches Google.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from app import approvals, review_executors, run_trace
from app.review_executors import ExecutionError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import run_review_executor as executor  # noqa: E402


# --- Fakes --------------------------------------------------------------------


class FakeDrafts:
    def __init__(self, recorder):
        self.recorder = recorder

    def create(self, userId=None, body=None):
        self.recorder.append(("drafts.create", body))

        class Req:
            def execute(inner):
                return {"id": "d1", "message": {"id": "m1", "threadId": "t1"}}

        return Req()


class FakeUsers:
    def __init__(self, recorder):
        self.recorder = recorder

    def drafts(self):
        return FakeDrafts(self.recorder)


class FakeService:
    def __init__(self):
        self.calls = []

    def users(self):
        return FakeUsers(self.calls)


class FakeMailer:
    def __init__(self, *, configured=True, blow_up=None):
        self._configured = configured
        self.blow_up = blow_up
        self.sends = []

    def configured(self):
        return self._configured

    def send(self, **kwargs):
        if self.blow_up:
            raise self.blow_up
        self.sends.append(kwargs)
        return "sent-1"


def _email_item(**over):
    item = approvals.email_draft(
        body="hello there",
        reason="because",
        recipient_address="them@example.com",
        subject="Re: a thing",
        message_id="parent-mid",
        thread_id="parent-tid",
        rfc_message_id="<parent@mail.example.com>",
    )
    item["execution"] = {"state": "queued", "executor": "create_draft", "attempts": 0}
    item.update(over)
    return item


# --- Handlers -----------------------------------------------------------------


class TestCreateDraft:
    def test_it_threads_under_the_parent_when_there_is_one(self):
        service = FakeService()
        out = review_executors.execute_create_draft(_email_item(), gmail_service=service)
        (_name, body), = service.calls
        assert body["message"]["threadId"] == "parent-tid"
        assert out["draft_id"] == "d1"

    def test_a_cold_draft_has_no_thread(self):
        service = FakeService()
        item = _email_item()
        item["in_reply_to"] = None
        review_executors.execute_create_draft(item, gmail_service=service)
        (_name, body), = service.calls
        assert "threadId" not in body["message"]

    def test_it_refuses_an_item_with_no_recipient(self):
        item = _email_item()
        item["recipient"] = {"name": None, "address": None, "org": None}
        with pytest.raises(ExecutionError) as exc:
            review_executors.execute_create_draft(item, gmail_service=FakeService())
        assert exc.value.retryable is False

    def test_the_reviewers_edit_is_what_gets_sent(self):
        service = FakeService()
        item = _email_item(edited_body="I changed my mind")
        review_executors.execute_create_draft(item, gmail_service=service)
        (_name, body), = service.calls
        import base64

        raw = base64.urlsafe_b64decode(body["message"]["raw"]).decode()
        assert "I changed my mind" in raw
        assert "hello there" not in raw


class TestSend:
    def test_it_refuses_when_no_credential_is_configured(self):
        item = _email_item()
        item["execution"]["executor"] = "send"
        with pytest.raises(ExecutionError) as exc:
            review_executors.execute_send(item, mailer=FakeMailer(configured=False))
        assert exc.value.kind == "capability_missing"
        assert exc.value.retryable is False

    def test_it_passes_the_parent_through_so_the_reply_threads(self):
        mailer = FakeMailer()
        item = _email_item()
        item["execution"]["executor"] = "send"
        out = review_executors.execute_send(item, mailer=mailer)
        # The header gets the RFC id, the API gets the Gmail id. Sending the
        # Gmail id as In-Reply-To names nothing outside our mailbox, so the
        # recipient's client sees an orphan and starts a new conversation —
        # invisible from our side, because Gmail threads on thread_id anyway.
        assert mailer.sends[0]["in_reply_to"] == "<parent@mail.example.com>"
        assert mailer.sends[0]["thread_id"] == "parent-tid"
        assert out["message_id"] == "sent-1"


class TestApplyLabels:
    def _filter_item(self, **rule_over):
        rule = {
            "add_label": "Check Later",
            "remove_from_inbox": True,
            "example_message_ids": ["m1", "m2"],
        }
        rule.update(rule_over)
        item = approvals.filter_proposal(
            name="n", rule=rule, rationale="r", example_message_ids=rule["example_message_ids"]
        )
        item["execution"] = {"state": "queued", "executor": "apply_labels", "attempts": 0}
        return item

    def test_it_ensures_the_label_then_files_the_examples(self, monkeypatch):
        from app import gmail_api

        seen = {}

        def fake_ensure_label(service, name):
            seen["label"] = name
            return "L1"

        def fake_batch_modify(service, ids, add=(), remove=()):
            seen.update(ids=list(ids), add=list(add), remove=list(remove))

        monkeypatch.setattr(gmail_api, "ensure_label", fake_ensure_label)
        monkeypatch.setattr(gmail_api, "batch_modify", fake_batch_modify)
        out = review_executors.execute_apply_labels(
            self._filter_item(), gmail_service=object()
        )
        assert seen["label"] == "Check Later"
        assert seen["ids"] == ["m1", "m2"]
        assert seen["add"] == ["L1"]
        assert seen["remove"] == ["INBOX"]
        assert out["labeled_message_ids"] == ["m1", "m2"]

    def test_no_examples_is_a_correct_outcome_not_a_failure(self):
        out = review_executors.execute_apply_labels(
            self._filter_item(example_message_ids=[]), gmail_service=object()
        )
        assert out["labeled_message_ids"] == []


class TestCreateFilter:
    def test_it_fails_loudly_rather_than_half_doing_it(self):
        with pytest.raises(ExecutionError) as exc:
            review_executors.execute_create_filter({})
        assert exc.value.kind == "capability_missing"
        assert exc.value.retryable is False


class TestRetryPolicy:
    def test_a_send_is_never_in_the_retryable_set(self):
        # The load-bearing assertion of this whole module. A duplicate draft is
        # a nuisance; a duplicate email to a client cannot be recalled.
        assert "send" not in review_executors.RETRYABLE
        assert "create_draft" in review_executors.RETRYABLE
        assert "apply_labels" in review_executors.RETRYABLE


# --- The poller ---------------------------------------------------------------


@pytest.fixture
def queue(tmp_path, monkeypatch):
    for name in ("APPROVED", "EXECUTING", "EXECUTED", "FAILED"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(executor, f"{name}_DIR", d)
    monkeypatch.setattr(run_trace, "STATE_DIR", str(tmp_path / "adk"))
    return tmp_path


def _traces(queue):
    """Every trace record the executor wrote, oldest first."""
    root = queue / "adk" / "traces" / executor.TRACE_APP
    if not root.is_dir():
        return []
    return [
        json.loads(line)
        for path in sorted(root.glob("*.jsonl"))
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def _place(queue, item, name="2026-08-07T00:00:00Z--abc12345.json"):
    (queue / "approved" / name).write_text(json.dumps(item))
    return name


class TestAtMostOnce:
    def test_a_successful_item_lands_in_executed(self, queue, monkeypatch):
        monkeypatch.setattr(review_executors, "run", lambda item, **kw: {"draft_id": "d1"})
        name = _place(queue, _email_item())
        assert executor.process_one(name) == "done"
        written = json.loads((queue / "executed" / name).read_text())
        assert written["execution"]["state"] == "done"
        assert written["execution"]["result"] == {"draft_id": "d1"}
        assert not (queue / "executing" / name).exists()

    def test_a_second_pass_does_not_run_it_again(self, queue, monkeypatch):
        runs = []
        monkeypatch.setattr(
            review_executors, "run", lambda item, **kw: runs.append(1) or {}
        )
        name = _place(queue, _email_item())
        executor.process_one(name)
        # approved/ is read-only in production, so the file is still there. The
        # presence of executed/<name> is what must stop the second run.
        executor.process_one(name)
        assert len(runs) == 1, "a restart must not re-send"

    def test_two_racing_executors_claim_it_once(self, queue):
        name = "2026-08-07T00:00:00Z--race0001.json"
        assert executor._claim(name) is True
        assert executor._claim(name) is False

    def test_a_failure_lands_in_failed_with_its_reason(self, queue, monkeypatch):
        def boom(item, **kw):
            raise ExecutionError("nope", kind="label_missing", retryable=True)

        monkeypatch.setattr(review_executors, "run", boom)
        name = _place(queue, _email_item())
        assert executor.process_one(name) == "failed"
        written = json.loads((queue / "failed" / name).read_text())
        assert written["execution"]["error"]["kind"] == "label_missing"
        assert written["execution"]["error"]["retryable"] is True

    def test_an_unexpected_send_failure_is_not_retryable(self, queue, monkeypatch):
        # The dangerous case: we do not know whether the mail left. Marking it
        # retryable would let a button turn one uncertain message into two.
        def boom(item, **kw):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(review_executors, "run", boom)
        item = _email_item()
        item["execution"]["executor"] = "send"
        name = _place(queue, item)
        executor.process_one(name)
        written = json.loads((queue / "failed" / name).read_text())
        assert written["execution"]["error"]["retryable"] is False

    def test_an_unexpected_draft_failure_is_retryable(self, queue, monkeypatch):
        def boom(item, **kw):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(review_executors, "run", boom)
        name = _place(queue, _email_item())
        executor.process_one(name)
        written = json.loads((queue / "failed" / name).read_text())
        assert written["execution"]["error"]["retryable"] is True

    def test_an_item_that_was_not_queued_is_left_alone(self, queue, monkeypatch):
        monkeypatch.setattr(review_executors, "run", lambda item, **kw: {})
        item = _email_item()
        item["execution"] = {"state": "not_applicable"}
        name = _place(queue, item)
        assert executor.process_one(name) is None


class TestStalled:
    def test_a_stale_claim_is_reported_and_never_retried(self, queue):
        name = "2026-08-07T00:00:00Z--stall001.json"
        item = _email_item()
        item["execution"] = {
            "state": "running",
            "executor": "send",
            "started_at": "2000-01-01T00:00:00Z",
        }
        (queue / "executing" / name).write_text(json.dumps(item))
        assert executor.sweep_stalled() == 1
        written = json.loads((queue / "failed" / name).read_text())
        assert written["execution"]["error"]["kind"] == "stalled"
        assert written["execution"]["error"]["retryable"] is False

    def test_a_fresh_claim_is_left_alone(self, queue):
        name = "2026-08-07T00:00:00Z--fresh001.json"
        item = _email_item()
        item["execution"] = {"state": "running", "started_at": executor._now()}
        (queue / "executing" / name).write_text(json.dumps(item))
        assert executor.sweep_stalled() == 0


class TestWrittenItemsAreReadable:
    def test_the_executor_writes_world_readable_items(self, queue, monkeypatch):
        # Same reason as the producer side: three containers, three uids. A
        # 0600 result is one the dashboard cannot show anyone.
        import stat

        monkeypatch.setattr(review_executors, "run", lambda item, **kw: {})
        name = _place(queue, _email_item())
        executor.process_one(name)
        mode = stat.S_IMODE(os.stat(queue / "executed" / name).st_mode)
        assert mode & stat.S_IRGRP
        assert mode & stat.S_IROTH


class TestMetricsAreRecorded:
    """The queue's output has to show up in the fleet's numbers.

    Until the executor wrote traces, mail that actually left this building was
    invisible to every total on the metrics screen, while the *intention* to
    write it was counted at queue time and never corrected. These tests pin the
    rule that fixes both halves: count the effect, where the effect happened,
    and only once it really happened.
    """

    def test_a_send_is_counted_as_an_approved_email(self, queue, monkeypatch):
        monkeypatch.setattr(
            review_executors, "run", lambda item, **kw: {"message_id": "m1"}
        )
        item = _email_item()
        item["execution"] = {"state": "queued", "executor": "send", "attempts": 0}
        executor.process_one(_place(queue, item))

        (trace,) = _traces(queue)
        assert trace["metrics"]["produced"] == {"approved_email": 1}
        assert trace["status"] == "ok"
        assert trace["app"] == executor.TRACE_APP

    def test_an_approved_send_is_never_counted_as_unattended(self, queue, monkeypatch):
        # auto_email means "nobody was looking". Every item here was read and
        # approved by a person, so counting one as auto_email would corrupt the
        # single number that says how much unsupervised sending happens.
        monkeypatch.setattr(
            review_executors, "run", lambda item, **kw: {"message_id": "m1"}
        )
        item = _email_item()
        item["execution"] = {"state": "queued", "executor": "send", "attempts": 0}
        executor.process_one(_place(queue, item))

        (trace,) = _traces(queue)
        assert "auto_email" not in trace["metrics"]["produced"]

    def test_a_created_draft_is_counted_as_a_draft_email(self, queue, monkeypatch):
        monkeypatch.setattr(
            review_executors, "run", lambda item, **kw: {"draft_id": "d1"}
        )
        executor.process_one(_place(queue, _email_item()))

        (trace,) = _traces(queue)
        assert trace["metrics"]["produced"] == {"draft_email": 1}

    def test_a_failed_send_produces_nothing(self, queue, monkeypatch):
        # The most important one here. A send that raised must never appear in
        # the totals as mail that went out.
        def boom(item, **kw):
            raise ExecutionError("nope", kind="transport", retryable=False)

        monkeypatch.setattr(review_executors, "run", boom)
        item = _email_item()
        item["execution"] = {"state": "queued", "executor": "send", "attempts": 0}
        executor.process_one(_place(queue, item))

        (trace,) = _traces(queue)
        assert trace["status"] == "failed"
        assert trace["metrics"]["produced"] == {}

    def test_a_stalled_claim_produces_nothing(self, queue):
        # A stall around a send may or may not have delivered. The count says
        # only what is known; the ambiguity is a human's to resolve.
        name = "2026-08-07T00:00:00Z--stall001.json"
        item = _email_item()
        item["execution"] = {
            "state": "running",
            "executor": "send",
            "started_at": "2026-08-07T00:00:00Z",
        }
        (queue / "executing" / name).write_text(json.dumps(item))
        assert executor.sweep_stalled() == 1

        (trace,) = _traces(queue)
        assert trace["status"] == "failed"
        assert trace["metrics"]["produced"] == {}

    def test_filing_mail_produces_nothing_but_counts_what_it_touched(
        self, queue, monkeypatch
    ):
        # Applying a label creates no artifact and sends nothing; it is real
        # work on real messages, which is what `touched` is for.
        monkeypatch.setattr(
            review_executors,
            "run",
            lambda item, **kw: {"labeled_message_ids": ["a", "b", "c"]},
        )
        item = _email_item()
        item["execution"] = {
            "state": "queued",
            "executor": "apply_labels",
            "attempts": 0,
        }
        executor.process_one(_place(queue, item))

        (trace,) = _traces(queue)
        assert trace["metrics"]["produced"] == {}
        assert trace["metrics"]["touched"] == {"email": 3}

    def test_a_retry_reuses_the_run_id_so_one_send_counts_once(
        self, queue, monkeypatch
    ):
        # The store keeps the newest record per run_id. Keying by the item means
        # a retried execution replaces its earlier record instead of adding a
        # second send to the totals.
        monkeypatch.setattr(
            review_executors, "run", lambda item, **kw: {"draft_id": "d1"}
        )
        original = _email_item()
        name = _place(queue, original)
        executor.process_one(name)

        # Retry, as the dashboard performs it: it is the only writer of
        # approved/, and it re-queues the SAME item there with the attempt
        # count carried forward. Replaying the file would not be a retry, and a
        # fresh item would not be the same item.
        retried = dict(original)
        retried["execution"] = {
            "state": "queued",
            "executor": "create_draft",
            "attempts": 1,
        }
        (queue / "approved" / name).write_text(json.dumps(retried))
        (queue / "executed" / name).unlink()
        executor.process_one(name)

        first, second = _traces(queue)
        assert first["run_id"] == second["run_id"]
        assert (first["attempt"], second["attempt"]) == (1, 2)

    def test_an_item_it_skips_leaves_no_trace(self, queue, monkeypatch):
        # A pass that found nothing to do did nothing. Recording it would bury
        # the real records and inflate the fleet's run count every ten seconds.
        monkeypatch.setattr(review_executors, "run", lambda item, **kw: {})
        item = _email_item()
        item["execution"] = {"state": "done", "executor": "send"}
        assert executor.process_one(_place(queue, item)) is None
        assert _traces(queue) == []

    def test_traces_are_world_readable(self, queue, monkeypatch):
        # The executor runs as root; the dashboard that reads these does not.
        import stat

        monkeypatch.setattr(
            review_executors, "run", lambda item, **kw: {"draft_id": "d1"}
        )
        executor.process_one(_place(queue, _email_item()))

        written = sorted(
            (queue / "adk" / "traces" / executor.TRACE_APP).glob("*.jsonl")
        )
        mode = stat.S_IMODE(os.stat(written[0]).st_mode)
        assert mode & stat.S_IRGRP
        assert mode & stat.S_IROTH

    def test_bookkeeping_never_breaks_the_work(self, queue, monkeypatch):
        # A trace that cannot be written must not take the executor down with
        # it, and must not stop the item reaching executed/.
        monkeypatch.setattr(
            review_executors, "run", lambda item, **kw: {"draft_id": "d1"}
        )
        monkeypatch.setattr(run_trace, "STATE_DIR", "/proc/nonexistent/nowhere")
        name = _place(queue, _email_item())
        assert executor.process_one(name) == "done"
        assert (queue / "executed" / name).exists()
