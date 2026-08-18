"""Tests for the review queue: the type model, the action gate, and the writes.

Three things are under test, in rising order of how badly they fail.

**Back-compatibility.** There are items in the live queue written months before
`review_type` existed, and several are ragged in ways that were invisible while
one hardcoded template rendered them: no `subject` key at all, `evidence: null`,
no `recipient`. Nothing on disk is migrated, so every one of those shapes has to
survive `normalize_item` — an item a human was asked to look at must never
disappear because the reviewer did not recognise it.

**The action gate.** Actions are derived from the type by the server, never read
from the item. The producer is the untrusted side of this boundary: its input is
arbitrary inbound mail. So the test that matters is that a client cannot ask for
an action the type does not have, whatever the item says.

**The writes.** Approving now creates drafts and sends mail, so the decision
route is no longer a file move. Two failures here were live bugs: an unvalidated
`decision` filed anything that was not exactly "approve" as *rejected* and
reported success, and every file written into `approved/` was mode 0600 — which
an executor running as a different uid cannot read.
"""

from __future__ import annotations

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from . import main
from . import review_types


# The shapes actually on disk. Ported from the standalone prototype's fixtures.py
# before that file was deleted, because these ragged items are the only coverage
# of the back-compat rule and they would otherwise have gone with it.
LEGACY_ITEMS = {
    "full": {
        "id": "a7f3c920",
        "created_at": "2026-08-05T14:30:00Z",
        "channel": "email",
        "recipient": {"name": "Dana", "address": "dana@example.com", "org": "Acme"},
        "subject": "Re: the thing",
        "body": "hello",
        "reason": "they asked",
        "evidence": {
            "source": "conference:sysdyn-2026",
            "conversation_notes": "met at the booth",
            "enrichment": "runs ops",
            "scores": {"product": 8, "consulting": 3},
        },
        "decision": None,
        "decided_at": None,
        "rejection_reason": None,
        "edited_body": None,
    },
    # No subject key at all.
    "no_subject": {
        "id": "f3b8a104",
        "created_at": "2026-08-03T09:00:00Z",
        "channel": "email",
        "recipient": {"address": "x@example.com"},
        "body": "no subject on this one",
        "reason": "reply",
        "evidence": {"source": "gmail_inbox_triage", "scores": {}},
        "decision": None,
    },
    # evidence is null, not absent.
    "null_evidence": {
        "id": "b2f4c110",
        "created_at": "2026-08-05T04:03:38Z",
        "channel": "email",
        "recipient": {"name": None, "address": "y@example.com", "org": None},
        "subject": "hi",
        "body": "b",
        "reason": "r",
        "evidence": None,
        "decision": None,
    },
    # No recipient, no producer — the oldest shape.
    "bare": {
        "id": "d4e2f831",
        "created_at": "2026-08-01T00:00:00Z",
        "channel": "email",
        "subject": "bare",
        "body": "b",
        "reason": "r",
        "decision": None,
    },
}


@pytest.fixture
def queue(tmp_path, monkeypatch):
    """A full six-directory queue, wired into the module under test."""
    dirs = {}
    for state in ("pending", "approved", "rejected", "executing", "executed", "failed"):
        d = tmp_path / state
        d.mkdir()
        dirs[state] = d
        monkeypatch.setattr(main, f"APPROVAL_{state.upper()}_DIR", str(d))
    monkeypatch.setattr(
        main, "REVIEW_STATE_DIRS", [(s, str(p)) for s, p in dirs.items()]
    )
    monkeypatch.setattr(main, "APPROVAL_ROOT", str(tmp_path))
    monkeypatch.setattr(main, "APPROVAL_HEALTH_FILE", str(tmp_path / "health.json"))
    # No live integrations lookup in a unit test.
    monkeypatch.setattr(main, "_with_producer_access", lambda item: item)
    return dirs


def place(queue, item, state="pending"):
    name = f"{item.get('created_at', '2026-01-01T00:00:00Z')}--{item['id']}.json"
    (queue[state] / name).write_text(json.dumps(item))
    return name


