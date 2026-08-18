"""Tests for writing extracted triplets into the wiki.

The extraction shape is unchanged from what `graph_write` submitted to Graphiti
— agents still produce (source, edge, target) intents with provenance. What is
pinned here is that the object becomes a `[[wikilink]]`, because that link is
what keeps the one-hop neighbourhood traversable now that RELATES_TO edges are
gone.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import wiki, wiki_index, wiki_write


@pytest.fixture(autouse=True)
def _wiki(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WIKI_DIR", str(tmp_path))


def intent(**overrides):
    base = {
        "source_name": "Kestrel Underwriting",
        "source_labels": ["Organization"],
        "edge_name": "Offers",
        "target_name": "Claims Forecasting",
        "target_labels": ["Topic"],
        "fact": "Kestrel Underwriting offers Claims Forecasting to mid-market insurers.",
        "attributes": {},
    }
    base.update(overrides)
    return base


class TestRendering:
    def test_the_target_becomes_a_link_where_it_appears_in_the_fact(self):
        line = wiki_write._render(intent())
        assert "[[Claims Forecasting]]" in line
        assert line.startswith("Kestrel Underwriting offers")

    def test_a_target_absent_from_the_prose_is_appended_rather_than_lost(self):
        line = wiki_write._render(
            intent(fact="They also run a quarterly risk seminar.")
        )
        assert "[[Claims Forecasting]]" in line
        assert "quarterly risk seminar" in line

    def test_only_the_first_occurrence_is_linked(self):
        """Linking every mention makes the prose unreadable and the backlink is
        already established by the first."""
        line = wiki_write._render(
            intent(fact="Claims Forecasting matters because Claims Forecasting sells.")
        )
        assert line.count("[[Claims Forecasting]]") == 1

    def test_an_intent_with_no_fact_still_yields_the_relationship(self):
        line = wiki_write._render(intent(fact=""))
        assert line == "Kestrel Underwriting Offers [[Claims Forecasting]]"

    def test_an_intent_with_neither_fact_nor_target_renders_nothing(self):
        assert wiki_write._render(intent(fact="", target_name="")) == ""

    def test_provenance_travels_with_the_assertion(self):
        line = wiki_write._render(
            intent(attributes={"source_url": "https://x.example/about", "confidence": "high"})
        )
        assert "source_url=https://x.example/about" in line
        assert "confidence=high" in line

    def test_provenance_is_a_comment_so_it_does_not_read_as_prose(self):
        line = wiki_write._render(intent(attributes={"run_id": "r1"}))
        assert "<!--" in line and "-->" in line


class TestWriteTriplets:
    def test_facts_land_under_their_subject(self):
        attempted, written, errors = wiki_write.write_triplets([intent()])
        assert (attempted, written, errors) == (1, 1, [])
        doc = wiki.read("Kestrel Underwriting")
        assert doc is not None
        assert len(doc.facts) == 1

    def test_many_facts_about_one_subject_write_one_section(self):
        """Twenty sections for one crawl would make the file unreadable, which
        is the property this format exists for."""
        intents = [intent(target_name=f"Topic {i}", fact=f"Fact {i}.") for i in range(5)]
        wiki_write.write_triplets(intents)
        doc = wiki.read("Kestrel Underwriting")
        assert doc is not None
        assert len(doc.sections) == 1
        assert len(doc.sections[0].lines) == 5

    def test_facts_about_different_subjects_go_to_different_documents(self):
        wiki_write.write_triplets(
            [intent(), intent(source_name="Heron Group", fact="Heron Group is a broker.")]
        )
        assert wiki.read("Kestrel Underwriting") is not None
        assert wiki.read("Heron Group") is not None

    def test_the_link_is_a_backlink_the_target_can_be_found_by(self):
        wiki_write.write_triplets([intent()])
        assert wiki_index.backlinks("Claims Forecasting") == ["Kestrel Underwriting"]

    def test_one_bad_intent_does_not_cost_the_rest_of_the_crawl(self):
        attempted, written, errors = wiki_write.write_triplets(
            [intent(source_name=""), intent()]
        )
        assert attempted == 2
        assert written == 1
        assert len(errors) == 1

    def test_an_empty_batch_is_not_an_error(self):
        assert wiki_write.write_triplets([]) == (0, 0, [])

    def test_written_means_written_rather_than_submitted(self):
        """graph_write could only report 'submitted': the graph extracted
        asynchronously after returning, so an accepted write and a dropped one
        were indistinguishable. These bytes are on disk before this returns."""
        _, written, _ = wiki_write.write_triplets([intent()])
        assert written == 1
        assert wiki.path_for("Kestrel Underwriting").is_file()

    def test_rerun_with_different_run_id_does_not_append_duplicate_fact(self):
        first = intent(attributes={"source_ref": "https://kestrel.io/", "run_id": "run-1"})
        second = intent(attributes={"source_ref": "https://kestrel.io/", "run_id": "run-2"})

        assert wiki_write.write_triplets([first])[:2] == (1, 1)
        assert wiki_write.write_triplets([second])[:2] == (1, 0)

        doc = wiki.read("Kestrel Underwriting")
        assert doc is not None
        assert len(doc.sections) == 1
        assert len(doc.facts) == 1

    def test_a_materially_changed_fact_still_appends(self):
        wiki_write.write_triplets([intent(attributes={"run_id": "run-1"})])
        changed = intent(
            fact="Kestrel Underwriting offers Claims Forecasting to enterprise insurers.",
            attributes={"run_id": "run-2"},
        )

        assert wiki_write.write_triplets([changed])[:2] == (1, 1)

        doc = wiki.read("Kestrel Underwriting")
        assert doc is not None
        assert len(doc.sections) == 2
        assert len(doc.facts) == 2

    def test_all_duplicate_write_refreshes_without_empty_section(self):
        first_when = datetime(2026, 8, 1, tzinfo=timezone.utc)
        second_when = datetime(2026, 8, 2, tzinfo=timezone.utc)
        line = "Kestrel works on [[simulation]] <!-- source_ref=https://kestrel.io/; run_id=run-1 -->"
        _, written, skipped = wiki.append_section_deduped(
            "Kestrel", [line], "test", title="Kestrel", when=first_when
        )
        assert (written, skipped) == (1, 0)

        duplicate = "  Kestrel   works on [[simulation]] <!-- source_ref=https://kestrel.io/; run_id=run-2 -->"
        _, written, skipped = wiki.append_section_deduped(
            "Kestrel", [duplicate], "test", title="Kestrel", when=second_when
        )

        doc = wiki.read("Kestrel")
        assert (written, skipped) == (0, 1)
        assert doc is not None
        assert len(doc.sections) == 1
        assert doc.last_refreshed == "2026-08-02T00:00:00Z"


class TestWriteFacts:
    def test_plain_observations_can_be_appended_without_a_triplet(self):
        assert wiki_write.write_facts("a@x.example", ["one", "two"], "src") == 2

    def test_an_empty_list_writes_nothing(self):
        assert wiki_write.write_facts("a@x.example", [], "src") == 0
        assert wiki.read("a@x.example") is None
