"""How well are the automations working, and how well does fixing them work?

`cron_watchdog` answers "is anything broken right now" and files a card when
something is. That is the alarm. This module is the ledger behind it: which
jobs fail most, whether the cards the watchdog files actually reach `done`, and
how long the round trip takes. An alarm with no ledger tells you the system is
sick today but never whether it is getting better, and "is it getting better"
is the only question that changes what you build next.

It lives beside the watchdog, in the dashboard container, for the reason the
watchdog documents at length: a reporter that runs inside cron cannot report
the failures that kill cron. Everything here is read-only for the same reason
in reverse — the watchdog owns card-filing, and two writers racing on the same
idempotency key is exactly how a completed task suppresses every future
recurrence of a real problem.

Two measurement rules, borrowed from the modules either side of this one:

* Never report an uninstrumented field as a measurement (`adk_scorecard`).
  A job with no execution history is reported as having no history, not as a
  job with a 0% failure rate.

* Distinguish "never succeeded" from "succeeded once, long ago". The watchdog
  cannot alert on the first case as promptly as it deserves, because its
  staleness threshold scales with the job's period: a MONTHLY job broken from
  birth is not called sick for roughly three months. That blind spot is not
  fixed here — it is *surfaced* here, as its own list, so it is visible long
  before the alarm would fire.

Unlike the ADK eval artifacts, which live in an unmounted directory and die
with the container, both sources here are durable sqlite/JSON on the data
volume, so this is a reporting job rather than new plumbing.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import statistics
from typing import Any, Dict, List, Optional

from .cron_watchdog import (
    CLOSED_STATUSES,
    consecutive_failures,
    discover_stores,
    expected_period_seconds,
    is_watchable,
    last_success_at,
    latest_error,
    load_jobs,
    read_execution_history,
    _parse_ts,
)

# How much execution history to weigh per job. The watchdog reads 40 for a
# yes/no verdict; a rate wants a longer arm, but not an unbounded one — a job
# that failed all last month and has been clean all this month should read as
# recovering, not as a coin flip forever.
HISTORY_LIMIT = int(os.environ.get("AUTOMATION_HEALTH_HISTORY", "200"))

# Cards this reads as "the self-healing loop ran". Both the watchdog and
# invoke_workflow's health tasks use a stable prefix in `created_by`, which is
# more reliable than title matching once titles get edited on the board.
SELF_HEAL_AUTHORS = ("cron_watchdog", "invoke_workflow")


def _utc_now() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


# --- cron side ---------------------------------------------------------------


def job_report(profile: str, job: dict, history: List[dict], now: float) -> Dict[str, Any]:
    """One job's record: how often it runs, how often it fails, and why.

    ``runs`` counts only what the executions store actually recorded. A job
    with an empty history reports ``runs: 0`` and ``failure_rate: None`` —
    not ``0.0``, which would read as a perfect record for a job that has never
    demonstrably done anything.
    """
    completed = sum(1 for h in history if h.get("status") == "completed")
    failed = sum(1 for h in history if h.get("status") == "failed")
    runs = completed + failed

    success = last_success_at(history)
    created = _parse_ts(job.get("created_at"))
    # Age from the last success, or from creation for a job that has never had
    # one — otherwise the case worth catching (broken from its first run) has
    # nothing to be measured against and silently drops out of the report.
    baseline = success if success is not None else created

    return {
        "profile": profile,
        "job_id": str(job.get("id") or "unknown"),
        "name": str(job.get("name") or job.get("id") or "unnamed job"),
        "enabled": bool(job.get("enabled", True)),
        "watchable": is_watchable(job),
        "schedule": (job.get("schedule") or {}).get("display")
        or (job.get("schedule") or {}).get("expr"),
        "period_seconds": expected_period_seconds(job, history),
        "runs": runs,
        "failures": failed,
        # None, not 0.0 — see the docstring. An unmeasured job is not a healthy one.
        "failure_rate": (failed / runs) if runs else None,
        "consecutive_failures": consecutive_failures(history),
        "last_success_at": success,
        "never_succeeded": success is None,
        "age_seconds": int(now - baseline) if baseline else None,
        "last_error": latest_error(history),
    }


def scan_jobs(data_dir: str = "/opt/data", now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Every job in every profile's store, with its execution record."""
    now = now if now is not None else _utc_now()
    reports: List[Dict[str, Any]] = []
    for profile, jobs_path, executions_db in discover_stores(data_dir):
        for job in load_jobs(jobs_path):
            job_id = str(job.get("id") or "")
            if not job_id:
                continue
            history = read_execution_history(executions_db, job_id, limit=HISTORY_LIMIT)
            reports.append(job_report(profile, job, history, now))
    return reports


# --- kanban side -------------------------------------------------------------


