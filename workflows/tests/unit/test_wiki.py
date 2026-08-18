"""Tests for the wiki store and its FTS5 index."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import wiki, wiki_index


class WikiTestCase(unittest.TestCase):
    """Each test gets its own wiki tree, so the index cannot leak between them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._previous = os.environ.get("HERMES_WIKI_DIR")
        os.environ["HERMES_WIKI_DIR"] = self._tmp.name

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop("HERMES_WIKI_DIR", None)
        else:
            os.environ["HERMES_WIKI_DIR"] = self._previous
        self._tmp.cleanup()


class TestSlug(WikiTestCase):
    def test_same_key_always_yields_the_same_stem(self):
        self.assertEqual(wiki.slug("alice@example.com"), wiki.slug("alice@example.com"))

    def test_case_folds_so_one_entity_cannot_occupy_two_files(self):
        # The failure this prevents is silent: on a case-insensitive filesystem
        # two spellings would race for one file and lose writes.
        self.assertEqual(wiki.slug("Alice@Example.com"), wiki.slug("alice@example.com"))

    def test_a_key_that_folds_away_entirely_still_gets_a_unique_stem(self):
        first = wiki.slug("日本語")
        second = wiki.slug("한국어")
        self.assertTrue(first)
        self.assertNotEqual(first, second)

    def test_path_separators_cannot_escape_the_wiki_directory(self):
        # A key is caller-supplied — an Attio id or an address — so it must not
        # be able to address a path outside the tree.
        stem = wiki.slug("../../etc/passwd")
        self.assertNotIn("/", stem)
        self.assertFalse(stem.startswith("."))
        resolved = wiki.path_for("../../etc/passwd").resolve()
        self.assertEqual(resolved.parent, Path(self._tmp.name).resolve())


class TestRoundTrip(WikiTestCase):
    def test_append_then_read_preserves_facts_and_provenance(self):
        wiki.append_section(
            "alice@example.com",
            ["Works for [[Kestrel Underwriting]] as Head of Analytics."],
            "contact refresh from Attio and Gmail",
            title="Alice Smith",
            aliases=["Alice Smith"],
        )
        doc = wiki.read("alice@example.com")
        self.assertIsNotNone(doc)
        assert doc is not None
        self.assertEqual(doc.title, "Alice Smith")
        self.assertIn("Alice Smith", doc.aliases)
        self.assertEqual(len(doc.facts), 1)
        self.assertEqual(
            doc.facts[0]["source_description"], "contact refresh from Attio and Gmail"
        )

    def test_render_parse_is_a_fixed_point(self):
        wiki.append_section("k", ["one", "two"], "src", title="T", aliases=["A", "B"])
        doc = wiki.read("k")
        assert doc is not None
        reparsed = wiki.parse(wiki.render(doc), key="k")
        self.assertEqual(wiki.render(reparsed), wiki.render(doc))

    def test_appending_is_additive_rather_than_replacing(self):
        # The graph re-extracted a whole prose blob per refresh and produced 66
        # near-duplicate episodes. Appending keeps each observation distinct.
        wiki.append_section("k", ["first"], "run one")
        wiki.append_section("k", ["second"], "run two")
        doc = wiki.read("k")
        assert doc is not None
        self.assertEqual([f["fact"] for f in doc.facts], ["second", "first"])

    def test_newest_section_is_returned_first(self):
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        new = datetime(2026, 1, 1, tzinfo=timezone.utc)
        wiki.append_section("k", ["older"], "s", when=old)
        wiki.append_section("k", ["newer"], "s", when=new)
        doc = wiki.read("k")
        assert doc is not None
        self.assertEqual(doc.facts[0]["fact"], "newer")

    def test_a_malformed_file_yields_what_it_can_rather_than_raising(self):
        path = wiki.path_for("broken")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nkey: broken\n(no closing fence)\n- orphan bullet\n")
        doc = wiki.read("broken")
        self.assertIsNotNone(doc)

    def test_reading_an_absent_document_returns_none(self):
        self.assertIsNone(wiki.read("nobody@example.com"))


