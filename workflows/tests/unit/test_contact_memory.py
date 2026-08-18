"""What a contact refresh actually stores.

The regression these pin is specific and was live: a Substack newsletter was
read as correspondence, its bodies were handed to `memory.add_episode`, and one
sender's document grew forty bullets of somebody else's prose — headings like
"The Situation" filed as peers of 800-character paragraphs.

So the assertions below are mostly negative. It is easy to check that a store
contains what we wanted; the failure here was that it also contained everything
else. Several tests therefore assert that a message body does NOT appear.
"""

from __future__ import annotations

import pytest

from app import contact_context, fact_distill, gmail_api, memory, wiki

CRM = [{
    "name": "Eric K",
    "job_title": "Principal",
    "company": "Northwind Strategies",
    "primary_location": "Chicago",
    "record_id": "rec_1",
    "web_url": "https://app.attio.com/x",
    "last_interaction": "email on 2026-08-06",
}]

MAIL = [{
    "subject": "Scoping the warehouse migration",
    "sender": "eric@x.example",
    "date": "2026-08-01",
    "snippet": "We have been weighing a cloud computing cooperative for years. " * 12,
}]

BULK = [
    {"subject": "The Consensus Paralysis", "sender": "Gene <g@substack.com>", "date": "2026-08-01"},
    {"subject": "The Innovation Theater Loop", "sender": "Gene <g@substack.com>", "date": "2026-08-09"},
]