@pytest.fixture
def client():
    return TestClient(main.app)


# Every write route requires this header, so it is not optional in a test that
# expects to reach a handler.
CONFIRM = {"X-Review-Confirm": "1"}


# --- Type derivation ----------------------------------------------------------


class TestReviewTypeDerivation:
    def test_a_legacy_email_is_an_email_draft(self):
        assert review_types.review_type_of({"channel": "email"}) == "email_draft"

    def test_a_legacy_filter_is_a_gmail_filter(self):
        assert review_types.review_type_of({"channel": "gmail_filter"}) == "gmail_filter"

    def test_an_explicit_type_beats_a_conflicting_channel(self):
        item = {"channel": "email", "review_type": "memory_fact"}
        assert review_types.review_type_of(item) == "memory_fact"

    def test_an_unrecognised_item_is_generic_rather_than_an_error(self):
        # It still has to reach a human. Refusing to classify it must not mean
        # dropping it out of the queue.
        assert review_types.review_type_of({"id": "x"}) == "unknown"
        assert review_types.review_type_of(None) == "unknown"


class TestNormalizeSurvivesEveryShapeOnDisk:
    @pytest.mark.parametrize("key", sorted(LEGACY_ITEMS))
    def test_it_does_not_raise(self, key):
        out = main.normalize_item(dict(LEGACY_ITEMS[key]), "pending")
        assert out["review_type"] == "email_draft"
        assert out["state"] == "pending"

    def test_null_evidence_becomes_an_empty_dict(self):
        out = main.normalize_item(dict(LEGACY_ITEMS["null_evidence"]), "pending")
        assert out["evidence"] == {"scores": {}}

    def test_a_missing_subject_becomes_present_and_null(self):
        out = main.normalize_item(dict(LEGACY_ITEMS["no_subject"]), "pending")
        assert "subject" in out
        assert out["subject"] is None

    def test_a_missing_recipient_becomes_an_empty_one(self):
        out = main.normalize_item(dict(LEGACY_ITEMS["bare"]), "pending")
        assert out["recipient"] == {"name": None, "address": None, "org": None}

    def test_every_item_gets_a_title_even_with_no_subject(self):
        # The generic template renders from title/summary/fields, so an item
        # with neither a subject nor a template still says something.
        out = main.normalize_item(dict(LEGACY_ITEMS["no_subject"]), "pending")
        assert out["title"]

    def test_an_unknown_type_still_gets_an_action(self):
        out = main.normalize_item({"id": "z", "review_type": "memory_fact"}, "pending")
        assert [a["id"] for a in out["actions"]] == ["record"]


# --- The action gate ----------------------------------------------------------


class TestActionsAreDerivedNotDeclared:
    def test_an_email_offers_draft_and_send_with_draft_primary(self):
        actions = review_types.actions_for("email_draft")
        assert [a["id"] for a in actions] == ["create_draft", "send"]
        assert actions[0]["primary"] is True
        assert actions[1]["destructive"] is True

    def test_an_action_without_its_credential_is_visible_but_unavailable(self):
        actions = {a["id"]: a for a in review_types.actions_for("gmail_filter")}
        assert actions["create_filter"]["available"] is False
        assert "gmail.settings.basic" in actions["create_filter"]["unavailable_reason"]

    def test_an_unavailable_action_is_never_the_primary_one(self):
        # Otherwise the big green button is the disabled one.
        actions = review_types.actions_for("email_draft", capabilities=set())
        primary = [a for a in actions if a["primary"]]
        assert primary == [] or primary[0]["available"]

    def test_a_producer_hint_can_move_the_highlight(self):
        actions = {a["id"]: a for a in review_types.actions_for(
            "email_draft", suggested_action="send"
        )}
        assert actions["send"]["primary"] is True
        assert actions["create_draft"]["primary"] is False

    def test_a_producer_hint_cannot_invent_an_action(self):
        # The whole point: an item cannot talk its way into a capability.
        actions = review_types.actions_for("gmail_filter", suggested_action="send")
        assert "send" not in [a["id"] for a in actions]