class TestFactsAreBounded(WikiTestCase):
    """One bullet is one fact, and a fact is a sentence.

    This ceiling is the invariant that was missing. With no bound at the write,
    a caller handing over an 1800-character email body stored an 1800-character
    "fact"; nine documents of those is what the store became. Enforced here
    rather than at each caller, because the next caller will be written by
    someone who has not read that caller.
    """

    def test_an_over_long_line_is_truncated_at_the_write(self):
        wiki.append_section("a@x.com", ["word " * 500], "ingest")
        stored = wiki.read("a@x.com").facts[0]["fact"]
        self.assertLessEqual(len(stored), wiki.MAX_FACT_CHARS + 1)
        self.assertTrue(stored.endswith("…"))

    def test_truncation_lands_on_a_word_boundary(self):
        cut = wiki.truncate_fact("alpha bravo charlie delta echo foxtrot", limit=20)
        self.assertNotIn("cha…", cut)
        self.assertTrue(cut.endswith("…"))

    def test_a_short_fact_is_left_exactly_as_written(self):
        line = "Works for [[Kestrel Underwriting]] as Head of Analytics."
        self.assertEqual(wiki.truncate_fact(line), line)

    def test_provenance_survives_truncation(self):
        """A fact that keeps its text and loses the reason to believe it is
        worse than one that was never stored."""
        cut = wiki.truncate_fact("word " * 200 + "<!-- source_ref=abc; confidence=0.9 -->")
        self.assertTrue(cut.endswith("<!-- source_ref=abc; confidence=0.9 -->"))
        self.assertIn("…", cut)

    def test_hard_wrapped_input_is_collapsed_to_one_line(self):
        self.assertEqual(wiki.truncate_fact("alpha\n  bravo\tcharlie"), "alpha bravo charlie")


class TestRelevanceFloor(WikiTestCase):
    """The specific defect that made the graph unusable.

    contact_context.relevant_facts() existed because Graphiti "has no relevance
    floor: it returns its closest facts whether or not any are about the person
    asked for. Two unrelated addresses once returned the same five facts about
    two other people." That is reproduced here and must not happen.
    """

    def setUp(self) -> None:
        super().setUp()
        wiki.append_section(
            "alice@kestrel.example",
            ["Alice runs the analytics team at [[Kestrel Underwriting]]."],
            "attio",
            title="Alice Smith",
            aliases=["Alice Smith"],
        )
        wiki.append_section(
            "bob@heron.example",
            ["Bob chairs the [[Heron Group]] risk committee."],
            "attio",
            title="Bob Jones",
            aliases=["Bob Jones"],
        )

    def test_an_unrelated_address_returns_nothing_at_all(self):
        self.assertEqual(wiki_index.search("carol@osprey.example"), [])

    def test_two_unrelated_addresses_do_not_return_the_same_facts(self):
        alice = {f["fact"] for f in wiki_index.search("alice@kestrel.example")}
        bob = {f["fact"] for f in wiki_index.search("bob@heron.example")}
        self.assertTrue(alice)
        self.assertTrue(bob)
        self.assertEqual(alice & bob, set())

    def test_a_query_of_only_noise_tokens_returns_nothing(self):
        # "com" and "www" match nearly every document; an OR query built from
        # them would return the corpus.
        self.assertEqual(wiki_index.search("www.com"), [])

    def test_the_right_person_is_found_by_local_part(self):
        hits = wiki_index.search("alice@kestrel.example")
        self.assertTrue(any("Alice" in hit["fact"] for hit in hits))


class TestIndexIsDisposable(WikiTestCase):
    def test_deleting_the_index_reproduces_identical_results(self):
        wiki.append_section("a@x.example", ["Alpha fact about widgets"], "s")
        wiki.append_section("b@y.example", ["Beta fact about widgets"], "s")
        before = wiki_index.search("widgets")
        wiki_index.index_path().unlink()
        after = wiki_index.search("widgets")
        self.assertEqual(before, after)
        self.assertTrue(before)

    def test_the_index_refreshes_when_the_markdown_changes(self):
        wiki.append_section("a@x.example", ["first observation"], "s")
        self.assertTrue(wiki_index.search("observation"))
        wiki.append_section("a@x.example", ["second sighting entirely"], "s")
        self.assertTrue(wiki_index.search("sighting"))

    def test_a_corrupt_index_is_rebuilt_rather_than_raising(self):
        wiki.append_section("a@x.example", ["recoverable content here"], "s")
        wiki_index.search("recoverable")
        wiki_index.index_path().write_bytes(b"this is not a database")
        self.assertTrue(wiki_index.search("recoverable"))


class TestBacklinks(WikiTestCase):
    def test_backlinks_give_the_one_hop_neighbourhood(self):
        wiki.append_section("alice@x.example", ["Works for [[Kestrel]]"], "s")
        wiki.append_section("bob@y.example", ["Also works for [[Kestrel]]"], "s")
        wiki.append_section("carol@z.example", ["Unrelated entirely"], "s")
        self.assertEqual(
            sorted(wiki_index.backlinks("Kestrel")),
            ["alice@x.example", "bob@y.example"],
        )

    def test_link_targets_match_case_insensitively(self):
        wiki.append_section("a@x.example", ["Works for [[Kestrel Underwriting]]"], "s")
        self.assertEqual(wiki_index.backlinks("kestrel underwriting"), ["a@x.example"])

    def test_an_unlinked_document_has_no_backlinks(self):
        wiki.append_section("a@x.example", ["No links here"], "s")
        self.assertEqual(wiki_index.backlinks("a@x.example"), [])


if __name__ == "__main__":
    unittest.main()
