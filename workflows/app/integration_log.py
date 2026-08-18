"""Append-only record of every outbound call this project makes.

The dashboard's Integrations screen answers "can each part of the system still
reach what it needs, and when did it last do so?". That question can only be
answered honestly from the outcome of real calls: a synthetic ping proves a
credential authenticates, but not that the scope you need is still granted —
and a revoked scope is the failure that actually happens. So the code making
the call is the code that says how it went, and this module is where it says it.

One JSON object per line, one file per UTC day, in `INTEGRATION_CALL_LOG_DIR`:

    {"at": 1785994646.3, "source": "gmail", "consumer": "gmail_inbox_triage",
     "capability": "read", "operation": "messages.list", "ok": true,
     "error": null, "run_id": null}

`source` is the system reached, and must match the key the dashboard groups by
(`gmail`, `wiki`, …). `consumer` is who reached it — set once per pipeline
with `consumer_scope()` so individual call sites do not have to thread it
through. `capability` is the scope class being exercised, which is what makes
"exactly one consumer can send mail" readable off the screen.

Two rules this module keeps absolutely:

  It never raises. A logging failure that broke a triage run would be a strictly
  worse outcome than a missing row on a status screen, so every path here
  swallows its own errors.

  It never records a payload. Subjects, bodies, addresses and credentials stay
  out; `operation` is an API method name and `error` is trimmed. The log is
  read by an unauthenticated LAN-reachable dashboard.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# Default matches the dashboard's INTEGRATION_CALL_LOG_DIR, which reads this
# directory through its own ~/.hermes mount. Both sides take it from the
# environment so a deployment can move it in one place.
LOG_DIR = os.environ.get("INTEGRATION_CALL_LOG_DIR", "/code/adk-state/integration-calls")

# Long errors are usually a stack of provider prose wrapped around one useful
# clause ("insufficient authentication scopes"). The screen shows the error
# inline on a failed row, so it has to stay one line.
MAX_ERROR_CHARS = 300

_consumer: contextvars.ContextVar[str] = contextvars.ContextVar(
    "integration_consumer", default="workflows"
)
_run_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "integration_run_id", default=None
)

# Appends are single-line and O_APPEND, so concurrent writers interleave safely
# at the OS level; the lock only keeps this process from interleaving with
# itself when a pipeline fans out.
_lock = threading.Lock()


@contextlib.contextmanager
def consumer_scope(consumer: str, run_id: Optional[str] = None) -> Iterator[None]:
    """Attribute every call made inside this block to one consumer.

    A context manager rather than an argument on every call site, because the
    consumer is a property of the pipeline, not of the individual API method —
    and because `asyncio.to_thread` copies the context, so a stage can set it
    once and the synchronous client code inside the thread inherits it.
    """
    consumer_token = _consumer.set(consumer)
    run_token = _run_id.set(run_id)
    try:
        yield
    finally:
        _consumer.reset(consumer_token)
        _run_id.reset(run_token)


def current_consumer() -> str:
    return _consumer.get()


def _trim(error: object) -> Optional[str]:
    if error is None:
        return None
    text = " ".join(str(error).split())
    return text[:MAX_ERROR_CHARS] if text else None


def record(
    source: str,
    operation: str,
    ok: bool,
    capability: Optional[str] = None,
    error: object = None,
    consumer: Optional[str] = None,
) -> None:
    """Note that one outbound call happened and how it ended.

    Called on both paths deliberately. A screen that only logs successes cannot
    distinguish "working" from "has not run", which is the distinction the whole
    thing exists to make.
    """
    entry = {
        "at": time.time(),
        "source": source,
        "consumer": consumer or _consumer.get(),
        "capability": capability,
        "operation": operation,
        "ok": bool(ok),
        "error": _trim(error),
        "run_id": _run_id.get(),
    }
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(LOG_DIR, f"{day}.jsonl")
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as exc:  # never let bookkeeping break the pipeline
        logger.debug("integration_log: could not record %s/%s: %s", source, operation, exc)
