"""Aggregate ADK run traces into a scorecard.

Source of truth is `${ADK_STATE_DIR}/traces/<app>/<YYYY-MM-DD>.jsonl`, one JSON
object per invocation, appended by ~/.hermes/scripts/invoke_workflow.py.

The governing rule here is: never report an uninstrumented field as a measurement.
Traces written before instrumentation carry a literal `"model_calls": 0` — not
because zero models were called, but because `emit_result` never populated the
field the schema declared. Averaging those into a utilization number would invent
data. So instrumented runs are tagged with `trace_version`, and anything derived
from newer fields is computed over that subset only, with the sample size
reported alongside. A tile that says "not recorded" is worth more than a
confident zero.
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any, Dict, List, Optional

# Runs written by the instrumented invoke_workflow carry this. Absent => the
# utilization and self-report fields on that line are placeholders, not data.
TRACE_VERSION = 2


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def parse_ts(value: Any) -> Optional[datetime.datetime]:
    """A trace timestamp as a datetime, or None if it is not one.

    Public because the automation detail route matches cron executions to
    traces by time and has to read a trace's clock the same way the scorecard
    does — two parsers over one format is how the two ends stop agreeing.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


_parse_ts = parse_ts  # the name the rest of this module was written against


def load_runs(state_dir: str, app: str, days: int = 30) -> List[dict]:
    """Every trace line for `app` within the window, newest first."""
    root = os.path.join(state_dir, "traces", app)
    if not os.path.isdir(root):
        return []
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    runs: List[dict] = []
    try:
        files = sorted(f for f in os.listdir(root) if f.endswith(".jsonl"))
    except OSError:
        return []
    for fname in files:
        try:
            with open(os.path.join(root, fname), "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a torn append is not worth failing the page over
                    started = _parse_ts(rec.get("started_at"))
                    if started and started < cutoff:
                        continue
                    runs.append(rec)
        except OSError:
            continue
    runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return runs


def _dir_stats(path: str) -> Dict[str, int]:
    count = 0
    total = 0
    for root, dirs, files in os.walk(path):
        if root == path:
            count = len(dirs)
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return {"count": count, "bytes": total}


def _metric(runs: List[dict], key: str) -> dict:
    """Sum a utilization field over instrumented runs only."""
    vals = [r.get(key) for r in runs if r.get("trace_version") and isinstance(r.get(key), (int, float))]
    if not vals:
        return {"total": None, "avg": None, "instrumented": False, "sample": 0}
    return {
        "total": sum(vals),
        "avg": round(sum(vals) / len(vals), 2),
        "instrumented": True,
        "sample": len(vals),
    }


def _eval_block(runs: List[dict]) -> dict:
    """Outcome tallies. Shared by the full scorecard and the fleet summary.

    Counts every run, instrumented or not: `status` predates instrumentation,
    so unlike the utilization fields it is trustworthy across the whole window.
    """
    total = len(runs)
    by_status: Dict[str, int] = {}
    for r in runs:
        by_status[str(r.get("status"))] = by_status.get(str(r.get("status")), 0) + 1
    ok = by_status.get("ok", 0)
    partial = by_status.get("partial", 0)
    first_try = [r for r in runs if r.get("attempt") == 1 and r.get("status") in ("ok", "partial")]
    return {
        "by_status": by_status,
        "ok": ok,
        "partial": partial,
        "failed": by_status.get("failed", 0),
        "success_rate": round((ok + partial) / total, 3) if total else None,
        "first_attempt_rate": round(len(first_try) / total, 3) if total else None,
        "last_run_at": runs[0].get("started_at") if runs else None,
        "last_status": runs[0].get("status") if runs else None,
    }


def summarize(runs: List[dict], app: str) -> dict:
    """Headline health for one app, for the fleet view.

    Deliberately cheap: no per-agent merge, no run list, no workspace stat, no
    source hash. The fleet view calls this once per app on every load, and the
    expensive parts of `aggregate` are exactly the parts it does not render.
    """
    ev = _eval_block(runs)
    # Cheap enough to belong here: reading one number off the newest run is what
    # makes "this pipeline is not working" visible from the fleet view instead of
    # only after clicking into an agent.
    latest = next(
        (r["self_assessment"] for r in runs
         if isinstance((r.get("self_assessment") or {}).get("score"), (int, float))),
        None,
    )
    return {
        "app": app,
        "run_count": len(runs),
        "success_rate": ev["success_rate"],
        "last_run_at": ev["last_run_at"],
        "last_status": ev["last_status"],
        "self_assessment_score": latest["score"] if latest else None,
        "failing_stages": (latest or {}).get("failed_stages") or [],
    }


def aggregate(runs: List[dict], app: str, days: int, state_dir: str,
              current_sha: Optional[str] = None) -> dict:
    total = len(runs)
    instrumented = [r for r in runs if r.get("trace_version")]

    durations = [r["duration_ms"] for r in runs if isinstance(r.get("duration_ms"), (int, float))]

    # Self-report: does the model's claimed status match what run_tests measured?
    scored = [r for r in instrumented if isinstance(r.get("self_report_accurate"), bool)]
    agreed = sum(1 for r in scored if r["self_report_accurate"])

    # --- self-assessment ---
    # Two different claims, kept apart all the way to the UI. The run-level
    # score is measured: the fraction of a run's checkpoints that passed. The
    # per-agent score is a model's opinion of its own turn, offered only by
    # stages that make an LLM call anyway. Averaging them would let an
    # optimistic model raise a measured number, so they never meet.
    assessed = [r for r in runs if isinstance((r.get("self_assessment") or {}).get("score"), (int, float))]
    assess_scores = [r["self_assessment"]["score"] for r in assessed]
    # Which stages fail, across the window — the standing list of what to fix.
    failing: Dict[str, int] = {}
    for r in assessed:
        for stage in r["self_assessment"].get("failed_stages") or []:
            failing[stage] = failing.get(stage, 0) + 1

    # Counters merged across runs. `None` until something measures them, for the
    # reason run_metrics.py states: absent is not zero, and LiteLLM against
    # Ollama routinely omits usage entirely. Summing them from 0 would report a
    # run that measured nothing as a run that spent nothing.
    _AGENT_TOKEN_KEYS = (
        "prompt_tokens", "completion_tokens",
        "cache_read_tokens", "reasoning_tokens", "api_call_count",
    )

    def _agent_slot(name: str) -> Dict[str, Any]:
        return {
            "name": name, "runs": 0, "turns": 0, "function_calls": 0,
            # Which model this stage actually ran on. The trace records it per
            # agent (run_metrics.MODEL_CAPTURE_BEFORE reads LlmRequest.model),
            # and dropping it here is what left the scorecard showing millions
            # of tokens attributed to nothing — the same "cannot be priced at
            # all" hole the callbacks were added to close. Stays None for the
            # deterministic stages, which take turns but never call a model.
            "model": None, "models": [],
            **{k: None for k in _AGENT_TOKEN_KEYS},
        }

    # Per-agent utilization, merged across runs.
    per_agent: Dict[str, Dict[str, Any]] = {}
    for r in instrumented:
        for entry in r.get("agents") or []:
            name = entry.get("name")
            if not name:
                continue
            slot = per_agent.setdefault(name, _agent_slot(name))
            slot["runs"] += 1
            slot["turns"] += entry.get("turns") or 0
            slot["function_calls"] += entry.get("function_calls") or 0
            for key in _AGENT_TOKEN_KEYS:
                val = entry.get(key)
                if isinstance(val, int):
                    slot[key] = (slot[key] or 0) + val
            # `runs` is newest-first, so the first model seen is the one in force
            # now; the rest are kept so a stage that was repointed mid-window
            # shows its history instead of silently claiming the totals all
            # belong to the current model.
            model = entry.get("model")
            if model and model not in slot["models"]:
                slot["models"].append(model)
                if slot["model"] is None:
                    slot["model"] = model

    # A stage's own scores and its latest note about what hindered it. Attached
    # to the agent row so the rail can show it beside that agent's utilization.
    # `self_score` stays None when the stage never offered one — an agent that
    # declined to judge itself has no score, which is not the same as a low one.
    for r in assessed:
        for rep in (r["self_assessment"].get("self_reports") or []):
            name = rep.get("agent")
            if not name:
                continue
            slot = per_agent.setdefault(name, _agent_slot(name))
            if isinstance(rep.get("score"), (int, float)):
                slot.setdefault("_self_scores", []).append(rep["score"])
            if rep.get("could_improve"):
                slot["could_improve"] = rep["could_improve"]
            if rep.get("went_well"):
                slot["went_well"] = rep["went_well"]
    for slot in per_agent.values():
        vals = slot.pop("_self_scores", None)
        slot["self_score"] = round(sum(vals) / len(vals), 3) if vals else None
        slot["self_scored_runs"] = len(vals or [])

    iters =[r.get("iterations_used") for r in instrumented if isinstance(r.get("iterations_used"), int)]
    # Turns the team kept taking after run_tests already reported green. The
    # LoopAgent never escalates, so this is the standing cost of that.
    wasted = [r.get("turns_after_pass") for r in instrumented
              if isinstance(r.get("turns_after_pass"), int)]

    last_sha = next((r.get("agent_py_sha") for r in runs if r.get("agent_py_sha")), None)

    return {
        "app": app,
        "window_days": days,
        # Measured pipeline health, distinct from the eval block above: that is
        # about whether runs succeeded, this is about whether the pipeline's own
        # stages are doing their jobs. A pipeline can pass every run and still
        # score badly here — that is exactly the disconnected-pipeline case.
        "self_assessment": {
            "assessed_runs": len(assessed),
            "score_avg": round(sum(assess_scores) / len(assess_scores), 3) if assess_scores else None,
            "score_last": assess_scores[0] if assess_scores else None,
            "failing_stages": sorted(
                ({"stage": s, "runs": n} for s, n in failing.items()),
                key=lambda x: (-x["runs"], x["stage"]),
            ),
            "instrumented": bool(assessed),
        },
        "run_count": total,
        "instrumented_count": len(instrumented),
        "eval": _eval_block(runs),
        "utilization": {
            "duration_ms": {
                "p50": _percentile(durations, 0.5),
                "p95": _percentile(durations, 0.95),
                "total": sum(durations) if durations else None,
                "sample": len(durations),
            },
            "model_calls": _metric(runs, "model_calls"),
            "tool_calls": _metric(runs, "tool_calls"),
            "tokens": _metric(runs, "total_tokens"),
            "iterations": {
                "used_avg": round(sum(iters) / len(iters), 2) if iters else None,
                "wasted_after_pass": sum(wasted) if wasted else None,
                "instrumented": bool(iters),
            },
            "workspaces": _dir_stats(os.path.join(state_dir, "workspaces")),
        },
        "self_report": {
            "scored_runs": len(scored),
            "agreed": agreed,
            "disagreed": len(scored) - agreed,
            "accuracy": round(agreed / len(scored), 3) if scored else None,
            "instrumented": bool(scored),
        },
        "per_agent": sorted(per_agent.values(), key=lambda a: a["name"]),
        "drift": {
            "current_sha": current_sha,
            "last_run_sha": last_sha,
            # None, not False: with no instrumented run we do not know, and saying
            # "no drift" would be a claim we cannot support.
            "changed_since_last_run": (
                None if not (current_sha and last_sha) else current_sha != last_sha
            ),
        },
        "runs": runs[:50],
    }
