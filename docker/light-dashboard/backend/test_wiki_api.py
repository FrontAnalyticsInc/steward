"""Tests for the dashboard's read-only view of the wiki.

The tab reads a directory the workflows service writes. Two properties matter
more than the rendering: it must never create or modify anything (it runs as a
different user, and a dashboard-owned index would lock the writer out of its own
store), and a missing index must degrade to "no search" rather than an error
page.
"""

from __future__ import annotations

import importlib
import sqlite3
import tempfile
import unittest
from pathlib import Path


def _load(directory: Path):
    """Import wiki_api bound to a scratch directory.

    Reloaded per test because WIKI_DIR is resolved at import time, matching how
    the module runs in the container.
    """
    import os

    os.environ["HERMES_WIKI_DIR"] = str(directory)
    from . import wiki_api

    return importlib.reload(wiki_api)


def _write(directory: Path, stem: str, body: str) -> None:
    (directory / f"{stem}.md").write_text(body, encoding="utf-8")


DOC = """---
key: alice@example.com
title: Alice Smith
aliases: Alice Smith, A. Smith
last_refreshed: 2026-08-08T12:00:00Z
---

# Alice Smith

## 2026-08-08T12:00:00Z — contact refresh from Attio and Gmail
- Runs analytics at [[Kestrel Underwriting]].
- Prefers email to calls.
"""


class WikiApiTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.api = _load(self.dir)

    def tearDown(self):
        self._tmp.cleanup()


class TestHealth(WikiApiTestCase):
    def test_an_empty_directory_is_ok_with_nothing_in_it(self):
        health = self.api.health()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["documents"], 0)

    def test_a_missing_directory_says_unavailable_rather_than_raising(self):
        api = _load(self.dir / "does-not-exist")
        self.assertEqual(api.health()["status"], "unavailable")

    def test_documents_are_counted(self):
        _write(self.dir, "alice@example.com", DOC)
        self.assertEqual(self.api.health()["documents"], 1)

    def test_a_missing_index_is_reported_not_created(self):
        """The dashboard runs as a different user than the writer. An index it
        created would be owned by the wrong user and the workflows container
        could no longer refresh its own store."""
        _write(self.dir, "alice@example.com", DOC)
        health = self.api.health()
        self.assertFalse(health["index_present"])
        self.assertFalse((self.dir / self.api.INDEX_FILENAME).exists())

    def test_reading_never_creates_an_index_file(self):
        _write(self.dir, "alice@example.com", DOC)
        self.api.documents()
        self.api.search("analytics")
        self.api.backlinks("Kestrel Underwriting")
        self.assertFalse((self.dir / self.api.INDEX_FILENAME).exists())


class TestDocuments(WikiApiTestCase):
    def setUp(self):
        super().setUp()
        _write(self.dir, "alice@example.com", DOC)

    def test_frontmatter_is_parsed_into_the_list_row(self):
        rows = self.api.documents()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Alice Smith")
        self.assertEqual(rows[0]["key"], "alice@example.com")
        self.assertIn("A. Smith", rows[0]["aliases"])

    def test_filtering_matches_title_or_key(self):
        self.assertEqual(len(self.api.documents(q="alice")), 1)
        self.assertEqual(len(self.api.documents(q="nobody")), 0)

    def test_a_missing_directory_raises_the_typed_error(self):
        api = _load(self.dir / "gone")
        with self.assertRaises(api.WikiUnavailable):
            api.documents()


class TestDocument(WikiApiTestCase):
    def setUp(self):
        super().setUp()
        _write(self.dir, "alice@example.com", DOC)

    def test_the_raw_markdown_is_returned_so_the_tab_shows_the_record(self):
        doc = self.api.document("alice@example.com")
        self.assertIn("## 2026-08-08T12:00:00Z", doc["markdown"])

    def test_sections_and_bullets_are_parsed(self):
        doc = self.api.document("alice@example.com")
        self.assertEqual(len(doc["sections"]), 1)
        self.assertEqual(len(doc["sections"][0]["lines"]), 2)

    def test_outgoing_links_are_extracted(self):
        doc = self.api.document("alice@example.com")
        self.assertEqual(doc["links_out"], ["Kestrel Underwriting"])

    def test_an_absent_document_is_none_rather_than_an_error(self):
        self.assertIsNone(self.api.document("nobody"))

    def test_a_key_cannot_address_a_file_outside_the_wiki(self):
        """The slug is caller-supplied through a URL path."""
        self.assertIsNone(self.api.document("../../etc/passwd"))


class TestSearchWithoutAnIndex(WikiApiTestCase):
    def test_search_returns_nothing_rather_than_failing(self):
        _write(self.dir, "alice@example.com", DOC)
        self.assertEqual(self.api.search("analytics"), [])

    def test_backlinks_return_nothing_rather_than_failing(self):
        self.assertEqual(self.api.backlinks("Kestrel Underwriting"), [])


