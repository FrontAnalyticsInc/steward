"""Which model each alias resolves to.

Workflows name a *role* — `drafting`, `extraction`, `fast` — and never a vendor
model string. Swapping a model is then one line in one file, and no agent module
changes.

That indirection is the piece worth keeping from the proxy we dropped. The proxy
also supplied it, but at 1.06 GB of resident memory and a Postgres dependency;
here it costs a dict.

The file lives under `HERMES_DATA_DIR/config/` so the operator console can edit
it — the dashboard already mounts that tree read-write, and already drives
Hermes's own model settings through `PUT /api/config`. One screen, two files.

Deliberately NOT in the image: a model choice you have to rebuild to change is
not a configuration value.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ALIASES_PATH = os.environ.get("MODEL_ALIASES_PATH", "/code/config/model-aliases.yaml")

# Used when the file is absent or unreadable. Not a fallback so much as the
# answer to "what should a fresh install do" — an alias that resolves to
# nothing would fail every workflow at dispatch, which is a worse failure than
# running on a sensible default and saying so in the log.
#
# `provider/model` is LiteLLM's addressing, and the prefix is load-bearing
# downstream: the metrics store reads the vendor out of it for cost attribution.
DEFAULTS: Dict[str, str] = {
    # Open-ended judgement, adversarial input, anything user-facing.
    "drafting": "anthropic/claude-opus-5",
    # Structured extraction and classification: bounded, schema-shaped work.
    "extraction": "anthropic/claude-sonnet-5",
    # Titles, summaries, tag generation — not intelligence-sensitive.
    "fast": "anthropic/claude-haiku-4-5",
}

_lock = threading.RLock()
_cache: Optional[Dict[str, str]] = None
_cache_stamp: Optional[tuple] = None


def _parse(text: str) -> Dict[str, str]:
    """Parse `alias: model` lines.

    Hand-rolled rather than PyYAML, which is only a transitive dependency here —
    the file is a flat string-to-string map and taking a hard dependency for it
    would be a poor trade. Same reasoning as app/wiki.py's frontmatter.
    """
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        alias, _, model = line.partition(":")
        alias, model = alias.strip(), model.strip().strip("\"'")
        if alias and model:
            out[alias] = model
    return out


def _stamp(path: Path) -> Optional[tuple]:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load(force: bool = False) -> Dict[str, str]:
    """The alias map, re-read when the file changes.

    Keyed on (mtime, size) like app/user_context.py, so an edit from the
    dashboard takes effect without restarting the service — which is the point
    of the file being outside the image.
    """
    global _cache, _cache_stamp
    path = Path(ALIASES_PATH)
    stamp = _stamp(path)
    with _lock:
        if not force and _cache is not None and stamp == _cache_stamp:
            return dict(_cache)

        resolved = dict(DEFAULTS)
        if stamp is None:
            logger.info(
                "model aliases: %s absent, using built-in defaults (%s)",
                ALIASES_PATH,
                ", ".join(f"{k}={v}" for k, v in DEFAULTS.items()),
            )
        else:
            try:
                # Unknown aliases are kept, not dropped: an operator adding
                # `reranking: ...` should not need a code change for a workflow
                # to reference it.
                resolved.update(_parse(path.read_text(encoding="utf-8")))
            except OSError as exc:
                logger.warning("model aliases: could not read %s (%s); using defaults",
                               ALIASES_PATH, exc)

        _cache, _cache_stamp = resolved, stamp
        return dict(resolved)


def resolve(alias: str) -> str:
    """The model string for an alias.

    An unknown alias raises rather than falling back. A typo silently answered
    by the drafting model is a cost and quality bug that would surface as a
    surprising invoice weeks later.
    """
    aliases = load()
    if alias in aliases:
        return aliases[alias]
    raise KeyError(
        f"unknown model alias {alias!r}; defined: {', '.join(sorted(aliases))}. "
        f"Add it to {ALIASES_PATH} or use one of those."
    )


def describe() -> Dict[str, Any]:
    """For the dashboard and for logs: what is in force, and from where."""
    path = Path(ALIASES_PATH)
    return {
        "path": ALIASES_PATH,
        "present": path.is_file(),
        "aliases": load(),
        "defaults": dict(DEFAULTS),
    }