class TestTheDecisionRouteEnforcesTheGate:
    def test_an_action_from_another_type_is_refused(self, queue, client):
        item = dict(LEGACY_ITEMS["full"])
        place(queue, item)
        r = client.post(
            "/api/review/decision",
            json={"id": item["id"], "decision": "approve", "action": "apply_labels"},
            headers=CONFIRM,
        )
        assert r.status_code == 422
        assert "create_draft" in r.text

    def test_a_refused_action_leaves_the_item_pending(self, queue, client):
        item = dict(LEGACY_ITEMS["full"])
        name = place(queue, item)
        client.post(
            "/api/review/decision",
            json={"id": item["id"], "decision": "approve", "action": "nonsense"},
            headers=CONFIRM,
        )
        assert (queue["pending"] / name).exists(), "a rejected request must not consume the item"

    def test_an_unavailable_action_is_a_conflict_not_a_queued_execution(
        self, queue, client, monkeypatch
    ):
        monkeypatch.setenv("REVIEW_CAPABILITIES", "")
        item = dict(LEGACY_ITEMS["full"])
        place(queue, item)
        r = client.post(
            "/api/review/decision",
            json={"id": item["id"], "decision": "approve", "action": "create_draft"},
            headers=CONFIRM,
        )
        assert r.status_code == 409
        assert r.json()["code"] == "capability_missing"


class TestDecisionValidation:
    def test_an_unknown_decision_is_refused(self, queue, client):
        # Regression: this used to file as *rejected* and report success,
        # because the handler was `approved if decision == 'approve' else
        # rejected` with nothing checking the value.
        item = dict(LEGACY_ITEMS["full"])
        place(queue, item)
        r = client.post(
            "/api/review/decision",
            json={"id": item["id"], "decision": "banana"},
            headers=CONFIRM,
        )
        assert r.status_code == 422
        assert not os.listdir(queue["rejected"])

    def test_rejecting_without_a_reason_is_400_not_500(self, queue, client):
        # Regression: the 400 was raised inside a try whose `except Exception`
        # turned it into a 500 with the message buried in a detail string.
        item = dict(LEGACY_ITEMS["full"])
        place(queue, item)
        r = client.post(
            "/api/review/decision",
            json={"id": item["id"], "decision": "reject", "rejection_reason": "  "},
            headers=CONFIRM,
        )
        assert r.status_code == 400

    def test_deciding_a_gone_item_is_a_conflict(self, queue, client):
        r = client.post(
            "/api/review/decision",
            json={"id": "nosuchid", "decision": "approve"},
            headers=CONFIRM,
        )
        assert r.status_code == 409
        assert r.json()["code"] == "conflict"


# --- The writes ---------------------------------------------------------------


