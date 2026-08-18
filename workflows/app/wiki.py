"""The wiki: markdown files on disk that replaced the knowledge graph.

One file per entity, keyed on a stable identifier — an Attio record id where the
CRM knows the entity, an email address otherwise. That key is the whole reason
this is more reliable than what it replaces: Graphiti resolved "Marcus Ravel" and
"M. Vale" to one entity with an LLM and no caller ever read the result back, so
identity was a guess made per-extraction. Here it is a filename.

The format is deliberately boring. Frontmatter of flat `key: value` lines, then
dated sections of bullets, each bullet one fact. Boring buys three things:

  - `grep` works, and so does a human with an editor. The file IS the record,
    not a projection of one.
  - `[[wikilink]]` between files gives the one-hop neighbourhood for free. That
    is all the dashboard's graph view ever showed (a single OPTIONAL MATCH), so
    the view survives the database being deleted.
  - The index (wiki_index.py) is derived and disposable. Delete it and it
    rebuilds; there is no migration and no second source of truth.

Frontmatter is parsed by hand rather than with PyYAML, which is only a
transitive dependency here — taking a hard dependency on it for four scalar
fields would be a poor trade. The parser is correspondingly strict: flat
`key: value`, comma-separated lists, no nesting.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

# Sections are written newest-last and read newest-first. The heading carries an
# ISO timestamp and the provenance string, because a fact whose origin cannot be
# named is worse than an absent one — that was true of the graph and is true here.
_SECTION_RE = re.compile(r"^##\s+(?P<when>\S+)\s+—\s+(?P<source>.*?)\s*$")
_BULLET_RE = re.compile(r"^[-*]\s+(?P<text>.+?)\s*$")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_TRAILING_HTML_COMMENT_RE = re.compile(r"\s*<!--.*?-->\s*$")
_FRONTMATTER_FENCE = "---"

# Filenames must survive a case-insensitive filesystem and a human reading them.
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9._@+-]+")

# One bullet is one fact, and a fact is a sentence. This is the invariant that
# was missing: with no ceiling here, a caller handing over an 1800-character
# email body stored an 1800-character "fact", and nine documents of those is
# what the store became. Enforced at the write, not at each caller, because the
# next caller will be written by someone who has not read this file.
MAX_FACT_CHARS = int(os.environ.get("WIKI_MAX_FACT_CHARS", "200"))


def wiki_dir() -> Path:
    """Where the wiki lives.

    HERMES_WIKI_DIR wins so a test or a backfill can point at a scratch tree.
    The container default is explicit for the same reason app/user_context.py
    sets one: the fallback (~/.hermes/wiki) resolves to /root inside a container
    and would silently hold nothing.
    """
    override = os.environ.get("HERMES_WIKI_DIR")
    if override:
        return Path(override)
    data_dir = os.environ.get("HERMES_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "wiki"
    return Path.home() / ".hermes" / "wiki"


def slug(key: str) -> str:
    """A stable, filesystem-safe filename stem for a key.

    Lowercased and ASCII-folded so the same entity cannot land in two files on a
    case-insensitive filesystem. Deterministic: the same key always yields the
    same stem, which is what makes the file the identity rather than a lookup.
    """
    text = unicodedata.normalize("NFKD", key.strip().lower())
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _SLUG_STRIP_RE.sub("-", text).strip("-.")
    # A key that folds away entirely (e.g. all-CJK) would collide with every
    # other such key on an empty stem. Fall back to a hex encoding of the
    # original so the file is ugly but unique and reversible.
    if not text:
        return "k-" + key.strip().lower().encode("utf-8").hex()[:48]
    return text[:120]


@dataclass
class Section:
    """One dated observation: when, where it came from, and what was learned."""

    when: str
    source: str
    lines: list[str] = field(default_factory=list)


@dataclass
class Doc:
    key: str
    title: str = ""
    aliases: list[str] = field(default_factory=list)
    last_refreshed: str = ""
    sections: list[Section] = field(default_factory=list)

    @property
    def facts(self) -> list[dict[str, str]]:
        """Every bullet, newest section first, shaped like the old graph facts.

        `valid_at`/`created_at` are both the section timestamp. The graph
        distinguished them and no workflow ever read the difference — only
        `min(valid_at, created_at)` as a staleness clock — so collapsing them
        loses nothing a caller was using.
        """
        out: list[dict[str, str]] = []
        for section in reversed(self.sections):
            for line in section.lines:
                out.append(
                    {
                        "fact": line,
                        "valid_at": section.when,
                        "created_at": section.when,
                        "source_description": section.source,
                        "key": self.key,
                    }
                )
        return out

    @property
    def links(self) -> list[str]:
        """Every [[wikilink]] target in this document, deduplicated."""
        seen: list[str] = []
        for section in self.sections:
            for line in section.lines:
                for target in _WIKILINK_RE.findall(line):
                    target = target.strip()
                    if target and target not in seen:
                        seen.append(target)
        return seen


def _parse_frontmatter(lines: list[str]) -> tuple[dict[str, str], int]:
    """Return (fields, index of first body line)."""
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return {}, 0
    fields: dict[str, str] = {}
    for index in range(1, len(lines)):
        stripped = lines[index].strip()
        if stripped == _FRONTMATTER_FENCE:
            return fields, index + 1
        if not stripped or ":" not in stripped:
            continue
        name, _, value = stripped.partition(":")
        fields[name.strip()] = value.strip()
    # Unterminated frontmatter: treat the whole file as frontmatter rather than
    # silently reinterpreting metadata as facts.
    return fields, len(lines)


def parse(text: str, key: str = "") -> Doc:
    """Parse a wiki document. Never raises — a malformed file yields what it can."""
    lines = text.splitlines()
    fields, start = _parse_frontmatter(lines)
    doc = Doc(
        key=fields.get("key", key),
        title=fields.get("title", ""),
        aliases=[a.strip() for a in fields.get("aliases", "").split(",") if a.strip()],
        last_refreshed=fields.get("last_refreshed", ""),
    )
    current: Optional[Section] = None
    for raw in lines[start:]:
        heading = _SECTION_RE.match(raw)
        if heading:
            current = Section(when=heading.group("when"), source=heading.group("source"))
            doc.sections.append(current)
            continue
        bullet = _BULLET_RE.match(raw)
        if bullet and current is not None:
            current.lines.append(bullet.group("text"))
    return doc


def render(doc: Doc) -> str:
    """Render a document back to markdown. Round-trips with parse()."""
    parts = [
        _FRONTMATTER_FENCE,
        f"key: {doc.key}",
        f"title: {doc.title}",
        f"aliases: {', '.join(doc.aliases)}",
        f"last_refreshed: {doc.last_refreshed}",
        _FRONTMATTER_FENCE,
        "",
    ]
    if doc.title:
        parts += [f"# {doc.title}", ""]
    for section in doc.sections:
        parts.append(f"## {section.when} — {section.source}")
        parts.extend(f"- {line}" for line in section.lines)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def path_for(key: str) -> Path:
    return wiki_dir() / f"{slug(key)}.md"


def normalize_fact_line(line: str) -> str:
    """Stable identity for a fact line, ignoring volatile provenance.

    Link syntax is stripped as well as the provenance comment. The same
    statement gets written both ways over a document's life — plain the first
    time an employer is mentioned, linked once that employer is an entity worth
    pointing at — and comparing the rendered text would file it twice.
    """
    without_provenance = _TRAILING_HTML_COMMENT_RE.sub("", line.strip())
    without_links = without_provenance.replace("[[", "").replace("]]", "")
    return " ".join(without_links.split()).casefold()


def truncate_fact(line: str, limit: Optional[int] = None) -> str:
    """Bound one bullet to a readable length, keeping its provenance comment.

    Truncated rather than dropped: an over-long line is usually a real fact with
    a paragraph welded to it, and the first clause is the part worth keeping.
    Cut on a word boundary so the result reads as a sentence rather than as
    corruption. The trailing comment survives intact — a fact that keeps its
    text and loses the reason to believe it is worse than one never stored.
    """
    limit = MAX_FACT_CHARS if limit is None else limit
    text = " ".join(str(line or "").split())
    match = _TRAILING_HTML_COMMENT_RE.search(text)
    comment = ""
    if match:
        comment = f" {match.group(0).strip()}"
        text = text[: match.start()].rstrip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:… ") + "…"
    return f"{text}{comment}"


def _write(doc: Doc, key: str) -> Doc:
    directory = wiki_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = path_for(key)
    # Write-then-rename: a reader (or the indexer) never sees a half-written
    # file, and a crash mid-write leaves the previous version intact.
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(render(doc), encoding="utf-8")
    temporary.replace(path)
    return doc


def read(key: str) -> Optional[Doc]:
    path = path_for(key)
    if not path.is_file():
        return None
    return parse(path.read_text(encoding="utf-8"), key=key)


def iter_docs() -> Iterator[Doc]:
    """Every document, in stable filename order."""
    directory = wiki_dir()
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.md")):
        try:
            yield parse(path.read_text(encoding="utf-8"), key=path.stem)
        except OSError:
            # A file that cannot be read is skipped rather than aborting a
            # rebuild over the whole tree.
            continue


def append_section(
    key: str,
    lines: list[str],
    source: str,
    *,
    title: str = "",
    aliases: Optional[list[str]] = None,
    when: Optional[datetime] = None,
) -> Doc:
    """Append one dated observation, creating the document if needed.

    Append-only by design. The graph re-extracted an entire prose blob on every
    refresh and produced 66 near-duplicate episodes for the same contacts;
    appending a dated section instead keeps the history legible and makes a
    duplicate obvious to a human reading the file.

    Returns the document as written.
    """
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = read(key) or Doc(key=key)
    if title:
        doc.title = title
    for alias in aliases or []:
        if alias and alias not in doc.aliases:
            doc.aliases.append(alias)
    cleaned = [truncate_fact(line) for line in lines if line and line.strip()]
    if cleaned:
        doc.sections.append(Section(when=stamp, source=source, lines=cleaned))
    doc.last_refreshed = stamp
    return _write(doc, key)


def append_section_deduped(
    key: str,
    lines: list[str],
    source: str,
    *,
    title: str = "",
    aliases: Optional[list[str]] = None,
    when: Optional[datetime] = None,
) -> tuple[Doc, int, int]:
    """Append one dated observation after skipping facts already in the doc.

    Existing and incoming lines are compared through `normalize_fact_line`, which
    strips volatile trailing provenance comments while preserving the original
    incoming text for genuinely new facts. If every incoming line is a duplicate,
    no empty section is added; `last_refreshed` still advances so a successful
    freshness check is visible without cluttering the history.

    Returns (document as written, lines written, lines skipped as duplicates).
    """
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc = read(key) or Doc(key=key)
    if title:
        doc.title = title
    for alias in aliases or []:
        if alias and alias not in doc.aliases:
            doc.aliases.append(alias)

    seen = {
        normalized
        for section in doc.sections
        for line in section.lines
        if (normalized := normalize_fact_line(line))
    }
    new_lines: list[str] = []
    skipped = 0
    for line in [truncate_fact(line) for line in lines if line and line.strip()]:
        normalized = normalize_fact_line(line)
        if not normalized:
            continue
        if normalized in seen:
            skipped += 1
            continue
        seen.add(normalized)
        new_lines.append(line)

    if new_lines:
        doc.sections.append(Section(when=stamp, source=source, lines=new_lines))
    doc.last_refreshed = stamp
    return _write(doc, key), len(new_lines), skipped
