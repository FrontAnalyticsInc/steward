#!/usr/bin/env python3
"""Remove the sections written before facts were distilled, so they rebuild.

Between the knowledge graph being deleted and `app/fact_distill.py` being
written, `memory.add_episode` stored whatever it was handed, and what it was
handed was whole email bodies. Nine documents, 481 "facts", most of them
paragraphs of newsletter with section headings filed beside them as peers.

The obvious remedy — prune the bad bullets and keep the good ones — was tried
first and abandoned, and the reason is worth recording so nobody tries it again.
Length and punctuation can identify a paragraph of prose, so they remove the
worst of it, but what survives is not facts. It is shorter chatter: "See you
then!", "Same time next week?", "The Paradox of the Perfect Checklist". No
deterministic rule separates a durable fact from a short sentence, because the
difference is meaning. Pruning left documents that looked repaired and were not,
which is worse than either extreme.

So this removes whole sections by their provenance heading. A section written by
the contact-refresh writers before the fix has nothing recoverable in it; a
section written by anything else (org_site_ingest triplets, hand-authored notes)
is untouched. The next refresh finds the document stale and rewrites it through
the distiller, which is where the facts should have come from in the first
place. A document left with no sections at all is deleted, so it is recreated
cleanly rather than lingering as frontmatter with nothing under it.

Dry run by default. `--apply` writes, keeping a `.bak-<stamp>` beside each file.

    python scripts/compact_wiki.py                  # report only
    python scripts/compact_wiki.py --apply          # reset, with backups
    python scripts/compact_wiki.py --key a@b.com    # one document
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import wiki  # noqa: E402

# The provenance strings the raw-body writers stamped on their sections. Matched
# as substrings so the "(no model)" suffix the deterministic path now appends is
# covered too. Anything not listed here is left alone: this script knows which
# writer was broken and must not guess about the others.
LEGACY_SOURCES = (
    "contact refresh from Attio and Gmail",
    "calendar_daily_briefing attendee refresh",
)


def is_legacy(source: str) -> bool:
    return any(marker in (source or "") for marker in LEGACY_SOURCES)


def reset(doc: wiki.Doc) -> tuple[wiki.Doc, int, int]:
    """Return (document without legacy sections, sections dropped, facts dropped)."""
    kept = [section for section in doc.sections if not is_legacy(section.source)]
    dropped = [section for section in doc.sections if is_legacy(section.source)]
    trimmed = wiki.Doc(
        key=doc.key,
        title=doc.title,
        aliases=list(doc.aliases),
        last_refreshed=doc.last_refreshed,
        sections=kept,
    )
    return trimmed, len(dropped), sum(len(section.lines) for section in dropped)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument("--key", default="", help="only this document")
    args = parser.parse_args()

    directory = wiki.wiki_dir()
    if not directory.is_dir():
        print(f"no wiki at {directory}")
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    paths = [wiki.path_for(args.key)] if args.key else sorted(directory.glob("*.md"))
    facts_dropped = deleted = rewritten = 0

    for path in paths:
        if not path.is_file():
            print(f"missing: {path}")
            continue
        doc = wiki.parse(path.read_text(encoding="utf-8"), key=path.stem)
        trimmed, sections, facts = reset(doc)
        if not sections:
            print(f"{path.name}: nothing written by the legacy ingest, left alone")
            continue

        facts_dropped += facts
        if trimmed.sections:
            rewritten += 1
            print(f"{path.name}: dropping {sections} section(s) / {facts} facts, "
                  f"keeping {len(trimmed.sections)}")
        else:
            deleted += 1
            print(f"{path.name}: dropping {sections} section(s) / {facts} facts, "
                  "nothing left — file will be deleted and rebuilt on next refresh")

        if args.apply:
            path.with_suffix(f".md.bak-{stamp}").write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            if trimmed.sections:
                temporary = path.with_suffix(".md.tmp")
                temporary.write_text(wiki.render(trimmed), encoding="utf-8")
                temporary.replace(path)
            else:
                path.unlink()

    print(f"\n{facts_dropped} facts removed; {rewritten} document(s) trimmed, "
          f"{deleted} deleted")
    if not args.apply:
        print("dry run; nothing written. re-run with --apply")
    else:
        print(f"backups written as *.md.bak-{stamp}")
        print("the index is derived — delete .wiki-index.db to force a rebuild")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
