"""The item shape producers write, and what stays true about it.

Two things are being protected here. The first is the dual-write of
`review_type` alongside the legacy `channel`: items live in this queue for days,
so a deploy that rolls the dashboard back must still be able to read what was
written while the new one was up.

The second is that this module does not grant itself capabilities. An action
list written by the producer would be a capability claimed by the process whose
input is arbitrary inbound mail — the thing the pending-only mount exists to
prevent. The reviewer derives actions from the type; the most a producer may say
is `suggested_action`, which the reviewer validates before honouring.
"""

from __future__ import annotations

from app import approvals


class TestDualWrite:
    def test_email_draft_declares_both_the_new_type_and_the_old_channel(self):
        item = approvals.email_draft(body="b", reason="r")
        assert item["review_type"] == "email_draft"
        assert item["channel"] == "email", "a rolled-back dashboard reads channel"

    def test_filter_proposal_declares_both(self):
        item = approvals.filter_proposal(
            name="n", rule={"add_label": "L"}, rationale="why", example_message_ids=[]
        )
        assert item["review_type"] == "gmail_filter"
        assert item["channel"] == "gmail_filter"

    def test_a_new_type_may_omit_channel_entirely(self):
        item = approvals.review_item("memory_fact", title="t", summary="s")
        assert item["review_type"] == "memory_fact"
        assert "channel" not in item


class TestProducerCannotGrantItselfActions:
    def test_no_constructor_emits_an_actions_list(self):
        for item in (
            approvals.email_draft(body="b", reason="r"),
            approvals.filter_proposal(
                name="n", rule={}, rationale="r", example_message_ids=[]
            ),
            approvals.review_item("memory_fact", title="t", summary="s"),
        ):
            assert "actions" not in item, (
                "the producer must not declare what may be done with its output"
            )


class TestEmailDraft:
    def test_a_reply_carries_its_parent_so_it_can_be_threaded(self):
        item = approvals.email_draft(
            body="b",
            reason="r",
            message_id="m1",
            thread_id="t1",
            rfc_message_id="<abc@mail.example.com>",
        )
        assert item["in_reply_to"] == {
            "message_id": "m1",
            "thread_id": "t1",
            "rfc_message_id": "<abc@mail.example.com>",
        }

    def test_the_rfc_message_id_is_kept_apart_from_the_gmail_id(self):
        # Not one field. The Gmail id addresses the API, the RFC id addresses
        # the recipient's mail client, and collapsing them is the bug this
        # separation exists to prevent.
        item = approvals.email_draft(
            body="b",
            reason="r",
            message_id="19a3f0c2",
            thread_id="t1",
            rfc_message_id="<abc@mail.example.com>",
        )
        parent = item["in_reply_to"]
        assert parent["message_id"] != parent["rfc_message_id"]

    def test_a_parent_without_a_known_rfc_id_still_travels(self):
        # Items queued before the header was captured. Gmail threads them by
        # thread_id, so they must not be rejected for lacking the header.
        item = approvals.email_draft(body="b", reason="r", message_id="m1", thread_id="t1")
        assert item["in_reply_to"]["rfc_message_id"] is None

    def test_cold_outreach_has_no_parent(self):
        item = approvals.email_draft(body="b", reason="r")
        assert item["in_reply_to"] is None

    def test_body_format_is_declared_not_guessed(self):
        assert approvals.email_draft(body="b", reason="r")["body_format"] == "markdown"


class TestFilterProposal:
    def test_the_rule_travels_as_data_not_as_an_editable_body(self):
        rule = {"add_label": "Check Later", "from_pattern": "@x.com"}
        item = approvals.filter_proposal(
            name="n", rule=rule, rationale="why", example_message_ids=["a"]
        )
        assert item["rule"] == rule
        # body is the editable field. A rule flattened into it would put a
        # textarea in front of a reviewer over text nothing parses back.
        assert item["body"] is None


class TestGenericReviewItem:
    def test_it_round_trips_the_fields_the_generic_template_renders(self):
        fields = [{"label": "Source", "value": "https://example.com"}]
        item = approvals.review_item(
            "crm_todo", title="Call Dana", summary="No contact in 90 days", fields=fields
        )
        assert item["title"] == "Call Dana"
        assert item["summary"] == "No contact in 90 days"
        assert item["fields"] == fields
        # `reason` mirrors summary so the existing list rail, which reads reason,
        # says something useful for a type it has never heard of.
        assert item["reason"] == "No contact in 90 days"

    def test_extra_payload_is_carried_through(self):
        item = approvals.review_item(
            "memory_fact", title="t", summary="s", payload={"topic": "x"}
        )
        assert item["payload"] == {"topic": "x"}

    def test_every_item_has_the_decision_keys_the_reviewer_writes(self):
        item = approvals.review_item("memory_fact", title="t", summary="s")
        for key in ("decision", "decided_at", "rejection_reason", "edited_body"):
            assert key in item
            assert item[key] is None
