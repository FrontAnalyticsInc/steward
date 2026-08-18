"""Read the ADK run-record corpus. The Hermes side's view of what the fleet did.

Every workflow invocation appends one JSON object to
``${ADK_STATE_DIR}/traces/<app>/<YYYY-MM-DD>.jsonl`` — written by
``invoke_workflow.py``, durable on the host, unaffected by container recreates.
Until now only the dashboard read them. This module is the reader for everything
that isn't the dashboard: the scheduled review, an agent answering "how is the
fleet doing", anything that needs the record after the fact.

Why a module and not a CLI subcommand or an HTTP call
-----------------------------------------------------
Both the gateway and the dashboard bind ``~/.hermes`` at ``/opt/data``, so one
file under ``/opt/data/scripts`` is importable from either side with no shared
package, no image rebuild, and no network hop. A CLI subcommand would live in
the gateway image and need a rebuild per iteration; an HTTP call to the
dashboard would make the reader depend on the dashboard being up — and the
dashboard shares a PID namespace with the gateway, so it dies with it. The
failure this reader most needs to explain is the one likeliest to have taken the
dashboard down with it.

What to key on
--------------
The outbreak that motivated this module — 83 runs lost over two days in August
2026 — was an address misconfiguration, not a workflow fault. Those runs died
before reaching self-assessment, so ``failed_stages`` was EMPTY on 95 of 96 of
them. A reviewer watching self-assessment scores would have seen nothing wrong
while the fleet was almost entirely down.

So the signals here are ordered by what actually catches things:

1. ``stale_apps`` — absence of success. Degrades correctly: a missing success
   looks the same whether the script was deleted, the interpreter died, or the
   whole container went away. Gets louder as the system gets sicker.
2. ``error_clusters`` — what the failures SAY, normalized so one outbreak reads
   as one cluster instead of 72 unique strings.
3. ``dishonest_runs`` — claims contradicted by measurements. Worth watching, but
   across the first 440 recorded runs this found zero, so it is a backstop
   rather than a primary signal.

Self-assessment scores come last on purpose. They only speak for runs that got
far enough to self-assess, which is exactly the set that was fine.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from collections import Counter, defaultdict
from typing import Any, Iterable

STATE_ROOT = os.environ.get("ADK_STATE_DIR", "/opt/data/adk")
TRACES_DIR = os.path.join(STATE_ROOT, "traces")


def parse_ts(value: Any) -> datetime.datetime | None:
    """A trace timestamp as a datetime, or None if it is not one.

    Deliberately identical to ``adk_scorecard.parse_ts``. Two readers over one
    format is how the two ends stop agreeing about what a run's clock said.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def list_apps(traces_dir: str | None = None) -> list[str]:
    """Every app with a trace directory, whether or not it has run recently."""
    root = traces_dir or TRACES_DIR
    try:
        return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    except OSError:
        return []


# Apps whose failures are the point. `intentional_failure_demo` exists to fail —
# it is how the health-task path is exercised — so counting it as a fleet fault
# puts a permanent entry on every report. Overridable rather than hardcoded at
# the call sites: a reviewer that cannot be told what to ignore gets ignored
# itself.
DEFAULT_EXCLUDED_APPS = ("app.agents.intentional_failure_demo",)


