"""Notice when a scheduled job stops succeeding, and put it on the board.

Why this lives in the dashboard rather than in a cron job
--------------------------------------------------------

A reporter that runs inside the thing it reports on can only report failures
it survives. `invoke_workflow` files a health task when a workflow run fails,
which covers everything that happens *after* Python starts — and nothing
before it. A job whose `script` path is wrong (a rename, a bad mount, a typo
like `scripts/scripts/x.py`) dies in the cron runner: the wrapper never
executes, so nothing can file anything. The job fails every slot, forever,
against an empty board. We have shipped that bug twice.

The fix is not a better reporter. It is an observer that does not share fate
with the observed, so this runs in the dashboard container: a different
process, a different image, a different failure domain. If cron dies entirely,
this still notices — where a watchdog implemented as a cron job would die
with it.

Polarity matters too. This alerts on the ABSENCE OF SUCCESS, not on failure.
Alerting on failure requires the failing component to be healthy enough to
report, which is exactly the assumption that keeps breaking. Absence is
observable from outside and degrades correctly: a missing success looks the
same whether the script was deleted, the interpreter died, the ticker stopped,
or the whole container went away. It is the one signal that gets louder as the
system gets sicker.

`executions.db` says *why* (it records the runner's own error string, even for
a failure below Python). The staleness check says *that*. Neither is much use
alone: a heartbeat alone sends you hunting, and a failure watcher cannot see a
job that stopped running at all.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from typing import Any, Iterable, NamedTuple, Optional

# How many expected runs may pass with no success before a job is called sick.
# Three rather than one: a single miss is a restart, a deploy, or a blip, and a
# watchdog that cries on those gets muted, which costs more than it saves.
MISSED_RUNS_BEFORE_ALERT = int(os.environ.get("CRON_WATCHDOG_MISSED_RUNS", "3"))

# Never call a job sick sooner than this, however fast it runs. Protects a
# high-frequency job from a single slow run tripping the alarm.
MIN_STALENESS_SECONDS = int(os.environ.get("CRON_WATCHDOG_MIN_STALENESS", "900"))

# Fallback cadence when a schedule's period cannot be determined and there is
# too little history to measure one. Daily is the common case for the cron
# expressions in this fleet, and erring long means erring quiet.
FALLBACK_PERIOD_SECONDS = 86400

CHECK_INTERVAL_SECONDS = int(os.environ.get("CRON_WATCHDOG_INTERVAL", "300"))
ENABLED = os.environ.get("CRON_WATCHDOG_ENABLED", "true").lower() not in (
    "0", "false", "no",
)

# Statuses meaning nobody is looking at this task any more, so it must not
# suppress a new one. Same rule as invoke_workflow's health tasks, and for the
# same reason: `hermes kanban`'s own idempotency lookup spans completed tasks,
# so a card someone fixed and closed would silence the next outbreak forever.
CLOSED_STATUSES = {"done", "archived"}

CARD_ASSIGNEE = os.environ.get("CRON_WATCHDOG_ASSIGNEE", "dev")
CARD_CREATED_BY = "cron_watchdog"

# Schedules that are not supposed to repeat. A one-shot job that ran and
# stopped is finished, not stale.
NON_RECURRING_KINDS = {"once"}


class StaleJob(NamedTuple):
    """A job that has not succeeded recently enough to be believed healthy."""

    profile: str
    job_id: str
    name: str
    period_seconds: int
    threshold_seconds: int
    stale_for_seconds: int
    last_success_at: Optional[float]   # epoch seconds, None if never
    last_error: Optional[str]
    consecutive_failures: int

    @property
    def key(self) -> str:
        return f"cron-health:{self.profile}:{self.job_id}"

    @property
    def title(self) -> str:
        # Deliberately free of times and counts: those move between checks and
        # would make one sick job look like a new problem every five minutes.
        return f"[cron] {self.name} has stopped succeeding"


# --- reading the cron stores -------------------------------------------------


def discover_stores(data_dir: str = "/opt/data") -> list[tuple[str, str, str]]:
    """Find every profile's cron store as ``(profile, jobs.json, executions.db)``.

    Cron is per-profile — ``HERMES_HOME`` selects the store — so scanning only
    the default profile would leave every job the dev and worker profiles own
    unwatched, which is precisely the blind spot this module exists to close.
    """
    stores: list[tuple[str, str, str]] = []

    def add(profile: str, cron_dir: str) -> None:
        jobs = os.path.join(cron_dir, "jobs.json")
        execs = os.path.join(cron_dir, "executions.db")
        if os.path.exists(jobs):
            stores.append((profile, jobs, execs))

    add("default", os.path.join(data_dir, "cron"))
    profiles_dir = os.path.join(data_dir, "profiles")
    if os.path.isdir(profiles_dir):
        for name in sorted(os.listdir(profiles_dir)):
            add(name, os.path.join(profiles_dir, name, "cron"))
    return stores


def load_jobs(jobs_path: str) -> list[dict]:
    """Read a jobs.json into a list. Never raises: an unreadable store is
    reported as empty rather than taking the dashboard down."""
    try:
        with open(jobs_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return [j for j in jobs if isinstance(j, dict)]


def _parse_ts(value: Any) -> Optional[float]:
    """ISO-8601 (as cron writes it) to epoch seconds."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def read_execution_history(
    executions_db: str, job_id: str, limit: int = 40
) -> list[dict]:
    """Recent executions for a job, newest first. Empty on any read problem.

    Opened read-only. This process is not the owner of that database and must
    never be the reason a cron write fails.
    """
    if not os.path.exists(executions_db):
        return []
    try:
        conn = sqlite3.connect(f"file:{executions_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT status, error, claimed_at, finished_at FROM executions "
                "WHERE job_id = ? ORDER BY claimed_at DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


# --- deciding what "too long" means ------------------------------------------


def _field_step(field: str) -> Optional[int]:
    """Step size for a cron field that fires more than once per unit.

    ``*`` -> 1, ``*/5`` -> 5. Anything pinned to specific values returns None,
    meaning "this field does not repeat within its unit".
    """
    if field == "*":
        return 1
    if field.startswith("*/"):
        try:
            step = int(field[2:])
        except ValueError:
            return None
        return step if step > 0 else None
    return None


def cron_period_seconds(expr: str) -> Optional[int]:
    """Approximate period of a 5-field cron expression, or None if unreadable.

    Deliberately small and coarse. This decides an alert threshold, not a fire
    time, so it only has to answer "roughly how often" — and a coarse answer
    that is right about the order of magnitude beats a precise one that needs
    a dependency this container does not have.

    It reads the largest repeating field, because that is what sets the
    cadence: `*/5 * * * *` is every five minutes regardless of what the day
    fields say, and `0 9 * * 1` is weekly because the day-of-week pins it.
    """
    parts = (expr or "").split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts

    step = _field_step(minute)
    if step is not None:
        return step * 60
    step = _field_step(hour)
    if step is not None:
        return step * 3600
    # Minute and hour are both pinned, so it fires at a fixed time of day. How
    # often depends on which calendar fields are constrained.
    if dow != "*":
        return 7 * 86400
    if dom != "*":
        return 30 * 86400
    if month != "*":
        return 365 * 86400
    return 86400


def expected_period_seconds(job: dict, history: Iterable[dict]) -> int:
    """How often this job is supposed to run.

    Read from the schedule wherever possible, and from history only as a last
    resort — history lies. A job in a failure loop retries in bursts and picks
    up manual runs, so the gaps between *executions* can be minutes for a job
    that is scheduled daily. Measuring that way gave a real daily job a period
    of 463s and a threshold of 23 minutes, which would call healthy jobs sick.

    When history is all there is, only successful runs are measured: successes
    follow the schedule, failures follow whatever went wrong.
    """
    schedule = job.get("schedule") or {}
    kind = schedule.get("kind")

    if kind == "interval":
        minutes = schedule.get("minutes")
        if isinstance(minutes, (int, float)) and minutes > 0:
            return int(minutes * 60)

    if kind == "cron":
        parsed = cron_period_seconds(schedule.get("expr") or "")
        if parsed:
            return parsed

    stamps = sorted(
        s
        for s in (
            _parse_ts(h.get("claimed_at"))
            for h in history
            if h.get("status") == "completed"
        )
        if s
    )
    gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
    if len(gaps) >= 2:
        return max(int(statistics.median(gaps)), 60)
    return FALLBACK_PERIOD_SECONDS


def last_success_at(history: Iterable[dict]) -> Optional[float]:
    """When this job last finished cleanly, or None if it never has."""
    best: Optional[float] = None
    for h in history:
        if h.get("status") != "completed":
            continue
        stamp = _parse_ts(h.get("finished_at")) or _parse_ts(h.get("claimed_at"))
        if stamp and (best is None or stamp > best):
            best = stamp
    return best


def consecutive_failures(history: Iterable[dict]) -> int:
    """Failures since the most recent success. ``history`` is newest first."""
    count = 0
    for h in history:
        status = h.get("status")
        if status == "completed":
            break
        if status == "failed":
            count += 1
    return count


def latest_error(history: Iterable[dict]) -> Optional[str]:
    """The newest error *since the last success* — the 'why' a heartbeat lacks.

    This is where a below-Python failure shows up: the runner writes
    "Script not found: ..." here even though the wrapper never executed.

    Stops at the first success, newest-first, so a fixed fault cannot be
    reported as current. Without that guard this scanned the whole history and
    happily surfaced an error somebody had already resolved hours earlier —
    which is how a healthy job gets described as broken.
    """
    for h in history:
        if h.get("status") == "completed":
            return None
        if h.get("status") == "failed" and h.get("error"):
            return str(h["error"])
    return None


def is_watchable(job: dict) -> bool:
    """Whether a job is supposed to keep succeeding.

    A job that is disabled, paused or finished is not broken — it is off. A
    watchdog that cannot tell those apart reports every retired job forever.
    """
    if not job.get("enabled", True):
        return False
    if job.get("paused_at"):
        return False
    if (job.get("state") or "").lower() in {"completed", "paused"}:
        return False
    if (job.get("schedule") or {}).get("kind") in NON_RECURRING_KINDS:
        return False
    return True


def evaluate_job(
    profile: str, job: dict, history: list[dict], now: float
) -> Optional[StaleJob]:
    """Return a StaleJob when this job has gone too long without succeeding."""
    if not is_watchable(job):
        return None

    period = expected_period_seconds(job, history)
    threshold = max(period * MISSED_RUNS_BEFORE_ALERT, MIN_STALENESS_SECONDS)

    success = last_success_at(history)
    # A job that has never succeeded is measured from when it was created —
    # otherwise the case this module was written for (broken from its very
    # first run) would never trip, having no success to be stale relative to.
    baseline = success if success is not None else _parse_ts(job.get("created_at"))
    if baseline is None:
        return None

    stale_for = now - baseline
    if stale_for <= threshold:
        return None

    return StaleJob(
        profile=profile,
        job_id=str(job.get("id") or "unknown"),
        name=str(job.get("name") or job.get("id") or "unnamed job"),
        period_seconds=int(period),
        threshold_seconds=int(threshold),
        stale_for_seconds=int(stale_for),
        last_success_at=success,
        last_error=latest_error(history),
        consecutive_failures=consecutive_failures(history),
    )


def scan(data_dir: str = "/opt/data", now: Optional[float] = None) -> list[StaleJob]:
    """Every job across every profile that has stopped succeeding."""
    now = time.time() if now is None else now
    stale: list[StaleJob] = []
    for profile, jobs_path, executions_db in discover_stores(data_dir):
        for job in load_jobs(jobs_path):
            job_id = job.get("id")
            if not job_id:
                continue
            history = read_execution_history(executions_db, str(job_id))
            found = evaluate_job(profile, job, history, now)
            if found:
                stale.append(found)
    return stale


# --- putting it on the board -------------------------------------------------


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def card_body(job: StaleJob, now: float) -> str:
    last_seen = (
        datetime.fromtimestamp(job.last_success_at, timezone.utc).isoformat()
        if job.last_success_at
        else "never — this job has not had a single successful run"
    )
    lines = [
        f"Scheduled job `{job.name}` (`{job.job_id}`, profile `{job.profile}`) "
        "has stopped succeeding.",
        "",
        f"- last success: {last_seen}",
        f"- silent for: {_fmt_duration(job.stale_for_seconds)}",
        f"- expected cadence: every {_fmt_duration(job.period_seconds)}",
        f"- alert threshold: {_fmt_duration(job.threshold_seconds)} "
        f"({MISSED_RUNS_BEFORE_ALERT} missed runs)",
        f"- consecutive failed executions on record: {job.consecutive_failures}",
    ]
    if job.last_error:
        lines += [
            "",
            "## Last recorded error",
            "",
            "```",
            job.last_error[:2000],
            "```",
        ]
    else:
        lines += [
            "",
            "No failed execution was recorded, which means the job is not "
            "merely failing — it is not running at all. Check that the "
            "scheduler is alive and that the job is still enabled.",
        ]
    lines += [
        "",
        "## Why this was filed by the dashboard and not by the job",
        "",
        "A job that dies before its interpreter starts — a missing script, a",
        "bad mount, a renamed path — cannot report anything, because the code",
        "that would report is the code that did not run. This card comes from",
        "a watcher in the dashboard container, which alerts on the absence of",
        "success rather than on a failure it has to be alive to observe.",
        "",
        "So: confirm the job still does what it claims before closing this. A",
        "green run is the only thing that clears it.",
    ]
    return "\n".join(lines)


def _match_open_card(conn: sqlite3.Connection, title: str) -> Optional[str]:
    """Id of a still-open card with this title, else None.

    Scoped to open tasks on purpose. A card that was fixed and marked `done`
    must not suppress the next outbreak — that is the exact bug that let a
    ten-minute job fail for hours against an empty board.
    """
    try:
        rows = conn.execute(
            "SELECT id, status FROM tasks WHERE title = ?", (title,)
        ).fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        if (row["status"] or "").lower() in CLOSED_STATUSES:
            continue
        return row["id"]
    return None


def _restore_ownership(db_path: str) -> None:
    """Give any sidecar file sqlite just made back to the database's owner.

    This container runs as root; kanban.db belongs to the hermes user. A
    root-owned `-journal` or `-wal` left beside it is a file the gateway cannot
    write, which would break the board for everyone else on the next write.
    """
    try:
        stat = os.stat(db_path)
    except OSError:
        return
    if os.geteuid() != 0:
        return
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = db_path + suffix
        try:
            if os.path.exists(sidecar):
                os.chown(sidecar, stat.st_uid, stat.st_gid)
        except OSError:
            pass


def file_card(kanban_db: str, job: StaleJob, now: Optional[float] = None) -> Optional[str]:
    """Open a card for a sick job, or return the open one already covering it.

    Returns the task id, or None if nothing could be written. Never raises:
    the watchdog failing must not take down the dashboard that hosts it.
    """
    now = time.time() if now is None else now
    if not os.path.exists(kanban_db):
        return None

    try:
        conn = sqlite3.connect(kanban_db, timeout=10)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None

    try:
        existing = _match_open_card(conn, job.title)
        if existing:
            return existing

        task_id = "t_" + secrets.token_hex(4)
        created = int(now)
        # Run-scoped so `hermes kanban`'s own idempotency lookup — which spans
        # completed tasks — can never match a closed card from a previous
        # outbreak and swallow this one. Cross-run dedup is _match_open_card's
        # job, and it only counts open cards.
        idem = f"{job.key}:{created}"
        try:
            with conn:
                conn.execute(
                    "INSERT INTO tasks "
                    "(id, title, body, assignee, status, priority, created_by, "
                    " created_at, workspace_kind, idempotency_key) "
                    "VALUES (?, ?, ?, ?, 'ready', 0, ?, ?, 'scratch', ?)",
                    (
                        task_id,
                        job.title,
                        card_body(job, now),
                        CARD_ASSIGNEE,
                        CARD_CREATED_BY,
                        created,
                        idem,
                    ),
                )
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                    "VALUES (?, NULL, 'created', ?, ?)",
                    (
                        task_id,
                        json.dumps(
                            {
                                "assignee": CARD_ASSIGNEE,
                                "status": "ready",
                                "source": "cron_watchdog",
                                "job_id": job.job_id,
                                "profile": job.profile,
                            }
                        ),
                        created,
                    ),
                )
        except sqlite3.Error:
            return None
        return task_id
    finally:
        conn.close()
        _restore_ownership(kanban_db)


def run_once(
    data_dir: str = "/opt/data",
    kanban_db: str = "/opt/data/kanban.db",
    now: Optional[float] = None,
) -> list[tuple[StaleJob, Optional[str]]]:
    """One full pass: find sick jobs, make sure each has an open card."""
    now = time.time() if now is None else now
    results = []
    for job in scan(data_dir, now=now):
        results.append((job, file_card(kanban_db, job, now=now)))
    return results


async def watchdog_loop(
    data_dir: str = "/opt/data", kanban_db: str = "/opt/data/kanban.db"
) -> None:
    """Run :func:`run_once` forever. Started from the dashboard's lifespan.

    Every exception is swallowed and retried. A watchdog that can die is a
    watchdog that reports healthy right up until you need it.
    """
    import asyncio

    while True:
        try:
            found = await asyncio.to_thread(run_once, data_dir, kanban_db)
            for job, task_id in found:
                if task_id:
                    print(
                        f"[cron-watchdog] {job.profile}/{job.name} stale for "
                        f"{_fmt_duration(job.stale_for_seconds)} -> {task_id}",
                        flush=True,
                    )
        except Exception as exc:  # noqa: BLE001 - see docstring
            print(f"[cron-watchdog] pass failed: {exc}", flush=True)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