class TestSearchWithAnIndex(WikiApiTestCase):
    def setUp(self):
        super().setUp()
        _write(self.dir, "alice@example.com", DOC)
        con = sqlite3.connect(self.dir / self.api.INDEX_FILENAME)
        con.executescript(
            """
            CREATE VIRTUAL TABLE facts USING fts5(
                fact, title, aliases, key UNINDEXED, when_iso UNINDEXED,
                source UNINDEXED, tokenize = 'unicode61'
            );
            CREATE TABLE links (src TEXT NOT NULL, dst_slug TEXT NOT NULL);
            """
        )
        con.execute(
            "INSERT INTO facts VALUES (?,?,?,?,?,?)",
            (
                "Runs analytics at Kestrel Underwriting.",
                "Alice Smith",
                "",
                "alice@example.com",
                "2026-08-08T12:00:00Z",
                "attio",
            ),
        )
        con.execute(
            "INSERT INTO links VALUES (?,?)",
            ("alice@example.com", "kestrel-underwriting"),
        )
        con.commit()
        con.close()

    def test_a_matching_query_returns_the_fact(self):
        hits = self.api.search("analytics")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["key"], "alice@example.com")

    def test_a_hit_carries_the_slug_so_the_tab_can_link_to_the_document(self):
        self.assertEqual(self.api.search("analytics")[0]["slug"], "alice@example.com")

    def test_an_unmatched_query_returns_nothing(self):
        self.assertEqual(self.api.search("submarines"), [])

    def test_a_query_of_only_short_tokens_returns_nothing(self):
        self.assertEqual(self.api.search("a b"), [])

    def test_backlinks_resolve_through_the_slug(self):
        self.assertEqual(
            self.api.backlinks("Kestrel Underwriting"), ["alice@example.com"]
        )

    def test_a_corrupt_index_degrades_to_empty_rather_than_raising(self):
        (self.dir / self.api.INDEX_FILENAME).write_bytes(b"not a database")
        self.assertEqual(self.api.search("analytics"), [])
        self.assertEqual(self.api.backlinks("Kestrel Underwriting"), [])


class TestGraph(WikiApiTestCase):
    """The wikilink graph behind /memory/graph.

    Read out of the markdown rather than the index on purpose: the index is
    derived, disposable, and written by another container, so a graph that
    depended on it would be empty for reasons unrelated to what is recorded.
    """

    def setUp(self):
        super().setUp()
        _write(self.dir, "alice@example.com", DOC)
        _write(
            self.dir,
            "bob@example.com",
            "---\nkey: bob@example.com\ntitle: Bob Jones\n---\n\n"
            "## 2026-08-08T12:00:00Z — refresh\n- Works with [[alice@example.com]].\n",
        )

    def test_every_document_is_a_node_even_with_no_links(self):
        api = _load(self.dir / "empty")
        (self.dir / "empty").mkdir()
        _write(self.dir / "empty", "solo@example.com", "---\nkey: solo@example.com\n---\n")
        graph = api.graph()
        self.assertEqual([n["slug"] for n in graph["nodes"]], ["solo@example.com"])
        self.assertEqual(graph["edges"], [])

    def test_a_wikilink_becomes_an_edge(self):
        graph = self.api.graph()
        self.assertIn(
            {"source": "bob@example.com", "target": "alice@example.com"}, graph["edges"]
        )

    def test_a_link_to_a_document_that_does_not_exist_is_kept_as_a_missing_node(self):
        """A name referred to and never written up is a hole in the record.
        Dropping the edge would draw the store as better connected than it is."""
        graph = self.api.graph()
        kestrel = [n for n in graph["nodes"] if n["slug"] == "kestrel-underwriting"]
        self.assertEqual(len(kestrel), 1)
        self.assertTrue(kestrel[0]["missing"])

    def test_focus_keeps_the_document_and_its_one_hop_neighbourhood(self):
        graph = self.api.graph(focus="alice@example.com")
        self.assertEqual(
            sorted(n["slug"] for n in graph["nodes"]),
            ["alice@example.com", "bob@example.com", "kestrel-underwriting"],
        )

    def test_focus_traverses_undirected(self):
        """Bob links to Alice, not the other way round. "What is this connected
        to" does not care which file holds the [[...]]."""
        graph = self.api.graph(focus="alice@example.com")
        self.assertIn("bob@example.com", [n["slug"] for n in graph["nodes"]])

    def test_focus_excludes_a_document_two_hops_away_at_depth_one(self):
        _write(
            self.dir,
            "carol@example.com",
            "---\nkey: carol@example.com\n---\n\n## w — s\n- Knows [[bob@example.com]].\n",
        )
        graph = self.api.graph(focus="alice@example.com", depth=1)
        self.assertNotIn("carol@example.com", [n["slug"] for n in graph["nodes"]])
        graph = self.api.graph(focus="alice@example.com", depth=2)
        self.assertIn("carol@example.com", [n["slug"] for n in graph["nodes"]])

    def test_focus_accepts_a_key_as_well_as_a_slug(self):
        graph = self.api.graph(focus="Kestrel Underwriting")
        self.assertEqual(graph["focus"], "kestrel-underwriting")
        self.assertTrue(graph["found"])

    def test_an_unknown_focus_is_empty_rather_than_the_whole_store(self):
        graph = self.api.graph(focus="nobody@example.com")
        self.assertFalse(graph["found"])
        self.assertEqual(graph["nodes"], [])

    def test_degree_counts_links_in_both_directions(self):
        graph = self.api.graph()
        by_slug = {n["slug"]: n for n in graph["nodes"]}
        self.assertEqual(by_slug["alice@example.com"]["degree"], 2)
        self.assertEqual(by_slug["bob@example.com"]["degree"], 1)

    def test_the_graph_never_creates_an_index_file(self):
        self.api.graph()
        self.api.graph(focus="alice@example.com")
        self.assertFalse((self.dir / self.api.INDEX_FILENAME).exists())

    def test_a_missing_directory_raises_unavailable(self):
        api = _load(self.dir / "does-not-exist")
        with self.assertRaises(api.WikiUnavailable):
            api.graph()


if __name__ == "__main__":
    unittest.main()