def load_runs(app: str | None = None, days: int = 30,
              traces_dir: str | None = None,
              exclude_apps: Iterable[str] = DEFAULT_EXCLUDED_APPS) -> list[dict]:
    """Trace records, newest first. All apps when ``app`` is None.

    A torn append (a run interrupted mid-write) is skipped rather than allowed
    to fail the read — one unparseable line must not cost the other 439.

    ``exclude_apps`` is ignored when ``app`` is named explicitly: asking for one
    app by name is unambiguous, and silently returning nothing would be worse
    than returning what was asked for.
    """
    root = traces_dir or TRACES_DIR
    apps = [app] if app else [a for a in list_apps(root) if a not in set(exclude_apps or ())]
    cutoff = _now() - datetime.timedelta(days=days)
    runs: list[dict] = []
    for a in apps:
        d = os.path.join(root, a)
        try:
            files = sorted(f for f in os.listdir(d) if f.endswith(".jsonl"))
        except OSError:
            continue
        for fname in files:
            try:
                with open(os.path.join(d, fname), "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        started = parse_ts(rec.get("started_at"))
                        if started and started < cutoff:
                            continue
                        rec.setdefault("app", a)
                        runs.append(rec)
            except OSError:
                continue
    runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return runs


# --- error clustering -------------------------------------------------------
#
# Raw error strings carry the ids of the run that produced them, so 72 instances
# of ONE outage present as 72 distinct strings and no count ever rises above 1.
# Normalizing collapses them to the shape of the fault, which is the level an
# outbreak is visible at.

_SUB = [
    (re.compile(r"/sessions/\S*"), "/sessions/…"),
    (re.compile(r"\bapp\.[A-Za-z0-9_.]+"), "<app>"),
    (re.compile(r"\bs_[0-9a-f]+\b", re.I), "<sid>"),
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def normalize_error(err: Any) -> str | None:
    """The shape of a failure, with the identity of the run stripped out."""
    if not err or not isinstance(err, str):
        return None
    out = err.strip()
    for pattern, repl in _SUB:
        out = pattern.sub(repl, out)
    return out.strip()[:160] or None


def error_clusters(runs: Iterable[dict]) -> list[dict]:
    """Failure shapes, commonest first, each with an example and its apps."""
    groups: dict[str, dict] = {}
    for r in runs:
        if r.get("status") == "ok":
            continue
        shape = normalize_error(r.get("error"))
        if shape is None:
            # A non-ok run with no error string failed inside the workflow
            # rather than around it; its stages say why.
            stages = (r.get("self_assessment") or {}).get("failed_stages") or []
            shape = f"(no error string; failed stages: {','.join(stages) or 'none declared'})"
        g = groups.setdefault(shape, {"shape": shape, "count": 0, "apps": Counter(),
                                      "example": r.get("error"), "last_seen": ""})
        g["count"] += 1
        g["apps"][r.get("app")] += 1
        started = r.get("started_at") or ""
        if started > g["last_seen"]:
            g["last_seen"] = started
    out = []
    for g in groups.values():
        g["apps"] = dict(g["apps"].most_common())
        out.append(g)
    out.sort(key=lambda g: -g["count"])
    return out


# --- absence of success -----------------------------------------------------


# A run that produced something is not an outage. `partial` means the stages
# worked and the yield was short — a quality signal, and the business of the
# self-assessment fields, not of a watchdog looking for silence.
PRODUCTIVE = ("ok", "partial")


def app_health(runs: Iterable[dict]) -> dict[str, dict]:
    """Per-app rollup: counts, last success, cadence, and how long since one."""
    by: dict[str, dict] = defaultdict(
        lambda: {"ok": 0, "failed": 0, "partial": 0, "total": 0,
                 "last_success": None, "last_run": None, "_starts": []}
    )
    for r in runs:
        a = r.get("app")
        h = by[a]
        h["total"] += 1
        status = r.get("status")
        if status in ("ok", "failed", "partial"):
            h[status] += 1
        started = r.get("started_at")
        if started:
            h["_starts"].append(started)
            if not h["last_run"] or started > h["last_run"]:
                h["last_run"] = started
            if status in PRODUCTIVE and (not h["last_success"] or started > h["last_success"]):
                h["last_success"] = started
    now = _now()
    for h in by.values():
        ts = parse_ts(h["last_success"])
        h["hours_since_success"] = round((now - ts).total_seconds() / 3600, 1) if ts else None
        h["fail_rate"] = round(h["failed"] / h["total"], 3) if h["total"] else None
        h["cadence_hours"] = _cadence_hours(h.pop("_starts"))
    return dict(by)


def _cadence_hours(starts: list[str]) -> float | None:
    """The app's own observed period, in hours. None if it has run once.

    The 90th percentile of the gaps, not the median. Runs come in bursts —
    retries, a manual re-run while debugging, a backfill — and a burst drags the
    median toward the spacing WITHIN it rather than between scheduled firings.
    Measured on this corpus, the daily briefing's median gap reads 0.28h and its
    p90 reads 24.4h; only one of those is its schedule. Taking a high percentile
    reads the quiet stretches, which is where the real period shows.
    """
    ts = sorted(t for t in (parse_ts(s) for s in starts) if t)
    if len(ts) < 2:
        return None
    gaps = sorted((b - a).total_seconds() / 3600 for a, b in zip(ts, ts[1:]))
    p90 = gaps[min(int(len(gaps) * 0.9), len(gaps) - 1)]
    return round(p90, 2)


def stale_apps(runs: Iterable[dict], missed_runs: float = 3.0,
               floor_hours: float = 1.0) -> list[dict]:
    """Apps that have run but not PRODUCED recently — the loudest signal.

    An app that fires on schedule, dies the same way every time, and never
    succeeds is the shape every silent outage took. Absence degrades correctly:
    it looks the same whether the script was deleted, the interpreter died, or
    the container went away.

    The threshold is each app's OWN cadence, not a fixed clock. A flat "26h with
    no success" calls a monthly job sick twenty-nine days early and gives a
    ten-minute job a free day and a half to be down in — one number cannot serve
    both, and the corpus has both. So staleness is ``missed_runs`` × the app's
    observed median gap. Three rather than one: a single miss is a restart, a
    deploy, or a blip, and a watchdog that cries on those gets muted, which
    costs more than it saves.

    An app seen only once has no cadence to measure and is not judged — a first
    run is not evidence of a schedule.
    """
    out = []
    for app, h in app_health(runs).items():
        if not h["total"]:
            continue
        gap = h["cadence_hours"]
        if gap is None:
            continue
        allowed = max(missed_runs * gap, floor_hours)
        hrs = h["hours_since_success"]
        if hrs is None or hrs > allowed:
            out.append({"app": app, "hours_since_success": hrs,
                        "expected_every_hours": gap,
                        "stale_after_hours": round(allowed, 1),
                        "runs_in_window": h["total"], "failed": h["failed"],
                        "last_run": h["last_run"]})
    out.sort(key=lambda a: (a["hours_since_success"] is None,
                            (a["hours_since_success"] or 0) / (a["stale_after_hours"] or 1)),
             reverse=True)
    return out


# --- claims vs measurements -------------------------------------------------


def dishonest_runs(runs: Iterable[dict]) -> list[dict]:
    """Runs whose own checkpoints contradict what they claimed.

    Reads the field rather than recomputing it: the derivation belongs to
    ``invoke_workflow`` at write time, and recomputing here would let a reader
    and a writer disagree about the same run. Records written before that
    derivation existed carry None and are correctly not counted as dishonest.
    """
    return [
        {"app": r.get("app"), "run_id": r.get("run_id"),
         "started_at": r.get("started_at"),
         "claimed": r.get("self_reported_status"),
         "score": (r.get("self_assessment") or {}).get("score"),
         "failed_stages": (r.get("self_assessment") or {}).get("failed_stages") or []}
        for r in runs if r.get("self_report_accurate") is False
    ]


def summary(days: int = 7, traces_dir: str | None = None,
            missed_runs: float = 3.0,
            exclude_apps: Iterable[str] = DEFAULT_EXCLUDED_APPS) -> dict:
    """Everything a scheduled review needs, in one read.

    Signals are ordered by what catches real outages, not by what is easiest to
    chart — see the module docstring.
    """
    runs = load_runs(days=days, traces_dir=traces_dir, exclude_apps=exclude_apps)
    statuses = Counter(r.get("status") for r in runs)
    triggers = Counter(r.get("trigger") or "unrecorded" for r in runs)
    judged = [r for r in runs if r.get("self_report_accurate") is not None]
    return {
        "window_days": days,
        "generated_at": _now().isoformat(),
        "total_runs": len(runs),
        "status_counts": dict(statuses),
        "fail_rate": round(
            (statuses["failed"] + statuses["partial"]) / len(runs), 3
        ) if runs else None,
        "trigger_counts": dict(triggers),
        "stale_apps": stale_apps(runs, missed_runs=missed_runs),
        "error_clusters": error_clusters(runs),
        "dishonest_runs": dishonest_runs(runs),
        "honesty_judged": len(judged),
        "app_health": app_health(runs),
    }


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Read the ADK run-record corpus.")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--app", default=None)
    p.add_argument("--json", action="store_true", help="Full summary as JSON.")
    args = p.parse_args()

    if args.json:
        print(json.dumps(summary(days=args.days), indent=2, default=str))
        raise SystemExit(0)

    s = summary(days=args.days)
    print(f"{s['total_runs']} runs over {args.days}d   {s['status_counts']}")
    if s["stale_apps"]:
        print("\nNO RECENT SUCCESS:")
        for a in s["stale_apps"]:
            hrs = a["hours_since_success"]
            print(f"  {a['app']:52} {'never' if hrs is None else f'{hrs}h ago':>10}"
                  f"  (runs ~{a['expected_every_hours']}h, stale >{a['stale_after_hours']}h,"
                  f" {a['failed']}/{a['runs_in_window']} failed)")
    if s["error_clusters"]:
        print("\nFAILURE SHAPES:")
        for c in s["error_clusters"][:8]:
            print(f"  {c['count']:>4}x  {c['shape'][:96]}")
    if s["dishonest_runs"]:
        print(f"\nCLAIMED SUCCESS WHILE FAILING: {len(s['dishonest_runs'])}")
        for d in s["dishonest_runs"][:8]:
            print(f"  {d['started_at'][:16]}  {d['app']}  score={d['score']}")
