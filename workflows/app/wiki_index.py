"""A derived, disposable SQLite FTS5 index over the wiki.

Delete the file and it rebuilds; there is no migration, no second source of
truth, and no state here that the markdown does not already hold. That is the
same contract metrics_store.py states for its DuckDB store ("`rm metrics.duckdb`
still rebuilds the whole store"), and it is what makes the index safe to treat
as a cache rather than a database.

FTS5 rather than vectors, deliberately. This codebase twice worked around vector
search before deleting it: contact_context.relevant_facts() was a substring
filter bolted on top of Graphiti's semantic search *to fix it*, and
graph_neo4j.py chose Neo4j's fulltext index over its vector index so entity
browsing would keep working "through the embedding-dimension mismatches that
break semantic fact search". The property both wanted is a relevance FLOOR: a
query that matches nothing must return nothing. MATCH gives that for free, where
nearest-neighbour search cannot — it always has a nearest neighbour.

FTS5 ships with the stdlib sqlite3 here, so this adds no dependency and no
container.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from app import wiki

INDEX_FILENAME = ".wiki-index.db"

# Tokens shorter than this carry no signal and match far too much: an OR query
# containing "com" scores every document with an email address in it.
_MIN_TOKEN = 3
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Domain parts that identify a mail provider rather than an organisation. Kept
# in step with contact_context._GENERIC_DOMAINS, which makes the same
# distinction for the same reason.
_NOISE_TOKENS = {
    "com", "net", "org", "www", "mail", "gmail", "googlemail", "outlook",
    "hotmail", "yahoo", "icloud", "proton", "protonmail", "live", "msn",
}


def index_path() -> Path:
    """Inside the wiki directory, dot-prefixed.

    Self-contained: point HERMES_WIKI_DIR at a scratch tree and the index
    follows it, so a test or a backfill dry-run cannot corrupt the real one. The
    leading dot keeps it out of wiki.iter_docs(), which globs `*.md`.
    """
    return wiki.wiki_dir() / INDEX_FILENAME


def tokenize(text: str) -> list[str]:
    """Query tokens worth searching for, in order, deduplicated."""
    out: list[str] = []
    for token in _TOKEN_RE.findall((text or "").lower()):
        if len(token) < _MIN_TOKEN or token in _NOISE_TOKENS:
            continue
        if token not in out:
            out.append(token)
    return out


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def _create(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE VIRTUAL TABLE facts USING fts5(
            fact,
            title,
            aliases,
            key UNINDEXED,
            when_iso UNINDEXED,
            source UNINDEXED,
            tokenize = 'unicode61'
        );
        CREATE TABLE links (src TEXT NOT NULL, dst_slug TEXT NOT NULL);
        CREATE INDEX links_dst ON links (dst_slug);
        CREATE TABLE stamp (files INTEGER NOT NULL, newest REAL NOT NULL);
        """
    )


def _fingerprint() -> tuple[int, float]:
    """(file count, newest mtime) — cheap enough to check on every read."""
    directory = wiki.wiki_dir()
    if not directory.is_dir():
        return (0, 0.0)
    files = 0
    newest = 0.0
    for path in directory.glob("*.md"):
        files += 1
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return (files, newest)


def rebuild() -> int:
    """Rebuild the index from the markdown. Returns the number of facts indexed."""
    directory = wiki.wiki_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = index_path()
    # Build beside the live index and rename over it, so a concurrent reader
    # never observes a half-built index and a crash leaves the old one usable.
    temporary = path.with_suffix(".db.tmp")
    if temporary.exists():
        temporary.unlink()
    con = _connect(temporary)
    indexed = 0
    try:
        _create(con)
        rows = []
        link_rows = []
        for doc in wiki.iter_docs():
            aliases = ", ".join(doc.aliases)
            for fact in doc.facts:
                rows.append(
                    (
                        fact["fact"],
                        doc.title,
                        aliases,
                        doc.key,
                        fact["valid_at"],
                        fact["source_description"],
                    )
                )
            for target in doc.links:
                # Store the target's slug, not its literal text: [[Alice Smith]]
                # and [[alice smith]] are the same document.
                link_rows.append((doc.key, wiki.slug(target)))
        con.executemany(
            "INSERT INTO facts (fact, title, aliases, key, when_iso, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        con.executemany("INSERT INTO links (src, dst_slug) VALUES (?, ?)", link_rows)
        files, newest = _fingerprint()
        con.execute("INSERT INTO stamp (files, newest) VALUES (?, ?)", (files, newest))
        con.commit()
        indexed = len(rows)
    finally:
        con.close()
    temporary.replace(path)
    return indexed


def _is_stale(con: sqlite3.Connection) -> bool:
    try:
        row = con.execute("SELECT files, newest FROM stamp").fetchone()
    except sqlite3.DatabaseError:
        return True
    if row is None:
        return True
    return (int(row["files"]), float(row["newest"])) != _fingerprint()


def _open_fresh() -> sqlite3.Connection:
    """Open the index, rebuilding first if the markdown has moved on."""
    path = index_path()
    if not path.exists():
        rebuild()
        return _connect(path)
    con = _connect(path)
    if _is_stale(con):
        con.close()
        rebuild()
        return _connect(path)
    return con


def _match_expression(query: str) -> Optional[str]:
    """Build an FTS5 MATCH expression, or None when nothing is worth searching.

    Returning None on an empty token set is the relevance floor made explicit:
    a caller asking about an address with no searchable tokens gets nothing
    back, rather than the corpus's nearest neighbours.
    """
    tokens = tokenize(query)
    if not tokens:
        return None
    # Quoted so FTS5 treats each as a literal term rather than parsing operators
    # out of user input (an address containing OR/NOT/NEAR would otherwise
    # change the query's shape).
    return " OR ".join(f'"{token}"' for token in tokens)


def search(query: str, limit: int = 15) -> list[dict[str, Any]]:
    """Facts matching `query`, best first. Empty when nothing matches."""
    expression = _match_expression(query)
    if not expression:
        return []
    con = _open_fresh()
    try:
        rows = con.execute(
            "SELECT fact, key, when_iso, source FROM facts "
            "WHERE facts MATCH ? ORDER BY bm25(facts, 1.0, 2.0, 4.0) LIMIT ?",
            (expression, max(1, min(limit, 100))),
        ).fetchall()
    except sqlite3.DatabaseError:
        # A corrupt index is a cache problem, not a data problem: rebuild once
        # and retry rather than failing a briefing over it.
        rebuild()
        con.close()
        con = _connect(index_path())
        rows = con.execute(
            "SELECT fact, key, when_iso, source FROM facts "
            "WHERE facts MATCH ? ORDER BY bm25(facts, 1.0, 2.0, 4.0) LIMIT ?",
            (expression, max(1, min(limit, 100))),
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "fact": row["fact"],
            "key": row["key"],
            "valid_at": row["when_iso"],
            "created_at": row["when_iso"],
            "source_description": row["source"],
        }
        for row in rows
    ]


def backlinks(key: str) -> list[str]:
    """Keys of documents that [[link]] to this one — the one-hop neighbourhood.

    This is what the dashboard's Cytoscape view rendered from Neo4j: a single
    `OPTIONAL MATCH (n)-[r:RELATES_TO]-(m)`. A grep over wikilinks answers the
    same question without a graph database behind it.
    """
    target = wiki.slug(key)
    con = _open_fresh()
    try:
        rows = con.execute(
            "SELECT DISTINCT src FROM links WHERE dst_slug = ? ORDER BY src",
            (target,),
        ).fetchall()
    finally:
        con.close()
    return [row["src"] for row in rows]
