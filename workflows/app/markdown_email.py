"""Markdown to HTML for outbound mail. Small, and deliberately not a library.

Two reasons this is hand-written rather than `markdown` or `mistune`:

**Escaping order is the security property.** The text converted here is a model's
prose about calendar invites, and the invites were written by other people. A
general markdown renderer passes raw HTML through by design — an event titled
`<img src=x onerror=...>` would arrive as live markup in Alton's inbox. So every
character is HTML-escaped FIRST, and only then are tags introduced, from a fixed
vocabulary this module controls. There is no path by which input text becomes a
tag.

**The input is narrow.** It is one model filling one schema, and it emits
headings, bullets, bold and paragraphs. Supporting exactly that is a page of
code; supporting all of CommonMark is a dependency, an image rebuild, and a much
larger surface for the escaping question above.

Inline styles rather than a stylesheet, because mail clients discard `<style>`
blocks and there is no external CSS in an email.
"""

from __future__ import annotations

import html
import re
from typing import List

# Inline styles applied per element. Gmail keeps these; it strips <style>.
# Roughly two paragraph gaps above a heading (paragraphs are 0.6em), so each
# meeting starts as a visibly separate block rather than running on from the
# previous one's prep notes. margin rather than padding: margins collapse
# predictably between blocks, and padding would show as a gap inside the
# heading's own box if a background colour is ever added.
_H = "margin:2.4em 0 0.5em;font-weight:600;line-height:1.3;"
_STYLES = {
    "h1": f"{_H}font-size:20px;",
    "h2": f"{_H}font-size:17px;",
    "h3": f"{_H}font-size:15px;",
    "p": "margin:0.6em 0;line-height:1.5;",
    "ul": "margin:0.4em 0;padding-left:1.4em;",
    # Slightly wider than ul: the marker is "10." rather than a bullet, and at
    # 1.4em the second digit hangs outside the text column.
    "ol": "margin:0.4em 0;padding-left:1.7em;",
    "li": "margin:0.25em 0;line-height:1.5;",
    "body": (
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
        "Helvetica,Arial,sans-serif;font-size:14px;color:#1a1a1a;"
        "max-width:640px;"
    ),
}

# Applied to already-escaped text, so the pattern can only ever match literal
# asterisks the author typed — never markup, which no longer exists at this point.
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")

_BULLET = re.compile(r"^(\s*)[*+-]\s+(.*)$")
# "1." and "1)" both appear in model output. Two digits is plenty for an agenda
# and stops a bare year like "2026. " from opening a list.
_ORDERED = re.compile(r"^(\s*)\d{1,2}[.)]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")


def _inline(escaped: str) -> str:
    """Bold and code spans, on text that is already HTML-escaped."""
    out = _BOLD.sub(r"<strong>\1</strong>", escaped)
    out = _CODE.sub(
        r'<code style="background:#f2f2f2;padding:1px 4px;border-radius:3px;">\1</code>',
        out,
    )
    return out


def to_html_fragment(text: str) -> str:
    """Convert a markdown-ish body to an HTML fragment.

    Block-level handling is line-based: headings, bullet and numbered lists (one
    level of nesting, which is all the briefing uses), and paragraphs. Anything
    unrecognised stays as a paragraph — the fallback is plain text, never a
    dropped line.

    Numbered lines matter more than they look. Without `<ol>` handling they fell
    through to the paragraph branch, where consecutive lines are joined with a
    space — so a three-step agenda arrived as one run-on line reading
    "1. Check in. 2. Blockers. 3. Next steps."
    """
    lines = html.escape(text or "", quote=False).splitlines()
    out: List[str] = []
    para: List[str] = []
    # Which list tag is open at each depth. A stack rather than a counter,
    # because closing has to emit the tag that was actually opened — a numbered
    # agenda nested under a bullet closes </ol> then </ul>.
    list_stack: List[str] = []

    def close_para() -> None:
        if para:
            out.append(f'<p style="{_STYLES["p"]}">' + _inline(" ".join(para)) + "</p>")
            para.clear()

    def close_lists(to_depth: int = 0) -> None:
        while len(list_stack) > to_depth:
            out.append(f"</{list_stack.pop()}>")

    def open_list(tag: str, depth: int) -> None:
        # Switching kind at the same depth — a bullet list followed by a
        # numbered one — closes the old list first, or the items would land in
        # the wrong container.
        if len(list_stack) >= depth and list_stack[depth - 1] != tag:
            close_lists(depth - 1)
        while len(list_stack) < depth:
            out.append(f'<{tag} style="{_STYLES[tag]}">')
            list_stack.append(tag)
        close_lists(depth)

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            close_para()
            close_lists()
            continue

        heading = _HEADING.match(line)
        if heading:
            close_para()
            close_lists()
            level = min(len(heading.group(1)), 3)
            tag = f"h{level}"
            out.append(f'<{tag} style="{_STYLES[tag]}">{_inline(heading.group(2))}</{tag}>')
            continue

        # Ordered before unordered: "1. x" cannot match _BULLET, but keeping the
        # pair adjacent makes the shared depth rule obvious.
        for pattern, tag in ((_ORDERED, "ol"), (_BULLET, "ul")):
            item = pattern.match(line)
            if item:
                close_para()
                # One level of nesting. Indentation in the wild is 2 or 4
                # spaces, so anything indented at all counts as nested rather
                # than guessing a width and getting it wrong on the other one.
                depth = 2 if len(item.group(1)) >= 2 else 1
                open_list(tag, depth)
                out.append(
                    f'<li style="{_STYLES["li"]}">{_inline(item.group(2))}</li>'
                )
                break
        else:
            # No list marker: ordinary prose.
            close_lists()
            para.append(line.strip())

    close_para()
    close_lists()
    return "\n".join(out)


def to_html_document(text: str, title: str = "") -> str:
    """A complete, self-contained HTML mail body."""
    return (
        "<!DOCTYPE html><html><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title or '', quote=False)}</title>"
        f'</head><body style="{_STYLES["body"]}">'
        f"{to_html_fragment(text)}"
        "</body></html>"
    )