def _open_kanban(kanban_db: str) -> Optional[sqlite3.Connection]:
    """Read-only handle on the board, or None if it cannot be read.

    Read-only by URI rather than by convention: this container runs as root
    and the board belongs to the hermes user, so a handle that could write is
    a handle that could leave a root-owned journal file behind and break the
    owner's next write.
    """
    if not os.path.exists(kanban_db):
        return None
    try:
        conn = sqlite3.connect(f"file:{kanban_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def self_heal_report(kanban_db: str) -> Dict[str, Any]:
    """Did the cards the watchers filed actually get fixed, and how fast?

    This is the measurement that says whether self-healing is real. A loop that
    files cards nobody closes is not self-healing; it is a queue.
    """
    conn = _open_kanban(kanban_db)
    if conn is None:
        return {"available": False, "reason": "kanban.db not readable"}

    try:
        placeholders = ",".join("?" for _ in SELF_HEAL_AUTHORS)
        try:
            cards = conn.execute(
                f"SELECT id, title, status, assignee, created_at, completed_at "
                f"FROM tasks WHERE created_by IN ({placeholders})",
                SELF_HEAL_AUTHORS,
            ).fetchall()
        except sqlite3.Error as exc:
            return {"available": False, "reason": f"query failed: {exc}"}

        filed = len(cards)
        closed = [c for c in cards if (c["status"] or "") in CLOSED_STATUSES]

        # Time-to-fix from the run ledger, not from the card's own timestamps:
        # a card can sit unclaimed for hours before anyone starts, and counting
        # that as repair time measures queue depth, not the fixing.
        #
        # The window starts at the FIRST run of any outcome and ends at the
        # last completed one. Measuring only completed runs reports zero for
        # the common repair shape — a run that works, blocks on something it
        # cannot reach, and a second run that closes the card the instant the
        # blocker clears. All the effort sits in the blocked run, so counting
        # just the closing one turns real work into a confident 0s.
        durations: List[float] = []
        try:
            card_ids = {c["id"] for c in cards}
            rows = conn.execute(
                "SELECT task_id, "
                "       MIN(started_at) AS first_start, "
                "       MAX(CASE WHEN outcome = 'completed' THEN ended_at END) AS last_end "
                "FROM task_runs WHERE started_at IS NOT NULL "
                "GROUP BY task_id"
            ).fetchall()
            for row in rows:
                if row["task_id"] not in card_ids:
                    continue
                if row["first_start"] and row["last_end"]:
                    delta = float(row["last_end"]) - float(row["first_start"])
                    if delta >= 0:
                        durations.append(delta)
        except sqlite3.Error:
            durations = []

        return {
            "available": True,
            "cards_filed": filed,
            "cards_closed": len(closed),
            # None rather than 0.0 when nothing has been filed: a loop that has
            # never run has no close rate, and reporting 0% would read as failure.
            "close_rate": (len(closed) / filed) if filed else None,
            "open_cards": [
                {
                    "id": c["id"],
                    "title": c["title"],
                    "status": c["status"],
                    "assignee": c["assignee"],
                    "created_at": c["created_at"],
                }
                for c in cards
                if (c["status"] or "") not in CLOSED_STATUSES
            ],
            "measured_fixes": len(durations),
            "median_time_to_fix_seconds": (
                int(statistics.median(durations)) if durations else None
            ),
        }
    finally:
        conn.close()


# --- the digest --------------------------------------------------------------


def digest(
    data_dir: str = "/opt/data",
    kanban_db: str = "/opt/data/kanban.db",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """The whole picture: what is scheduled, what breaks, and what gets fixed."""
    now = now if now is not None else _utc_now()
    jobs = scan_jobs(data_dir, now=now)
    watchable = [j for j in jobs if j["watchable"]]

    # Jobs that have never once succeeded. Called out separately because the
    # watchdog's threshold scales with the job's period, so a monthly job
    # broken from birth stays under its alarm for about three months. Here it
    # shows up on the first read.
    never = sorted(
        (j for j in watchable if j["never_succeeded"]),
        key=lambda j: (j["age_seconds"] is None, -(j["age_seconds"] or 0)),
    )

    measured = [j for j in watchable if j["failure_rate"] is not None]
    worst = sorted(
        measured, key=lambda j: (j["failure_rate"], j["failures"]), reverse=True
    )[:10]

    return {
        "generated_at": datetime.datetime.fromtimestamp(
            now, datetime.timezone.utc
        ).isoformat(),
        "history_limit": HISTORY_LIMIT,
        "totals": {
            "jobs": len(jobs),
            "watchable": len(watchable),
            # Jobs with no recorded runs are counted, not silently folded into
            # the healthy majority.
            "unmeasured": len(watchable) - len(measured),
            "runs": sum(j["runs"] for j in jobs),
            "failures": sum(j["failures"] for j in jobs),
            "never_succeeded": len(never),
        },
        "never_succeeded": never,
        "worst_offenders": worst,
        "self_healing": self_heal_report(kanban_db),
    }
