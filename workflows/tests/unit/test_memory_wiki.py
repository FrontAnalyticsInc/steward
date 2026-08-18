"""Tests for the wiki-backed memory module.

These pin the contract `contact_context` and `calendar_daily_briefing` depend
on, which did not change when the knowledge graph was deleted: ask for facts
about an address, get back dicts carrying `fact` and a `valid_at` old enough to
age against.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import contact_context, memory, wiki

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _wiki(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WIKI_DIR", str(tmp_path))


class TestKeyFor:
    def test_the_contact_prefix_is_stripped_so_the_file_is_keyed_on_the_address(self):
        assert memory.key_for("contact:alice@example.com") == "alice@example.com"

    def test_a_name_without_the_prefix_is_used_as_is(self):
        assert memory.key_for("Kestrel Underwriting") == "Kestrel Underwriting"


class TestBodyToLines:
    def test_crm_bullets_become_one_fact_each(self):
        lines = memory.body_to_lines("- a@x.com -> name: A\n- a@x.com -> title: B")
        assert lines == ["a@x.com -> name: A", "a@x.com -> title: B"]

    def test_an_indented_continuation_stays_with_its_bullet(self):
        """A message is one observation; three rows would index a subject with
        no sender and read as three unrelated facts."""
        body = "- Subject: Scoping\n  From: a@x.com\n  let's talk pricing"
        assert memory.body_to_lines(body) == [
            "Subject: Scoping From: a@x.com let's talk pricing"
        ]

    def test_a_bare_header_line_is_dropped(self):
        body = "What a@x.com has written recently, in their own words:\n- Subject: Hi"
        assert memory.body_to_lines(body) == ["Subject: Hi"]

    def test_a_bulleted_line_ending_in_a_colon_is_kept(self):
        """Only unbulleted headers are dropped — a real observation may well
        end in a colon."""
        assert memory.body_to_lines("- Subject:") == ["Subject:"]

    def test_blank_lines_are_ignored(self):
        assert memory.body_to_lines("- one\n\n\n- two") == ["one", "two"]

    def test_an_empty_body_yields_no_lines(self):
        assert memory.body_to_lines("") == []


class TestAddFacts:
    """The write path for material that has already been distilled.

    `add_episode` takes raw text and splits it; this takes facts that are
    already facts. Contact refresh uses this one, because by then
    `app/fact_distill.py` has done the extraction and splitting its output again
    would be nonsense.

    Deduplication is `wiki.append_section_deduped`'s job, not this module's; the
    cases below pin the behaviour contact memory depends on, through the door it
    actually uses.
    """

    def test_facts_are_written_and_counted(self):
        assert memory.add_facts("a@x.example", ["One.", "Two."], "src") == 2

    def test_a_repeat_is_not_written_twice(self):
        memory.add_facts("a@x.example", ["Works at Acme."], "src")
        assert memory.add_facts("a@x.example", ["Works at Acme."], "src") == 0

    def test_only_the_new_fact_is_appended(self):
        memory.add_facts("a@x.example", ["Works at Acme."], "src")
        assert memory.add_facts("a@x.example", ["Works at Acme.", "Moved to Berlin."], "src") == 1

    def test_the_first_occurrence_keeps_its_date(self):
        """When we learned something is the timestamp worth having."""
        memory.add_facts("a@x.example", ["Works at Acme."], "first")
        memory.add_facts("a@x.example", ["Works at Acme.", "New thing."], "second")
        by_fact = {f["fact"]: f for f in wiki.read("a@x.example").facts}
        assert by_fact["Works at Acme."]["source_description"] == "first"

    def test_becoming_linkable_does_not_make_a_fact_new(self):
        """The same statement, once the employer has a page. Comparing the
        rendered text would file it twice."""
        memory.add_facts("a@x.example", ["Works at Acme."], "src")
        assert memory.add_facts("a@x.example", ["Works at [[Acme]]."], "src") == 0

    def test_a_new_run_id_does_not_make_a_fact_new(self):
        memory.add_facts("a@x.example", ["Works at Acme. <!-- run_id=1 -->"], "src")
        assert memory.add_facts("a@x.example", ["Works at Acme. <!-- run_id=2 -->"], "src") == 0

    def test_duplicates_within_one_call_collapse(self):
        assert memory.add_facts("a@x.example", ["Same.", "same."], "src") == 1

    def test_writing_nothing_is_quiet_rather_than_an_error(self):
        assert memory.add_facts("a@x.example", [], "src") == 0


class TestRoundTripContract:
    def test_a_refresh_is_readable_by_the_briefing_that_follows_it(self):
        """The contract that matters: what a refresh writes, a briefing reads.

        Goes through `record_observations` rather than assembling a body by
        hand, because that is now the only supported way in — and with no model
        configured in the test environment it exercises the deterministic path,
        which is also what runs in production while ANTHROPIC_API_KEY is unset.
        """
        contact_context.record_observations(
            "eric@x.example",
            [{"subject": "Scoping", "sender": "eric@x.example",
              "date": "2026-08-01", "snippet": "pricing"}],
            [{"name": "Eric K", "job_title": "Principal", "company": "Northwind",
              "record_id": "rec_1"}],
            source_description="contact refresh from Attio and Gmail",
        )
        facts = memory.search_facts("eric@x.example")
        assert facts
        assert all("fact" in f and "valid_at" in f for f in facts)
        assert any("Principal" in f["fact"] for f in facts)

    def test_provenance_survives_onto_every_fact(self):
        memory.add_episode(
            name="contact:a@x.example",
            body="- something observed",
            source_description="calendar_daily_briefing attendee refresh",
        )
        facts = memory.search_facts("a@x.example")
        assert facts[0]["source_description"] == (
            "calendar_daily_briefing attendee refresh"
        )

    def test_add_episode_reports_what_it_wrote(self):
        result = memory.add_episode("contact:a@x.example", "- one\n- two", "src")
        assert result["facts_written"] == 2
        assert result["key"] == "a@x.example"

    def test_the_freshness_clock_still_reads_the_timestamps(self):
        """contact_context ages facts on valid_at; the wiki must keep supplying
        one that parses, or every contact reads as never-known and refreshes
        forever."""
        memory.add_episode("contact:a@x.example", "- observed", "src")
        facts = memory.search_facts("a@x.example")
        age = contact_context.freshest_age(facts, datetime.now(timezone.utc))
        assert age is not None
        assert age < 1

    def test_a_stale_document_reads_as_stale(self):
        wiki.append_section(
            "old@x.example", ["ancient news"], "src", when=NOW - timedelta(days=400)
        )
        facts = memory.search_facts("old@x.example")
        assert contact_context.freshest_age(facts, NOW) > 365


class TestOwnDocumentAlwaysWins:
    def test_the_persons_own_file_is_returned_even_when_ranked_low(self):
        """An exact key hit needs no scoring. This is what a stable filename
        buys over an LLM resolving 'M. Vale' to an entity per extraction."""
        memory.add_episode("contact:quiet@x.example", "- zzz", "src")
        for index in range(20):
            wiki.append_section(f"noise{index}@y.example", ["zzz zzz zzz"], "src")
        facts = memory.search_facts("quiet@x.example")
        assert facts
        assert facts[0]["key"] == "quiet@x.example"

    def test_results_are_deduplicated_across_both_sources(self):
        memory.add_episode("contact:a@x.example", "- distinctive widget fact", "src")
        facts = memory.search_facts("a@x.example")
        texts = [f["fact"] for f in facts]
        assert len(texts) == len(set(texts))

    def test_max_facts_is_honoured(self):
        body = "\n".join(f"- fact number {i}" for i in range(30))
        memory.add_episode("contact:a@x.example", body, "src")
        assert len(memory.search_facts("a@x.example", max_facts=5)) == 5