class TestApproving:
    def test_it_records_which_action_was_taken(self, queue, client):
        item = dict(LEGACY_ITEMS["full"])
        name = place(queue, item)
        r = client.post(
            "/api/review/decision",
            json={"id": item["id"], "decision": "approve", "action": "send"},
            headers=CONFIRM,
        )
        assert r.status_code == 200
        written = json.loads((queue["approved"] / name).read_text())
        assert written["decision"] == "approve"
        assert written["action"] == "send"
        assert written["execution"]["state"] == "queued"
        assert written["execution"]["executor"] == "send"

    def test_no_action_means_the_types_primary_one(self, queue, client):
        # What the legacy /api/approvals/decision callers meant by "approve".
        item = dict(LEGACY_ITEMS["full"])
        name = place(queue, item)
        client.post(
            "/api/review/decision",
            json={"id": item["id"], "decision": "approve"},
            headers=CONFIRM,
        )
        assert json.loads((queue["approved"] / name).read_text())["action"] == "create_draft"

    def test_an_action_with_no_executor_is_not_queued_for_execution(self, queue, client):
        item = {"id": "gen00001", "created_at": "2026-08-07T00:00:00Z",
                "review_type": "memory_fact", "title": "t", "summary": "s"}
        name = place(queue, item)
        client.post(
            "/api/review/decision",
            json={"id": item["id"], "decision": "approve", "action": "record"},
            headers=CONFIRM,
        )
        written = json.loads((queue["approved"] / name).read_text())
        assert written["execution"]["state"] == "not_applicable"

    def test_an_edit_is_kept_only_when_it_differs(self, queue, client):
        item = dict(LEGACY_ITEMS["full"])
        name = place(queue, item)
        client.post(
            "/api/review/decision",
            json={"id": item["id"], "decision": "approve", "edited_body": "  hello  "},
            headers=CONFIRM,
        )
        assert json.loads((queue["approved"] / name).read_text())["edited_body"] is None

    def test_the_written_file_is_readable_by_another_uid(self, queue, client):
        # The executor is a separate container. mkstemp creates 0600 and
        # os.replace preserves it, so without the chmod every approved item is
        # one the executor cannot open — a queue that silently stops.
        item = dict(LEGACY_ITEMS["full"])
        name = place(queue, item)
        client.post(
            "/api/review/decision",
            json={"id": item["id"], "decision": "approve"},
            headers=CONFIRM,
        )
        mode = stat.S_IMODE(os.stat(queue["approved"] / name).st_mode)
        assert mode & stat.S_IRGRP, f"group cannot read: {oct(mode)}"
        assert mode & stat.S_IROTH, f"others cannot read: {oct(mode)}"


class TestRejecting:
    def test_it_lands_in_rejected_with_the_reason(self, queue, client):
        item = dict(LEGACY_ITEMS["full"])
        name = place(queue, item)
        r = client.post(
            "/api/review/decision",
            json={"id": item["id"], "decision": "reject", "rejection_reason": "off tone"},
            headers=CONFIRM,
        )
        assert r.status_code == 200
        written = json.loads((queue["rejected"] / name).read_text())
        assert written["rejection_reason"] == "off tone"
        assert written["execution"]["state"] == "not_applicable"


# --- Reading ------------------------------------------------------------------


class TestQueue:
    def test_it_splits_pending_from_what_needs_rescuing(self, queue, client):
        place(queue, dict(LEGACY_ITEMS["full"]))
        failed = dict(LEGACY_ITEMS["bare"])
        failed["execution"] = {"state": "failed", "error": {"kind": "boom"}}
        place(queue, failed, state="failed")

        body = client.get("/api/review/queue").json()
        assert [i["id"] for i in body["pending"]] == ["a7f3c920"]
        assert [i["id"] for i in body["attention"]] == ["d4e2f831"]

    def test_a_stalled_claim_shows_up_as_needing_attention(self, queue, client):
        item = dict(LEGACY_ITEMS["full"])
        item["execution"] = {"state": "running", "started_at": "2000-01-01T00:00:00Z"}
        place(queue, item, state="executing")
        body = client.get("/api/review/queue").json()
        assert body["attention"][0]["execution"]["error"]["kind"] == "stalled"
        assert body["attention"][0]["execution"]["error"]["retryable"] is False

    def test_the_legacy_path_still_returns_a_bare_array(self, queue, client):
        # A browser holding a cached bundle from before the rename.
        place(queue, dict(LEGACY_ITEMS["full"]))
        body = client.get("/api/approvals/queue").json()
        assert isinstance(body, list)
        assert body[0]["id"] == "a7f3c920"


class TestItemEndpoint:
    @pytest.mark.parametrize(
        "state", ["pending", "approved", "rejected", "executing", "executed", "failed"]
    )
    def test_it_finds_an_item_in_every_state(self, queue, client, state):
        item = dict(LEGACY_ITEMS["full"])
        place(queue, item, state=state)
        body = client.get(f"/api/review/item/{item['id']}").json()
        assert body["id"] == item["id"]
        assert body["state"] == state

    def test_a_decided_item_is_still_addressable(self, queue, client):
        # The reason this endpoint exists. You approve, the URL still names the
        # item, you reload — answering 404 would read as data loss.
        item = dict(LEGACY_ITEMS["full"])
        place(queue, item, state="executed")
        assert client.get(f"/api/review/item/{item['id']}").status_code == 200

    def test_an_unknown_id_is_an_honest_404(self, queue, client):
        assert client.get("/api/review/item/nosuchid").status_code == 404

    def test_it_refuses_a_traversing_id(self, queue, client):
        assert client.get("/api/review/item/..%2F..%2Fetc").status_code in (400, 404)


