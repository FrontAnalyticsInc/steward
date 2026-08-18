"""Wiki views for the dashboard's memory tab.

This replaced `graph_api.py` (Graphiti REST) and `graph_neo4j.py` (a direct Bolt
connection for entity browsing). Both are gone with the graph.

What the tab shows is now simply the file. That is a real simplification rather
than a presentational one: the markdown IS the record, so a reader is looking at
the stored bytes instead of a projection assembled from edges and episodes. Two
services, an HTTP proxy and a Bolt driver collapse into a directory read.

Two things this deliberately keeps from the old tab:

* Search, via the FTS5 index the workflows service maintains beside the
  markdown. It is opened READ-ONLY here. The dashboard must never rebuild it —
  it runs as a different user, and a root-owned index would leave the writer
  unable to refresh its own store. A missing or stale index is reported as
  such and the tab still lists documents.

* The one-hop neighbourhood, which was the only genuinely graph-shaped thing
  the old tab did (a single `OPTIONAL MATCH (n)-[r:RELATES_TO]-(m)`). Backlinks
  over `[[wikilink]]` answer the same question, so the view survives the
  database being deleted.

There are no mutation routes. The old module gated deletes behind
GRAPH_ALLOW_DELETE because uvicorn binds 0.0.0.0:9120 with no auth and a delete
cost ~3 minutes of GPU re-ingestion to undo. Here the equivalent operation is
`rm` on a file that is the source of truth, so it is not exposed at all.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

WIKI_DIR = Path(os.getenv("HERMES_WIKI_DIR", "/opt/data/wiki"))
INDEX_FILENAME = ".wiki-index.db"

_SECTION_RE = re.compile(r"^##\s+(?P<when>\S+)\s+—\s+(?P<source>.*?)\s*$")
_BULLET_RE = re.compile(r"^[-*]\s+(?P<text>.+?)\s*$")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9._@+-]+")


class WikiUnavailable(RuntimeError):
    """The wiki directory is missing or unreadable."""


def _slug(key: str) -> str:
    """Mirror of app/wiki.slug for resolving link targets to filenames."""
    text = _SLUG_STRIP_RE.sub("-", key.strip().lower()).strip("-.")
    return text[:120]


def _index_path() -> Path:
    return WIKI_DIR / INDEX_FILENAME


def _open_index() -> Optional[sqlite3.Connection]:
    """Read-only handle on the index, or None when there isn't a usable one.

    Read-only is load-bearing, not caution: sqlite creates a missing file on a
    normal connect, and this process runs as a different user than the writer.
    A dashboard-created index would be owned by the wrong user and the workflows
    container could no longer refresh it.
    """
    path = _index_path()
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.DatabaseError:
        return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WikiUnavailable(str(exc)) from exc


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if ":" in stripped:
            name, _, value = stripped.partition(":")
            fields[name.strip()] = value.strip()
    return fields


def health() -> dict[str, Any]:
    """Enough for the tab's empty state to say something true."""
    present = WIKI_DIR.is_dir()
    documents = len(list(WIKI_DIR.glob("*.md"))) if present else 0
    index = _index_path()
    facts = 0
    con = _open_index()
    if con is not None:
        try:
            facts = int(con.execute("SELECT count(*) FROM facts").fetchone()[0])
        except sqlite3.DatabaseError:
            facts = 0
        finally:
            con.close()
    return {
        "status": "ok" if present else "unavailable",
        "directory": str(WIKI_DIR),
        "documents": documents,
        "facts": facts,
        # Reported rather than repaired. The workflows service owns the index;
        # a dashboard that rebuilt it would be writing as the wrong user.
        "index_present": index.is_file(),
    }


def documents(limit: int = 100, q: str = "") -> list[dict[str, Any]]:
    """Documents newest-first, optionally filtered by a substring of the title."""
    if not WIKI_DIR.is_dir():
        raise WikiUnavailable(f"{WIKI_DIR} does not exist")
    needle = (q or "").strip().lower()
    out: list[dict[str, Any]] = []
    for path in WIKI_DIR.glob("*.md"):
        try:
            stat = path.stat()
        except OSError:
            continue
        fields = _frontmatter(_read(path))
        key = fields.get("key", path.stem)
        title = fields.get("title") or key
        if needle and needle not in title.lower() and needle not in key.lower():
            continue
        out.append(
            {
                "key": key,
                "slug": path.stem,
                "title": title,
                "aliases": [a.strip() for a in fields.get("aliases", "").split(",") if a.strip()],
                "last_refreshed": fields.get("last_refreshed", ""),
                "modified": stat.st_mtime,
            }
        )
    out.sort(key=lambda row: row["modified"], reverse=True)
    return out[: max(1, min(limit, 1000))]


def document(slug: str) -> Optional[dict[str, Any]]:
    """One document: its raw markdown, its parsed facts, and its neighbourhood."""
    path = WIKI_DIR / f"{_slug(slug)}.md"
    if not path.is_file():
        return None
    text = _read(path)
    fields = _frontmatter(text)
    key = fields.get("key", path.stem)

    sections: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for line in text.splitlines():
        heading = _SECTION_RE.match(line)
        if heading:
            current = {
                "when": heading.group("when"),
                "source": heading.group("source"),
                "lines": [],
            }
            sections.append(current)
            continue
        bullet = _BULLET_RE.match(line)
        if bullet and current is not None:
            current["lines"].append(bullet.group("text"))

    links: list[str] = []
    for section in sections:
        for line in section["lines"]:
            for target in _WIKILINK_RE.findall(line):
                if target.strip() and target.strip() not in links:
                    links.append(target.strip())

    return {
        "key": key,
        "slug": path.stem,
        "title": fields.get("title") or key,
        "aliases": [a.strip() for a in fields.get("aliases", "").split(",") if a.strip()],
        "last_refreshed": fields.get("last_refreshed", ""),
        # The file itself, so the tab can show the record rather than a
        # reassembly of it.
        "markdown": text,
        "sections": list(reversed(sections)),
        "links_out": links,
        "links_in": backlinks(key),
    }