@pytest.fixture(autouse=True)
def _wiki(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WIKI_DIR", str(tmp_path))


@pytest.fixture
def _no_model(monkeypatch):
    """The live configuration at the time of writing: provider set, key absent."""
    monkeypatch.setattr(fact_distill.config, "model_available", lambda: False)


class TestIsBulk:
    def test_list_unsubscribe_marks_a_publication(self):
        """RFC 2369's header is the only reliable separator: a Substack digest's
        From address is indistinguishable from a correspondent's."""
        assert contact_context.is_bulk({"list_unsubscribe": "<https://x/u>"})

    def test_a_list_id_marks_a_publication(self):
        assert contact_context.is_bulk({"list_id": "<news.example.com>"})

    def test_precedence_bulk_marks_a_publication(self):
        assert contact_context.is_bulk({"precedence": "Bulk"})

    def test_an_ordinary_message_is_not_bulk(self):
        assert not contact_context.is_bulk({"subject": "Re: Thursday", "sender": "a@x.com"})

    def test_an_empty_header_does_not_count(self):
        assert not contact_context.is_bulk({"list_unsubscribe": "   "})


class TestMailContextSplitsPublicationsOut:
    def _service(self, monkeypatch, messages):
        monkeypatch.setattr(gmail_api, "search_ids", lambda *a, **k: ["1", "2"])
        monkeypatch.setattr(gmail_api, "fetch_messages", lambda *a, **k: (messages, {}))
        return object()

    def test_a_newsletter_never_arrives_as_correspondence(self, monkeypatch):
        body = "A long essay about organisational dynamics. " * 20
        service = self._service(monkeypatch, [{
            "subject": "The Consensus Paralysis",
            "sender": "Gene <g@substack.com>",
            "received_at": "2026-08-09T10:00:00Z",
            "list_unsubscribe": "<https://substack.com/unsub>",
            "body": body,
        }])
        mail, bulk = contact_context.mail_context(service, "g@substack.com")
        assert mail == []
        assert len(bulk) == 1
        assert "snippet" not in bulk[0]

    def test_a_real_message_still_arrives_with_its_body(self, monkeypatch):
        body = "We have been weighing a cloud computing cooperative for years. " * 6
        service = self._service(monkeypatch, [{
            "subject": "Re: Office Hours",
            "sender": "b@x.com",
            "received_at": "2026-08-09T10:00:00Z",
            "body": body,
        }])
        mail, bulk = contact_context.mail_context(service, "b@x.com")
        assert bulk == []
        assert "cloud computing cooperative" in mail[0]["snippet"]


class TestDeterministicFacts:
    def test_identity_is_one_dense_line_with_the_employer_linked(self):
        """Three CRM fields, one fact. The employer is the one edge that needs
        no inference at all — the CRM gives us the name exactly."""
        facts = contact_context.deterministic_facts("eric@x.example", [], CRM)
        assert "Eric K is Principal at [[Northwind Strategies]]." in facts

    def test_no_fact_exceeds_the_store_ceiling(self):
        facts = contact_context.deterministic_facts("eric@x.example", MAIL, CRM, BULK)
        assert all(len(f) <= wiki.MAX_FACT_CHARS + 1 for f in facts)

    def test_the_record_stays_short(self):
        """The whole point. Forty bullets was the bug."""
        facts = contact_context.deterministic_facts("eric@x.example", MAIL, CRM, BULK)
        assert len(facts) <= 8

    def test_no_message_body_reaches_the_facts(self):
        facts = contact_context.deterministic_facts("eric@x.example", MAIL, CRM)
        assert not any("cloud computing cooperative" in f for f in facts)

    def test_correspondence_is_summarised_not_reproduced(self):
        facts = contact_context.deterministic_facts("eric@x.example", MAIL, [])
        assert any("Exchanged 1 substantive message" in f for f in facts)
        assert any("Scoping the warehouse migration" in f for f in facts)

    def test_a_publication_yields_one_fact_about_publishing(self):
        facts = contact_context.deterministic_facts("g@substack.com", [], [], BULK)
        assert len(facts) == 1
        assert "Sends list mail" in facts[0]
        assert "2026-08-01 to 2026-08-09" in facts[0]

    def test_transient_crm_fields_never_appear(self):
        facts = contact_context.deterministic_facts("eric@x.example", [], CRM)
        joined = " ".join(facts)
        assert "last_interaction" not in joined
        assert "app.attio.com" not in joined
        assert "rec_1" not in joined

    def test_nothing_known_yields_nothing(self):
        assert contact_context.deterministic_facts("x@y.com", [], [], []) == []


class TestRecordObservations:
    def test_the_deterministic_path_writes_short_linked_facts(self, _no_model):
        result = contact_context.record_observations(
            "eric@x.example", MAIL, CRM, source_description="contact refresh"
        )
        assert result["written"] >= 1
        assert result["distilled"] is False
        doc = wiki.read("eric@x.example")
        assert "Northwind Strategies" in doc.links

    def test_the_newsletter_regression_cannot_recur(self, _no_model):
        """The document that prompted all of this. Its bodies must not be
        stored, whatever else happens."""
        contact_context.record_observations(
            "g@substack.com", [], [], BULK, source_description="contact refresh"
        )
        doc = wiki.read("g@substack.com")
        assert len(doc.facts) == 1
        assert all(len(f["fact"]) <= wiki.MAX_FACT_CHARS + 1 for f in doc.facts)

    def test_nothing_observed_writes_nothing(self, _no_model):
        result = contact_context.record_observations(
            "x@y.com", [], [], source_description="contact refresh"
        )
        assert result["written"] == 0
        assert wiki.read("x@y.com") is None

    def test_a_repeat_refresh_adds_nothing(self, _no_model):
        """A monthly refresh against a CRM record that has not changed should
        not bury the one line that did under twelve identical ones."""
        for _ in range(3):
            contact_context.record_observations(
                "eric@x.example", [], CRM, source_description="contact refresh"
            )
        facts = [f["fact"] for f in wiki.read("eric@x.example").facts]
        assert len(facts) == len(set(facts))

    def test_a_distillation_is_preferred_and_its_links_are_written(self, monkeypatch):
        monkeypatch.setattr(
            fact_distill,
            "distill",
            lambda subject, raw: fact_distill.Distillation(
                facts=[fact_distill.DistilledFact(
                    fact="Leads delivery at Northwind Strategies on systems thinking.",
                    links=["Northwind Strategies", "systems thinking"],
                    confidence=0.9,
                )]
            ),
        )
        result = contact_context.record_observations(
            "eric@x.example", MAIL, CRM, source_description="contact refresh"
        )
        assert result["distilled"] is True
        doc = wiki.read("eric@x.example")
        assert sorted(doc.links) == ["Northwind Strategies", "systems thinking"]
        # The deterministic identity line must NOT also be written: the same
        # employer stated twice in two different sentences is a store a reader
        # has to reconcile.
        assert len(doc.facts) == 1

    def test_an_empty_distillation_is_a_verdict_not_a_fallback(self, monkeypatch):
        """The model read a newsletter and found nothing durable. Falling back
        to deterministic facts would overrule a judgement we asked for."""
        monkeypatch.setattr(
            fact_distill, "distill", lambda subject, raw: fact_distill.Distillation()
        )
        result = contact_context.record_observations(
            "g@substack.com", [], [], BULK, source_description="contact refresh"
        )
        assert result["written"] == 0
        assert wiki.read("g@substack.com") is None


class TestObservationsAreInputNotStorage:
    def test_publication_bodies_are_never_offered_to_the_model(self):
        raw = contact_context.observations("g@substack.com", [], [], BULK)
        assert "The Consensus Paralysis" in raw
        assert "bodies are deliberately not included" in raw

    def test_nothing_to_say_is_an_empty_string(self):
        assert contact_context.observations("x@y.com", [], [], []) == ""