class TestRetryAndDismiss:
    def _failed(self, queue, retryable=True):
        item = dict(LEGACY_ITEMS["full"])
        item["execution"] = {
            "state": "failed",
            "executor": "create_draft",
            "attempts": 1,
            "error": {"kind": "boom", "message": "no", "retryable": retryable},
        }
        return item, place(queue, item, state="failed")

    def test_retry_puts_it_back_in_approved_for_the_executor(self, queue, client):
        item, name = self._failed(queue)
        r = client.post("/api/review/retry", json={"id": item["id"]}, headers=CONFIRM)
        assert r.status_code == 200
        written = json.loads((queue["approved"] / name).read_text())
        assert written["execution"]["state"] == "queued"
        assert "error" not in written["execution"]

    def test_an_unretryable_failure_refuses_to_be_retried(self, queue, client):
        # An ambiguous send. Re-running it could duplicate a message that
        # already went out, and no button should be able to do that.
        item, _name = self._failed(queue, retryable=False)
        r = client.post("/api/review/retry", json={"id": item["id"]}, headers=CONFIRM)
        assert r.status_code == 409

    def test_dismiss_moves_it_out_of_the_way(self, queue, client):
        item, name = self._failed(queue)
        r = client.post("/api/review/dismiss", json={"id": item["id"]}, headers=CONFIRM)
        assert r.status_code == 200
        assert json.loads((queue["executed"] / name).read_text())["execution"]["state"] == "abandoned"


# --- The guard ----------------------------------------------------------------


class TestWriteRoutesAreGuarded:
    def test_a_decision_without_the_confirm_header_is_refused(self, queue, client):
        # A form post or a simple cross-origin fetch cannot set a custom header
        # without a preflight, and the preflight is what the origin list refuses.
        # Before this, any page in the browser could POST here — which now means
        # any page could have sent mail.
        item = dict(LEGACY_ITEMS["full"])
        name = place(queue, item)
        r = client.post(
            "/api/review/decision", json={"id": item["id"], "decision": "approve"}
        )
        assert r.status_code == 403
        assert (queue["pending"] / name).exists()

    def test_a_cross_site_request_is_refused_even_with_the_header(self, queue, client):
        item = dict(LEGACY_ITEMS["full"])
        place(queue, item)
        r = client.post(
            "/api/review/decision",
            json={"id": item["id"], "decision": "approve"},
            headers={**CONFIRM, "Sec-Fetch-Site": "cross-site"},
        )
        assert r.status_code == 403

    def test_a_disallowed_origin_is_refused(self, queue, client):
        item = dict(LEGACY_ITEMS["full"])
        place(queue, item)
        r = client.post(
            "/api/review/decision",
            json={"id": item["id"], "decision": "approve"},
            headers={**CONFIRM, "Origin": "http://evil.example"},
        )
        assert r.status_code == 403

    def test_reading_the_queue_is_not_gated(self, queue, client):
        assert client.get("/api/review/queue").status_code == 200

    @pytest.mark.parametrize("route", ["/api/review/retry", "/api/review/dismiss"])
    def test_every_write_route_is_gated(self, queue, client, route):
        assert client.post(route, json={"id": "x"}).status_code == 403


class TestDeepLinkShellRoutes:
    @pytest.mark.parametrize(
        "path", ["/review", "/review/a7f3c920", "/approvals", "/approvals/a7f3c920"]
    )
    def test_the_shell_is_served_for_both_spellings(self, client, path, monkeypatch):
        # The old spelling has to keep working: links to it are already written
        # down. The frontend folds it onto the new tab and rewrites the URL.
        monkeypatch.setattr(main, "FRONTEND_INDEX", __file__)
        assert client.get(path).status_code == 200
