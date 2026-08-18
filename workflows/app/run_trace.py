"""Write a run trace for work that no invocation wrapped.

Every ADK pipeline gets a trace record for free: `invoke_workflow` writes one per
invocation, and the dashboard's whole metrics store is built on reading them. The
review executor gets nothing, because nothing invokes it — it is a daemon that
wakes up, notices a decision a human made minutes ago, and carries it out. Its
work is real and its output is the most consequential thing this system produces
(mail that actually leaves), and until this module existed none of it was counted.

So the executor writes its own record, in the same shape and the same place:

    ${ADK_STATE_DIR}/traces/<app>/<YYYY-MM-DD>.jsonl

That location is not an implementation detail to be tidied up later — it is the
interface. `metrics_store` globs `traces/*/*.jsonl` with `union_by_name`, so a
new app appears in `fact_activity`, the scorecard and the produced-by-kind
totals with no schema migration, no reader change and no registration step. The
one thing that must be right is the field names, which is why this module writes
the same keys `invoke_workflow` does rather than a shape of its own.

**One record per unit of work**, keyed by the review item, not one per polling
pass. A pass that found nothing did nothing and should leave no trace; writing an
empty record every ten seconds would bury the real ones and inflate the run count
of the whole fleet. Retrying an item reuses its `run_id` on purpose: the store
partitions by `run_id` and keeps the newest `started_at`, so a retried execution
replaces its earlier record instead of counting the same send twice — the same
rule `invoke_workflow` relies on for its own retries.

Two rules, both inherited from `integration_log`, which solves this same problem
one level down:

  It never raises. A bookkeeping failure that stopped a send from being recorded
  as finished — or worse, killed the executor mid-flight — would be far more
  damaging than a missing row on a metrics screen.

  It never records a payload. Recipients, subjects and bodies stay out. These
  files are read by an unauthenticated LAN-reachable dashboard; the item id is
  the handle, and the queue itself holds the content.

Absent is not zero, the same as everywhere else in `run_metrics`: this writes no
token or cost fields at all, because the executor makes no model calls. A NULL
there reads as "not measured", which is honest, where a 0.0 would claim a
measurement of a thing that never happened.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Same mount and same default as integration_log, so a deployment moves the two
# together. The dashboard reads this tree through its own ~/.hermes mount.
STATE_DIR = os.environ.get("ADK_STATE_DIR", "/code/adk-state")

# Matches what invoke_workflow writes today. The store projects fields by name
# and tolerates their absence, so this is about being read correctly, not about
# passing a validator.
TRACE_VERSION = 3

# The executor runs as root while the dashboard that reads these runs as another
# uid, so the mode is set explicitly rather than left to whatever umask the
# container happens to have. The same rule the queue items follow, for the same
# reason: a 0600 record is one the screen cannot show anyone.
FILE_MODE = 0o644
DIR_MODE = 0o755

_lock = threading.Lock()


def record(
    app: str,
    run_id: str,
    *,
    status: str,
    started_at: datetime,
    duration_ms: int,
    metrics: Optional[dict] = None,
    trigger: str = "unknown",
    error: Optional[str] = None,
    attempt: int = 1,
) -> None:
    """Append one run record. Never raises.

    `metrics` is a `RunMetrics.model_dump_trace()` dict — `touched`, `produced`
    and `extra` — and is what makes the run show up in the produced-by-kind
    totals rather than only as an activity that took some time.
    """
    entry = {
        "run_id": run_id,
        "app": app,
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
        "duration_ms": int(duration_ms),
        "status": status,
        "attempt": int(attempt),
        "trigger": trigger,
        "error": error,
        "trace_version": TRACE_VERSION,
        # A true measurement, not a placeholder: this app calls no model at all.
        # Token and cost fields are omitted entirely so they read as unmeasured.
        "model_calls": 0,
    }
    if metrics is not None:
        entry["metrics"] = metrics

    try:
        directory = os.path.join(STATE_DIR, "traces", app)
        os.makedirs(directory, mode=DIR_MODE, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(directory, f"{day}.jsonl")
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _lock:
            is_new = not os.path.exists(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            if is_new:
                os.chmod(path, FILE_MODE)
    except Exception as exc:  # never let bookkeeping break the work
        logger.debug("run_trace: could not record %s/%s: %s", app, run_id, exc)
