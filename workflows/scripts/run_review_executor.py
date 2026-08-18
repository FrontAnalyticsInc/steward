#!/usr/bin/env python3
"""Watch approved/ and perform what was approved.

This process is the only thing that acts on a human decision, and it is
deliberately on the reviewer's side of the queue's boundary:

    producer   writes pending/ only          (cannot approve, cannot execute)
    dashboard  writes approved/ on keystroke (the only writer of approved/)
    executor   reads approved/ READ-ONLY     (cannot approve its own work)

The read-only mount is the enforcement. An executor that could write approved/
could approve an item and then execute it, which would make the whole gate
theater in exactly the way the pending-only producer mount was built to prevent.

Why a poller rather than doing this inside the dashboard's request handler:

  The dashboard has no Google credentials and should not get them — it serves an
  HTTP surface on 0.0.0.0, and a mail-sending credential behind that is the
  thing an unauthenticated route would hand out.

  A Gmail call with backoff can take tens of seconds. On a request thread that
  is a hung Approve button, and a gateway timeout leaves the reviewer unable to
  tell "sent" from "still trying" — which for a send is the one distinction that
  matters.

  Restarting mid-flight is survivable: state is the directory a file is in, so
  recovery is a directory listing.

At-most-once, which is the whole point for `send`:

  1. Claim with O_CREAT|O_EXCL into executing/. Two executors race, exactly one
     creates the file, the loser moves on. This is the lock.

  2. approved/ is never deleted (it cannot be — read-only), so it is the ledger.
     A file already present in executing/, executed/ or failed/ is already
     claimed and is skipped forever after. Restart cannot re-send.

  3. A claim older than the lease is surfaced as failed(stalled) and is NEVER
     retried automatically. A stall between "Gmail accepted the send" and "we
     recorded it" is indistinguishable from a stall before the send, so the safe
     reading of an ambiguous send is that it went out. A human decides.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import review_executors, run_trace  # noqa: E402
from app.review_executors import ExecutionError  # noqa: E402
from app.run_metrics import Produced, RunMetrics, Touched  # noqa: E402

logger = logging.getLogger("review_executor")

# The name this process reports under in the metrics store. It sits beside the
# `app.agents.*` traces because at that grain it is the same kind of thing: work
# that started, took time and ended in a status. It is not an agent and calls no
# model, which is why its traces carry no token or cost fields.
TRACE_APP = "app.review_executor"

APPROVAL_ROOT = Path(os.getenv("APPROVALS_ROOT", "/approvals"))
APPROVED_DIR = APPROVAL_ROOT / "approved"
EXECUTING_DIR = APPROVAL_ROOT / "executing"
EXECUTED_DIR = APPROVAL_ROOT / "executed"
FAILED_DIR = APPROVAL_ROOT / "failed"

POLL_SECONDS = int(os.getenv("REVIEW_EXECUTOR_POLL", "10"))
LEASE_SECONDS = int(os.getenv("REVIEW_EXECUTION_LEASE", "600"))

# Liveness for the compose healthcheck, which otherwise has nothing to ask: this
# process serves no port, so "is it up?" could only be answered by "is the pid
# there?" — and a wedged poll loop keeps its pid.
#
# Written after every pass, including a pass that raised. That is deliberate and
# matches the loop below: a bad pass is survivable by design and the process is
# genuinely alive, so failing health on it would restart a container that is
# working. What this detects is the loop not coming round at all.
#
# Under EXECUTING_DIR rather than the writable-but-shared root: it is the mount
# this container already owns read-write on every deployment.
HEARTBEAT_PATH = EXECUTING_DIR / ".heartbeat"

# Same reason as everywhere else in this queue: three containers, three uids.
ITEM_MODE = 0o644


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(item: dict, dest_dir: Path, name: str) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(item, fh, indent=2)
        os.chmod(tmp, ITEM_MODE)
        os.replace(tmp, dest_dir / name)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _claim(name: str) -> bool:
    """Take exclusive ownership of one item. False if someone already has it."""
    EXECUTING_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(
            str(EXECUTING_DIR / name), os.O_CREAT | os.O_EXCL | os.O_WRONLY, ITEM_MODE
        )
    except FileExistsError:
        return False
    os.close(fd)
    return True


def _already_handled(name: str) -> bool:
    return any((d / name).exists() for d in (EXECUTING_DIR, EXECUTED_DIR, FAILED_DIR))


def _finish(item: dict, name: str, dest: Path) -> None:
    _write(item, dest, name)
    (EXECUTING_DIR / name).unlink(missing_ok=True)


def _record_trace(
    item: dict,
    *,
    executor: str | None,
    status: str,
    began: datetime,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    """Report this execution to the metrics store.

    Counted here rather than at queue time because this is where the side effect
    became real. A failed execution still writes a record — it did work and it
    ended in a status — but produces nothing: a send that raised must never be
    counted as mail that went out.
    """
    produced: dict[str, int] = {}
    touched: dict[str, int] = {}
    if status == "ok" and executor:
        kind = review_executors.PRODUCED_BY_ACTION.get(executor)
        if kind:
            produced[kind] = 1
            touched[Touched.email] = 1
        elif executor == "apply_labels":
            touched[Touched.email] = len((result or {}).get("labeled_message_ids") or [])

    began_utc = began.astimezone(timezone.utc)
    duration_ms = int(
        (datetime.now(timezone.utc) - began_utc).total_seconds() * 1000
    )
    run_trace.record(
        TRACE_APP,
        # Keyed by the item, not the attempt: a retry replaces its earlier record
        # rather than counting the same send twice.
        f"review:{item.get('id')}",
        status=status,
        started_at=began_utc,
        duration_ms=duration_ms,
        # What the reviewer chose is the interesting axis here, and it is not a
        # payload — it is one of a fixed set of action names.
        metrics=RunMetrics(
            touched={Touched(k): v for k, v in touched.items() if v},
            produced={Produced(k): v for k, v in produced.items()},
            extra={f"action_{executor}": 1} if executor else {},
        ).model_dump_trace(),
        trigger="review_approval",
        error=error,
        attempt=int((item.get("execution") or {}).get("attempts") or 1),
    )


def process_one(name: str) -> str | None:
    """Execute a single approved item. Returns the terminal state, or None if skipped."""
    source = APPROVED_DIR / name
    try:
        item = json.loads(source.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("could not read %s", name)
        return None

    execution = item.get("execution") or {}
    if execution.get("state") not in ("queued", "running"):
        return None
    if _already_handled(name):
        return None
    if not _claim(name):
        return None

    began = datetime.now(timezone.utc)
    executor = execution.get("executor")
    execution["state"] = "running"
    execution["started_at"] = _now()
    execution["attempts"] = int(execution.get("attempts") or 0) + 1
    item["execution"] = execution
    _write(item, EXECUTING_DIR, name)

    try:
        result = review_executors.run(item)
    except ExecutionError as exc:
        execution.update(
            state="failed",
            finished_at=_now(),
            error={"kind": exc.kind, "message": str(exc), "retryable": exc.retryable},
        )
        item["execution"] = execution
        _finish(item, name, FAILED_DIR)
        _record_trace(
            item, executor=executor, status="failed", began=began, error=str(exc)
        )
        logger.warning("execution failed for %s: %s", item.get("id"), exc)
        return "failed"
    except Exception as exc:  # noqa: BLE001
        # An unexpected failure from a send is the dangerous case: we do not know
        # whether it left. Mark it non-retryable so nobody can turn one uncertain
        # message into two certain ones with a button.
        retryable = execution.get("executor") in review_executors.RETRYABLE
        execution.update(
            state="failed",
            finished_at=_now(),
            error={
                "kind": type(exc).__name__,
                "message": str(exc),
                "retryable": retryable,
            },
        )
        item["execution"] = execution
        _finish(item, name, FAILED_DIR)
        _record_trace(
            item, executor=executor, status="failed", began=began, error=str(exc)
        )
        logger.exception("execution errored for %s", item.get("id"))
        return "failed"

    execution.update(state="done", finished_at=_now(), result=result)
    item["execution"] = execution
    _finish(item, name, EXECUTED_DIR)
    _record_trace(item, executor=executor, status="ok", began=began, result=result)
    logger.info("executed %s (%s)", item.get("id"), execution.get("executor"))
    return "done"


def sweep_stalled() -> int:
    """Surface claims that never finished. Never re-runs them."""
    moved = 0
    if not EXECUTING_DIR.is_dir():
        return moved
    for path in sorted(EXECUTING_DIR.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        execution = item.get("execution") or {}
        started = execution.get("started_at") or ""
        try:
            when = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if (datetime.now(timezone.utc) - when).total_seconds() <= LEASE_SECONDS:
            continue
        execution.update(
            state="failed",
            finished_at=_now(),
            error={
                "kind": "stalled",
                "message": (
                    "Claimed but never finished. Not retried automatically: a "
                    "stall around a send cannot be told apart from a send that "
                    "succeeded."
                ),
                "retryable": False,
            },
        )
        item["execution"] = execution
        _write(item, FAILED_DIR, path.name)
        path.unlink(missing_ok=True)
        # Deliberately produces nothing, even though a stalled `send` may well
        # have gone out. The count says what is known to have happened; an
        # ambiguous send is surfaced for a human on the Review screen, which is
        # the right place to resolve it, rather than guessed at in a total.
        _record_trace(
            item,
            executor=execution.get("executor"),
            status="failed",
            began=when,
            error="stalled",
        )
        moved += 1
    return moved


def tick() -> dict:
    counts = {"done": 0, "failed": 0, "stalled": sweep_stalled()}
    if not APPROVED_DIR.is_dir():
        return counts
    for path in sorted(APPROVED_DIR.glob("*.json")):
        outcome = process_one(path.name)
        if outcome in counts:
            counts[outcome] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once", action="store_true", help="run a single pass and exit"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    for d in (EXECUTING_DIR, EXECUTED_DIR, FAILED_DIR):
        d.mkdir(parents=True, exist_ok=True)

    if args.once:
        print(json.dumps(tick()))
        return 0

    logger.info("watching %s every %ss", APPROVED_DIR, POLL_SECONDS)
    while True:
        try:
            counts = tick()
            if any(counts.values()):
                logger.info("pass: %s", counts)
        except Exception:  # noqa: BLE001
            # A bad pass must not take the process down; the next one re-reads
            # the directory and the queue is unharmed either way.
            logger.exception("executor pass failed")
        try:
            HEARTBEAT_PATH.touch()
        except OSError:
            # Never fatal. An unwritable heartbeat means the healthcheck goes
            # stale and the container is reported unhealthy, which is the
            # correct outcome to surface — but the queue itself is still being
            # drained, and killing that to report a monitoring problem would
            # trade a working executor for a tidier status column.
            logger.warning("could not write heartbeat at %s", HEARTBEAT_PATH)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