def search(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Facts matching `query`. Empty when the index is absent or nothing matches."""
    terms = [t for t in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(t) >= 3]
    if not terms:
        return []
    con = _open_index()
    if con is None:
        return []
    expression = " OR ".join(f'"{term}"' for term in terms)
    try:
        rows = con.execute(
            "SELECT fact, key, when_iso, source FROM facts "
            "WHERE facts MATCH ? ORDER BY bm25(facts, 1.0, 2.0, 4.0) LIMIT ?",
            (expression, max(1, min(limit, 200))),
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        con.close()
    return [
        {
            "fact": row["fact"],
            "key": row["key"],
            "slug": _slug(row["key"]),
            "valid_at": row["when_iso"],
            "source": row["source"],
        }
        for row in rows
    ]


def backlinks(key: str) -> list[str]:
    """Documents linking here — the one-hop neighbourhood."""
    con = _open_index()
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT DISTINCT src FROM links WHERE dst_slug = ? ORDER BY src",
            (_slug(key),),
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        con.close()
    return [row["src"] for row in rows]


# --- The graph ---
# Edges are read out of the markdown, not out of the index. The index is
# derived and disposable by design (see the module docstring), and it is
# written by another container — so a dashboard that could only draw a graph
# when the index happened to be fresh would show an empty one for reasons that
# have nothing to do with what is recorded. Parsing six to a few hundred small
# files on demand is cheaper than the round trip that would keep the two
# honest, and it is the same parse `document()` already does.


def _all_documents() -> list[tuple[Path, dict[str, str], str]]:
    """(path, frontmatter, text) for every document, once."""
    if not WIKI_DIR.is_dir():
        raise WikiUnavailable(f"{WIKI_DIR} does not exist")
    out = []
    for path in sorted(WIKI_DIR.glob("*.md")):
        try:
            text = _read(path)
        except WikiUnavailable:
            continue
        out.append((path, _frontmatter(text), text))
    return out


def graph(focus: str = "", depth: int = 1) -> dict[str, Any]:
    """Nodes and wikilink edges, optionally cut to one document's neighbourhood.

    `focus` is a slug or a key; `depth` is how many hops out from it to keep,
    traversed undirected — "what is this connected to" does not care which file
    holds the `[[...]]`. Without a focus the whole store is returned.

    A link whose target has no file of its own is still an edge, to a node
    marked `missing`. Dropping it would silently redraw the graph as more
    connected than the records are: a name that is referred to and never
    written up is a real hole, and the view should show it as one.
    """
    docs = _all_documents()
    nodes: dict[str, dict[str, Any]] = {}
    for path, fields, _text in docs:
        key = fields.get("key", path.stem)
        nodes[path.stem] = {
            "slug": path.stem,
            "key": key,
            "title": fields.get("title") or key,
            "last_refreshed": fields.get("last_refreshed", ""),
            "missing": False,
        }

    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    for path, _fields, text in docs:
        for target in _WIKILINK_RE.findall(text):
            target = target.strip()
            if not target:
                continue
            dst = _slug(target)
            if not dst or dst == path.stem:
                continue
            if dst not in nodes:
                nodes[dst] = {
                    "slug": dst,
                    "key": target,
                    "title": target,
                    "last_refreshed": "",
                    "missing": True,
                }
            pair = (path.stem, dst)
            if pair in seen_edges:
                continue
            seen_edges.add(pair)
            edges.append({"source": path.stem, "target": dst})

    focus_slug = _slug(focus) if focus else ""
    if focus_slug and focus_slug in nodes:
        adjacency: dict[str, set[str]] = {slug: set() for slug in nodes}
        for edge in edges:
            adjacency[edge["source"]].add(edge["target"])
            adjacency[edge["target"]].add(edge["source"])
        keep = {focus_slug}
        frontier = {focus_slug}
        for _hop in range(max(1, min(depth, 6))):
            frontier = {n for f in frontier for n in adjacency[f]} - keep
            if not frontier:
                break
            keep |= frontier
        nodes = {slug: node for slug, node in nodes.items() if slug in keep}
        edges = [e for e in edges if e["source"] in keep and e["target"] in keep]
    elif focus_slug:
        # Named a document that is not here. An empty graph is the honest
        # answer — better than quietly widening to the whole store, which
        # would look like the focus was ignored rather than not found.
        return {"focus": focus_slug, "depth": depth, "found": False, "nodes": [], "edges": []}

    for node in nodes.values():
        node["degree"] = sum(
            1 for e in edges if e["source"] == node["slug"] or e["target"] == node["slug"]
        )
    return {
        "focus": focus_slug or None,
        "depth": depth if focus_slug else None,
        "found": True,
        "nodes": sorted(nodes.values(), key=lambda n: (-n["degree"], n["title"].lower())),
        "edges": edges,
    }
