"""Client-facing copy: the map applied to text on the way out of this console.

Why this module exists rather than a search-and-replace somewhere
-----------------------------------------------------------------
Six naming tasks (06, 09, 10, 15, 16, 17) took the ingredient's name out of the
docs, the prompts, the console's own strings and the backend's error text. A
fresh-box run then found ``GET /api/channels`` saying *"Use Hermes from Slack
via Socket Mode"* on the page every client walks through to connect Telegram.

Both existing checks were sound and both were blind to it:

* the backend sweep read our source. The string is not in our source.
* the frontend sweep compiled our JSX and grepped it. The string is not in our
  JSX either — it arrives from the upstream gateway's channel catalog, over
  HTTP, at request time.

So the fix cannot be a literal edited somewhere; it has to be a *map applied to
the payload as it passes through*, which is what :func:`scrub` is, and a check
that reads the wire rather than the tree, which is
``tools/check_client_copy.py``. :func:`find_leaks` is the one detector both the
check and the tests use, so there is no second opinion about what a leak is.

The map and the detector are deliberately not symmetric
-------------------------------------------------------
:func:`scrub` rewrites only what has a correct one-to-one replacement in the
console's vocabulary — the ingredient's product name, which is this product's
product name to the client. It does not try to be clever about anything else,
because a wrong rewrite of a factual sentence is worse than the name: upstream's
env-var descriptions carry real scopes, real token prefixes and real gotchas,
and they are the only accurate copy anyone has for those fields.

:func:`find_leaks` is broader on purpose. It flags every name a client should
never meet — the ingredient, its authors, the toolkit — and stops. Where there
is no safe automatic rewrite, a human decides. That asymmetry is the point: the
map keeps the known class from ever shipping again, and the detector makes the
unknown class loud instead of silent.
"""

from __future__ import annotations

import re
from typing import Any, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

#: What the client calls this product. The only name that replaces another.
PRODUCT = "Steward"

# Ordered: the longest, most specific form first, so "Hermes Agent" does not
# get rewritten to "Steward Agent" by the bare-name rule underneath it.
# Possessives are their own rule because "Hermes's" -> "Steward's" needs the
# apostrophe form preserved, and both the typographic and the ASCII apostrophe
# turn up in upstream copy.
_SUBS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bHermes\s+Agent\b"), PRODUCT),
    (re.compile(r"\bHermes(?:['’]s|['’])"), f"{PRODUCT}'s"),
    (re.compile(r"\bHermes\b"), PRODUCT),
]


def scrub(text: Optional[str]) -> Optional[str]:
    """Rewrite the ingredient's name out of one piece of client-facing copy.

    ``None`` and ``""`` pass through unchanged so a caller can apply this to an
    optional field without inventing a value for it. Everything else keeps its
    facts: this changes a name, not a sentence.
    """
    if not text:
        return text
    for pattern, replacement in _SUBS:
        text = pattern.sub(replacement, text)
    return text


# --- Links -----------------------------------------------------------------

#: Hosts that belong to the ingredient rather than to this product. A URL on
#: one of these is not scrubbable — rewriting the host would produce a dead
#: link — so callers drop it or substitute a link of their own. It matters more
#: than it looks: the Channels panel renders ``docs_url`` as its own link
#: *text*, so the hostname is read by the client, not just followed.
INGREDIENT_HOSTS = frozenset(
    {
        "hermes-agent.nousresearch.com",
        "nousresearch.com",
        "www.nousresearch.com",
    }
)


def is_ingredient_url(url: Optional[str]) -> bool:
    """True if this link would show the client the ingredient's own site."""
    if not url:
        return False
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in INGREDIENT_HOSTS


# --- The detector ----------------------------------------------------------

# Names a client must never meet in this console's copy. Case-insensitive, and
# matched as substrings rather than words so a hostname
# ("hermes-agent.nousresearch.com") and an identifier embedded in prose both
# hit — a leak that hides inside a longer token is still a leak on the page.
#
# ADK is here for the same reason as the rest: principle 6 settled that the
# console calls the runtime "the runner", and ADK stays in the workflows tree,
# the image names and the code. It is a word boundary match because "adk" as a
# substring appears inside ordinary words.
LEAK_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("hermes", re.compile(r"hermes", re.I)),
    ("nous research", re.compile(r"nous\s*research|nousresearch", re.I)),
    ("teknium", re.compile(r"teknium", re.I)),
    ("adk", re.compile(r"\badk\b", re.I)),
]


def leak_name(text: str) -> Optional[str]:
    """Which forbidden name this string carries, or None."""
    for name, pattern in LEAK_PATTERNS:
        if pattern.search(text):
            return name
    return None


def walk_strings(payload: Any, path: str = "") -> Iterator[Tuple[str, str]]:
    """Every string in a JSON payload, with the path that reaches it.

    Dict *keys* are yielded too, at ``<path>.<key>#key``. A field name is not
    client-visible copy and is never rewritten — task 32 forbids it outright,
    since the frontend and the gateway both key on them — but a *new* key
    carrying a forbidden name is worth seeing, because it means the payload's
    shape changed underneath the console.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            child = f"{path}.{key}"
            if isinstance(key, str):
                yield f"{child}#key", key
            yield from walk_strings(value, child)
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            yield from walk_strings(value, f"{path}[{index}]")
    elif isinstance(payload, str):
        yield path, payload


def find_leaks(payload: Any, path: str = "") -> List[Tuple[str, str, str]]:
    """Every forbidden name in a payload, as ``(path, name, value)``.

    This is the whole definition of "leaked" used by the check tool and by the
    tests. Keeping it in one function is deliberate: a check that disagreed
    with the test about what counts would let something through while both
    reported green.
    """
    found = []
    for where, text in walk_strings(payload, path):
        name = leak_name(text)
        if name:
            found.append((where, name, text))
    return found
