"""Read and refresh the wiki — what a briefing knows about a person.

This replaced a Graphiti/Neo4j knowledge graph. The function signatures are
unchanged on purpose, so `contact_context` and `calendar_daily_briefing` did not
have to move: they asked for facts about an address and got text back, which is
all they ever did with the graph.

What changed underneath is the failure mode. Graphiti answered every query with
its nearest neighbours whether or not any concerned the person asked for, so
`contact_context.relevant_facts()` existed to filter the answer with substring
matching afterwards. FTS5 has a relevance floor — no term match, no row — so
that filter is gone and the wrong-person failure it guarded against cannot
occur here.

Writes are append-only and synchronous. The graph's `POST /messages` ran an LLM
extraction after returning, so a 2xx meant "accepted" and never "the graph knows
this"; a caller could not tell a successful write from a silently dropped one.
`add_episode` here returns after the bytes are on disk, so a 'submitted' count
means what it says.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from app import integration_log, wiki, wiki_index

logger = logging.getLogger(__name__)

INTEGRATION_SOURCE = "wiki"
INTEGRATION_LABEL = "Wiki memory"

# Kept for the Integrations screen, which reports where memory is written.
WIKI_DIR = os.environ.get("HERMES_WIKI_DIR", "")

# How the caller names a document. Both callers pass `contact:<address>`; the
# prefix is stripped so the file is keyed on the address itself, which is the
# stable identifier the CRM and Gmail already agree on.
_CONTACT_PREFIX = "contact:"


def configured() -> bool:
    """True when memory has somewhere to live.

    Always true: the directory is created on first write. Retained because
    integration_config and both callers check it, and because a future backend
    with a real precondition should have somewhere to express it.
    """
    return True


def key_for(name: str) -> str:
    """The wiki key for a caller-supplied episode name."""
    name = (name or "").strip()
    if name.lower().startswith(_CONTACT_PREFIX):
        return name[len(_CONTACT_PREFIX):].strip()
    return name


def body_to_lines(body: str) -> list[str]:
    """Split an episode body into one bullet per observation.

    `contact_context.episode_body` emits `- ` bullets for CRM fields and, for
    each recent message, a three-line group whose second and third lines are
    indented. Those continuation lines are folded back onto their bullet so a
    message stays one fact — splitting it into three would index a subject line
    with no sender and read as three unrelated observations.

    Unbulleted lines ending in a colon are section headers in that format ("What
    alice@example.com has written recently, in their own words:"). They are
    dropped: a header is not an observation, and storing one gives the index a
    row that matches the person's address while asserting nothing about them —
    exactly the kind of content-free hit the wiki exists to stop returning.
    """
    lines: list[str] = []
    for raw in (body or "").splitlines():
        if not raw.strip():
            continue
        is_continuation = raw.startswith((" ", "\t")) and lines
        stripped = raw.strip()
        is_bullet = stripped.startswith(("- ", "* "))
        if is_bullet:
            stripped = stripped[2:].strip()
            is_continuation = False
        if is_continuation:
            lines[-1] = f"{lines[-1]} {stripped}"
            continue
        if not is_bullet and stripped.endswith(":"):
            continue
        lines.append(stripped)
    return lines


def search_facts(query: str, max_facts: int = 15) -> list[dict[str, Any]]:
    """Facts matching `query`, best first, newest-first within a document.

    Returns the same shape the graph did — `fact`, `valid_at`, `created_at`,
    `source_description` — so callers reading `f["fact"]` and ageing on
    `valid_at` are unaffected.

    Two sources, in this order:

    1. The document keyed on `query` itself. An exact key hit is the strongest
       signal available and needs no scoring — this is what having a stable
       filename buys, and it is why the wrong-person failure cannot recur: the
       person's own facts come from the person's own file.
    2. FTS5 matches elsewhere, for facts about them recorded under someone else.

    An empty list means nothing matched, and that is a real answer rather than a
    failure. It is also the answer the graph could not give.
    """
    limit = max(1, min(max_facts, 100))
    try:
        facts: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        own = wiki.read(query)
        if own is not None:
            for fact in own.facts:
                marker = (fact["key"], fact["fact"])
                if marker not in seen:
                    seen.add(marker)
                    facts.append(fact)

        for fact in wiki_index.search(query, limit=limit):
            marker = (fact.get("key", ""), fact["fact"])
            if marker not in seen:
                seen.add(marker)
                facts.append(fact)
    except Exception as exc:  # noqa: BLE001
        integration_log.record(
            INTEGRATION_SOURCE, "search", ok=False, capability="read", error=exc
        )
        raise
    integration_log.record(INTEGRATION_SOURCE, "search", ok=True, capability="read")
    return facts[:limit]


def add_episode(
    name: str,
    body: str,
    source_description: str,
    *,
    aliases: Optional[list[str]] = None,
    title: str = "",
) -> dict[str, Any]:
    """Append one dated observation to a document, creating it if needed.

    `source_description` is provenance and is worth writing carefully: it is the
    only thing that later explains why memory believes something, and a briefing
    that asserts a fact with no traceable origin is worse than one that says it
    does not know. It lands in the section heading, where a human reading the
    file sees it next to the facts it produced.
    """
    key = key_for(name)
    lines = body_to_lines(body)
    try:
        doc = wiki.append_section(
            key,
            lines,
            source_description,
            title=title,
            aliases=aliases,
        )
    except Exception as exc:  # noqa: BLE001
        integration_log.record(
            INTEGRATION_SOURCE, "append", ok=False, capability="write", error=exc
        )
        raise
    integration_log.record(
        INTEGRATION_SOURCE, "append", ok=True, capability="write"
    )
    return {
        "key": key,
        "path": str(wiki.path_for(key)),
        "facts_written": len(lines),
        "last_refreshed": doc.last_refreshed,
    }


def add_facts(
    name: str,
    lines: list[str],
    source_description: str,
    *,
    title: str = "",
    aliases: Optional[list[str]] = None,
) -> int:
    """Append already-distilled facts to a document. Returns lines written.

    The counterpart to `add_episode`: that one takes raw text and splits it,
    this one takes facts that are already facts. Contact refresh uses this,
    because `app/fact_distill.py` has done the extraction by the time we get
    here and splitting its output again would be nonsense.

    Deduplication belongs to `wiki.append_section_deduped` rather than to this
    module. A refresh runs monthly against a CRM record that changes rarely, so
    re-appending "is Head of Data at X" every time buries the one line that did
    change; but that is a property of the store, not of contact memory, and a
    second implementation here would be a second answer to the same question.
    """
    key = key_for(name)
    try:
        _doc, written, _skipped = wiki.append_section_deduped(
            key, lines, source_description, title=title, aliases=aliases
        )
    except Exception as exc:  # noqa: BLE001
        integration_log.record(
            INTEGRATION_SOURCE, "append", ok=False, capability="write", error=exc
        )
        raise
    # Nothing new is a normal, quiet outcome, not a failed write.
    integration_log.record(INTEGRATION_SOURCE, "append", ok=True, capability="write")
    return written


def backlinks(key: str) -> list[str]:
    """Documents that [[link]] to this one — the one-hop neighbourhood."""
    return wiki_index.backlinks(key)
