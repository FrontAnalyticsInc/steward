"""Write extracted triplets into the wiki.

This replaced `graph_write.py`, which spoke streamable-HTTP MCP to a Graphiti
server to call one tool, `add_triplet`. The extraction shape is unchanged —
agents still produce `(source, edge, target)` intents with provenance — because
that shape was never the problem. Where it landed was.

A triplet maps onto a wiki line almost exactly: the subject names the document,
and the object becomes a `[[wikilink]]` to its own. That link is what makes the
one-hop neighbourhood recoverable by grep, so the relationships the graph stored
in `RELATES_TO` edges are still traversable without a graph database.

Provenance still travels with the assertion rather than as an extractor-supplied
field, for the reason graph_write gave and which has not changed: source, run id
and page URL are ambient facts the caller knows and the page text does not
contain, so offering them as schema fields invites a model to invent them.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app import integration_log, wiki

logger = logging.getLogger(__name__)

INTEGRATION_SOURCE = "wiki"

# Provenance keys carried on the assertion. Rendered into the line rather than
# dropped, because a fact whose origin cannot be named is worse than no fact.
_PROVENANCE_KEYS = ("source_ref", "source_url", "run_id", "confidence")


def _render(intent: dict[str, Any]) -> str:
    """One triplet as one wiki line, with the object as a link."""
    fact = str(intent.get("fact") or "").strip()
    target = str(intent.get("target_name") or "").strip()
    edge = str(intent.get("edge_name") or "").strip()

    if fact:
        line = fact
        # Link the target inside the sentence where it appears verbatim, so the
        # prose stays readable and the backlink still resolves. Where it does
        # not appear, append the relationship instead of rewriting the sentence.
        if target and target in line:
            line = line.replace(target, f"[[{target}]]", 1)
        elif target:
            line = f"{line} ({edge} [[{target}]])" if edge else f"{line} ([[{target}]])"
    elif target:
        source = str(intent.get("source_name") or "").strip()
        line = f"{source} {edge} [[{target}]]".strip()
    else:
        return ""

    attributes = intent.get("attributes") or {}
    trail = [
        f"{key}={attributes[key]}"
        for key in _PROVENANCE_KEYS
        if isinstance(attributes, dict) and attributes.get(key)
    ]
    return f"{line} <!-- {'; '.join(trail)} -->" if trail else line


def write_triplets(
    intents: list[dict[str, Any]],
    *,
    source_description: str = "extraction",
) -> tuple[int, int, list[str]]:
    """Append every intent to its subject's document.

    Returns (attempted, written, errors), matching the shape `org_site_ingest`
    already reports in its run contract.

    The middle value is now honestly "written", not the old "submitted": the
    graph resolved entities asynchronously inside an LLM after returning, so a
    caller could not distinguish an accepted write from a dropped one. These
    bytes are on disk before this returns.

    One failure does not abandon the rest — a single bad name should not cost a
    crawl its whole contribution.
    """
    if not intents:
        return 0, 0, []

    # Group by subject so a crawl producing twenty facts about one organisation
    # writes one dated section rather than twenty, which is what keeps the file
    # readable by the human this format exists for.
    grouped: dict[str, list[str]] = {}
    titles: dict[str, str] = {}
    attempted = 0
    errors: list[str] = []

    for intent in intents:
        attempted += 1
        subject = str(intent.get("source_name") or "").strip()
        if not subject:
            errors.append(f"triplet with no source_name: {intent.get('fact')!r}")
            continue
        line = _render(intent)
        if not line:
            errors.append(f"triplet with no fact or target for {subject!r}")
            continue
        grouped.setdefault(subject, []).append(line)
        titles.setdefault(subject, subject)

    written = 0
    for subject, lines in grouped.items():
        try:
            _, new_lines, skipped = wiki.append_section_deduped(
                subject, lines, source_description, title=titles.get(subject, "")
            )
            written += new_lines
            if skipped:
                logger.info("wiki skipped %s duplicate facts for %s", skipped, subject)
        except OSError as exc:
            errors.append(f"{subject}: {exc}")

    ok = not errors
    integration_log.record(
        INTEGRATION_SOURCE,
        "write_triplets",
        ok=ok,
        capability="write",
        error=None if ok else "; ".join(errors[:3]),
    )
    return attempted, written, errors


def write_facts(
    key: str,
    lines: list[str],
    source_description: str,
    *,
    title: str = "",
    aliases: Optional[list[str]] = None,
) -> int:
    """Append plain observations to one document. Returns lines written."""
    if not lines:
        return 0
    doc = wiki.append_section(
        key, lines, source_description, title=title, aliases=aliases
    )
    return len(doc.sections[-1].lines) if doc.sections else 0
