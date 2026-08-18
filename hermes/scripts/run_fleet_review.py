"""Weekly review of the ADK run-record corpus. Files a board task, or says nothing.

Runs as a no_agent cron script. No model, no tokens: everything it reports is
counted, not judged, and paying for a turn to paraphrase counts would put the
spend back on exactly the jobs the no_agent path exists to keep cheap.

It reports on ABSENCE and SHAPE, not on scores. The outage that motivated this —
83 runs lost over two days — killed the runs before they could self-assess, so
every score-based signal read clean while the fleet was down. Scores only speak
for runs healthy enough to produce them.

Silence is the healthy output. A weekly report that always files something
trains the board to ignore it, so a clean week writes nothing and exits 0; the
absence of the task IS the all-clear. Whether the review itself is still running
is `cron_watchdog`'s question, and it is already asked from outside this
process — a self-report of liveness from a dead reporter is worth nothing.
"""

import json
import os
import subprocess
import sys

sys.path.insert(0, "/opt/data/scripts")

from trace_store import summary  # noqa: E402

WINDOW_DAYS = int(os.environ.get("FLEET_REVIEW_DAYS", "7"))

# The same board and assignee `invoke_workflow` files health tasks to, read from
# the same environment variables. A reviewer that files somewhere else splits the
# fleet's health across two boards, and the second one is the one nobody opens.
BOARD = os.environ.get("WORKFLOW_HEALTH_BOARD", "default")
ASSIGNEE = os.environ.get("WORKFLOW_HEALTH_ASSIGNEE", "dev")
CREATED_BY = "fleet_review"

# A failure rate the fleet has demonstrably beaten. Measured over the corpus
# after the August address fix: 90 runs, 88 ok. A ceiling set below what the
# system already achieves is a ceiling that reports every ordinary week.
FAIL_RATE_CEILING = 0.10


def _findings(s: dict) -> list[str]:
    """The report body, or an empty list when there is nothing worth saying."""
    out = []

    if s["stale_apps"]:
        out.append("## Apps with no recent success\n")
        out.append("Judged against each app's own observed cadence, not a fixed clock.\n")
        for a in s["stale_apps"]:
            since = "never" if a["hours_since_success"] is None else f"{a['hours_since_success']}h ago"
            out.append(
                f"- **{a['app']}** — last success {since}; runs about every "
                f"{a['expected_every_hours']}h, so >{a['stale_after_hours']}h is late. "
                f"{a['failed']}/{a['runs_in_window']} runs failed in the window."
            )
        out.append("")

    if s["dishonest_runs"]:
        out.append("## Runs that claimed success while their own checkpoints failed\n")
        for d in s["dishonest_runs"]:
            out.append(
                f"- `{d['run_id']}` claimed **{d['claimed']}** at score {d['score']} "
                f"(failed stages: {', '.join(d['failed_stages']) or 'none declared'})"
            )
        out.append("")

    rate = s["fail_rate"]
    if rate is not None and rate > FAIL_RATE_CEILING:
        out.append(
            f"## Fleet failure rate {rate:.0%} over {s['window_days']}d\n\n"
            f"{s['total_runs']} runs: {s['status_counts']}. "
            f"Above the {FAIL_RATE_CEILING:.0%} the fleet normally holds.\n"
        )
        out.append("### Failure shapes\n")
        out.append("Error strings normalized so one outbreak reads as one row.\n")
        for c in s["error_clusters"][:10]:
            apps = ", ".join(list(c["apps"])[:4])
            out.append(f"- **{c['count']}x** `{c['shape'][:120]}` — {apps} (last {c['last_seen'][:16]})")
        out.append("")

    return out


def build_report(s: dict) -> str | None:
    body = _findings(s)
    if not body:
        return None
    header = [
        f"Automated review of the last {s['window_days']} days of ADK run records.",
        "",
        f"- runs: **{s['total_runs']}** — {s['status_counts']}",
        f"- triggers: {s['trigger_counts']}",
        f"- self-reports checked against measurements: {s['honesty_judged']}",
        "",
        "Source: `${ADK_STATE_DIR}/traces`, read by `/opt/data/scripts/trace_store.py`.",
        "",
    ]
    return "\n".join(header + body)


def file_task(title: str, body: str) -> str | None:
    """Open one board task for this window. Never raises — an unreachable board
    must not fail the review, and the summary still reaches stdout.

    Filed `blocked`, not ready. A task assigned and left ready is claimed by the
    decomposer within the minute and worked by an agent — which is right for a
    health task describing one broken pipeline, and wrong for a digest. This is
    a week of counts for a person to read; auto-dispatching a turn to interpret
    it every Monday would put the model spend back on the reporting path that
    the whole no_agent design exists to keep free of it. Blocked means it waits
    for a human, which is the intended reader.
    """
    cmd = [
        "hermes", "kanban", "--board", BOARD, "create", title,
        "--body", body, "--assignee", ASSIGNEE,
        "--initial-status", "blocked",
        # Window-scoped: one task per review, and a re-run of the same week
        # updates nothing rather than opening a duplicate.
        "--idempotency-key", f"fleet-review:{title}",
        "--created-by", CREATED_BY, "--json",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"could not file review task: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"could not file review task: {proc.stderr.strip()[:300]}", file=sys.stderr)
        return None
    try:
        return (json.loads(proc.stdout) or {}).get("id")
    except json.JSONDecodeError:
        return (proc.stdout or "").strip()[:64] or None


def main() -> None:
    s = summary(days=WINDOW_DAYS)
    report = build_report(s)

    if report is None:
        # Deliberately silent: no stdout means no delivery, and a clean week
        # should not cost anyone a notification.
        return

    # Dated rather than content-keyed, so a fault that persists across weeks
    # opens a new task each week instead of being swallowed as a duplicate of
    # the one nobody closed.
    title = f"Fleet review — week ending {s['generated_at'][:10]}"
    task_id = file_task(title, report)

    print(f"{s['total_runs']} runs over {s['window_days']}d: {s['status_counts']}")
    if s["stale_apps"]:
        print(f"{len(s['stale_apps'])} app(s) with no recent success: "
              f"{', '.join(a['app'] for a in s['stale_apps'])}")
    if s["dishonest_runs"]:
        print(f"{len(s['dishonest_runs'])} run(s) claimed success while failing")
    print(f"filed review task {task_id}" if task_id else "review task NOT filed (see stderr)")


if __name__ == "__main__":
    main()
