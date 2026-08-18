"""invoke_workflow — the only thing in Hermes that talks to ADK.

Design principle: `worker` must not reason about invocation. It runs this
script; this script speaks HTTP. All intelligence lives in the ADK team, where
it is testable and eval-gated. That is deliberate — `worker` runs on a local
model with degraded tool-calling reliability, so anything needing multi-step
tool selection on the Hermes side is a design error.

Runs from a `no_agent` cron job, which executes in the gateway container and so
has host networking. (The `execute_code` sandbox does NOT — it runs in its own
network namespace and gets ECONNREFUSED on loopback. Verified, not assumed.)

Endpoint shapes were read from the running server's OpenAPI, not from any
writeup. Note the camelCase body keys: {appName, userId, sessionId, newMessage}.

Usage (no_agent cron):
    python invoke_workflow.py --app app.agents.gmail_inbox_triage \
        --run-id gmail-triage:2026-08-05T09:00
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

ADK_BASE = os.environ.get("ADK_BASE_URL", "http://127.0.0.1:8020")
STATE_ROOT = os.environ.get("ADK_STATE_DIR", "/opt/data/adk")
# The agents-cli project's source, as mounted into the gateway container. Used
# only to record which source actually ran; every read of it degrades to None.
WORKFLOWS_SRC_DIR = os.environ.get("WORKFLOWS_SRC_DIR", "/opt/workflows/app")
TRACES_DIR = os.path.join(STATE_ROOT, "traces")
USER_ID = "hermes-worker"

MAX_ATTEMPTS = 3          # 1 initial + 2 retries, per spec
BACKOFF_BASE_S = 2.0

# How long to wait for the ADK service to come back before giving up on a run.
#
# The service runs under `uvicorn --reload --reload-dir /code/app`, and /code/app
# is the tree agents author into. Every write restarts the server, so a cron slot
# that lands during an edit gets ECONNREFUSED through no fault of its own. The
# three-attempt ladder above spans 2s + 4s ≈ 6s, which is far shorter than a
# reload: uvicorn drains open connections, then re-imports every agent app, and
# a cold /list-apps on this host already takes ~2s on its own.
#
# So connection-refused is handled separately from the generic retry: wait for
# the port to answer again, then spend the normal attempts on the actual call. A
# run must only be reported failed when the *workflow* failed — reporting a
# routine authoring edit as a pipeline failure trains everyone to ignore the
# board, which is the exact failure this file exists to prevent.
READY_WAIT_S = float(os.environ.get("ADK_READY_WAIT_S", "180"))
READY_POLL_S = 3.0


class InvocationError(Exception):
    """Non-retryable failure: a bug, not a blip."""


class TransientError(Exception):
    """Retryable: transport failure or 5xx."""


class ServiceDownError(TransientError):
    """Nothing is listening on the ADK port — the server is restarting.

    Split from the generic transient case because the remedy differs. A 5xx or a
    timeout means the server is up and something inside it went wrong, so a short
    backoff is right. Connection-refused means the process is gone, and no amount
    of retrying in six seconds brings a uvicorn reload back; the caller waits for
    the port instead.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request(method: str, path: str, body: dict | None = None, timeout_s: int = 60):
    url = f"{ADK_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read()[:500].decode(errors="ignore")
        # 4xx is a bug in our request or the app name. Retrying hides it.
        if 400 <= e.code < 500:
            raise InvocationError(f"HTTP {e.code} on {method} {path}: {detail}") from e
        raise TransientError(f"HTTP {e.code} on {method} {path}: {detail}") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, ConnectionRefusedError):
            raise ServiceDownError(
                f"connection refused on {method} {path}: {e.reason}"
            ) from e
        raise TransientError(f"transport error on {method} {path}: {e.reason}") from e
    except http.client.RemoteDisconnected as e:
        # Uvicorn/h11 can close an in-flight request without an HTTP response
        # while the reload worker is cycling. Treat it like a transport blip so
        # the invoker writes a trace after bounded retries instead of crashing
        # before the dashboard can see what happened.
        raise TransientError(f"remote disconnected on {method} {path}: {e}") from e
    except TimeoutError as e:
        raise TransientError(f"timeout on {method} {path}") from e


def wait_for_service(budget_s: float = READY_WAIT_S) -> bool:
    """Block until /list-apps answers, or the budget runs out.

    Returns True if the service answered. Polls rather than sleeping a fixed
    interval so the common case — a reload that finishes in a few seconds —
    costs a few seconds, not the whole budget.

    Only connection-refused keeps us waiting. Any answer at all, including an
    error status, means the process is up and the run should proceed to fail on
    its own merits rather than here.
    """
    deadline = time.time() + budget_s
    while True:
        try:
            _request("GET", "/list-apps", timeout_s=10)
            return True
        except ServiceDownError:
            pass
        except (TransientError, InvocationError):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(READY_POLL_S)


# --- trace layer (§4) -------------------------------------------------------

def _trace_path(app: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    d = os.path.join(TRACES_DIR, app)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{day}.jsonl")


def find_completed_trace(app: str, run_id: str) -> dict | None:
    """Return a prior successful trace for this run_id, if one exists.

    This is what makes retries safe: a cron tick that fires twice, or a job
    re-run by hand, must not duplicate side effects.
    """
    d = os.path.join(TRACES_DIR, app)
    if not os.path.isdir(d):
        return None
    for name in sorted(os.listdir(d), reverse=True):
        if not name.endswith(".jsonl"):
            continue
        try:
            with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("run_id") == run_id and rec.get("status") in ("ok", "partial"):
                        return rec
        except OSError:
            continue
    return None


def write_trace(app: str, record: dict) -> None:
    with open(_trace_path(app), "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# --- utilization telemetry --------------------------------------------------
#
# Derived from the event list /run already returns, rather than asked of the
# agents. `metrics.model_calls` has been in result_schema.json and read below
# since the beginning, but nothing ever wrote it, so every trace recorded a
# literal 0 that looked like a measurement. Traces carrying TRACE_VERSION are
# the ones where these numbers are real.

# Version 3 adds what cost needs: which model each agent ran on, and the cached
# and reasoning token counts that version 2 discarded. It also removes
# `estimated_cost_usd`, which was written as a literal 0.0 on every run and was
# never anything but a placeholder wearing a measurement's clothes — the metrics
# store prices runs from tokens and a model, and a run with no model recorded is
# unpriced rather than free.
TRACE_VERSION = 3


def _parts(event: dict) -> list:
    content = event.get("content") or {}
    return content.get("parts") or []


def _tokens(event: dict) -> dict:
    """The four token counts, each None unless the provider reported it.

    LiteLLM against Ollama routinely omits usage entirely — that must read as
    "not measured", never as zero.

    `cache_read` is a breakdown of the input count on Gemini, not an addition to
    it, so nothing downstream may add the two together.
    """
    usage = event.get("usageMetadata") or event.get("usage_metadata") or {}

    def pick(*names):
        for name in names:
            value = usage.get(name)
            if isinstance(value, int):
                return value
        return None

    return {
        "prompt": pick("promptTokenCount", "prompt_token_count"),
        "completion": pick("candidatesTokenCount", "candidates_token_count"),
        "cache_read": pick("cachedContentTokenCount", "cached_content_token_count"),
        "reasoning": pick("thoughtsTokenCount", "thoughts_token_count"),
    }


def summarize_events(events: list) -> dict:
    """Per-agent turns, tool calls and tokens, plus loop-efficiency signals."""
    agents: dict = {}
    order: list = []
    authors: list = []
    model_calls = 0
    tool_calls = 0
    prompt_total = None
    completion_total = None
    cache_read_total = None
    reasoning_total = None
    first_pass_at = None

    for idx, event in enumerate(events or []):
        author = event.get("author") or "unknown"
        if author == "user":
            continue
        authors.append(author)
        slot = agents.setdefault(author, {
            "name": author, "turns": 0, "function_calls": 0,
            "prompt_tokens": None, "completion_tokens": None,
            "cache_read_tokens": None, "reasoning_tokens": None,
            # Filled from the run's own state below when the model-capture
            # callbacks are attached. None means the agent ran on a model this
            # trace cannot name — not that it ran on none.
            "model": None,
        })
        if author not in order:
            order.append(author)
        slot["turns"] += 1
        model_calls += 1

        for part in _parts(event):
            if part.get("functionCall") or part.get("function_call"):
                slot["function_calls"] += 1
                tool_calls += 1
            response = part.get("functionResponse") or part.get("function_response")
            if response and first_pass_at is None:
                body = response.get("response") or {}
                if isinstance(body, dict) and body.get("passed") is True:
                    first_pass_at = idx

        counts = _tokens(event)
        if isinstance(counts["prompt"], int):
            slot["prompt_tokens"] = (slot["prompt_tokens"] or 0) + counts["prompt"]
            prompt_total = (prompt_total or 0) + counts["prompt"]
        if isinstance(counts["completion"], int):
            slot["completion_tokens"] = (slot["completion_tokens"] or 0) + counts["completion"]
            completion_total = (completion_total or 0) + counts["completion"]
        if isinstance(counts["cache_read"], int):
            slot["cache_read_tokens"] = (slot["cache_read_tokens"] or 0) + counts["cache_read"]
            cache_read_total = (cache_read_total or 0) + counts["cache_read"]
        if isinstance(counts["reasoning"], int):
            slot["reasoning_tokens"] = (slot["reasoning_tokens"] or 0) + counts["reasoning"]
            reasoning_total = (reasoning_total or 0) + counts["reasoning"]

    # Loop passes: collapse runs of the same author, then count how often the
    # first agent comes back around.
    collapsed = [a for i, a in enumerate(authors) if i == 0 or a != authors[i - 1]]
    iterations = collapsed.count(collapsed[0]) if collapsed else 0

    # Turns spent after the fitness function already reported green. The
    # LoopAgent never escalates, so this is the cost of that.
    turns_after_pass = None
    if first_pass_at is not None:
        turns_after_pass = sum(
            1 for e in (events or [])[first_pass_at + 1:] if (e.get("author") or "") != "user"
        )

    # Prompt plus completion only. Cache reads are already counted inside the
    # prompt total by the provider, and reasoning tokens inside the completion,
    # so adding either again would inflate the run's total by however much
    # caching happened to save.
    total_tokens = None
    if prompt_total is not None or completion_total is not None:
        total_tokens = (prompt_total or 0) + (completion_total or 0)

    return {
        "agents": [agents[a] for a in order],
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "prompt_tokens": prompt_total,
        "completion_tokens": completion_total,
        "cache_read_tokens": cache_read_total,
        "reasoning_tokens": reasoning_total,
        "total_tokens": total_tokens,
        "iterations_used": iterations,
        "turns_after_pass": turns_after_pass,
    }


def app_source_dir(app: str) -> str | None:
    """Map an ADK app name onto its source directory under the mounted project.

    `app.agents.gmail_inbox_triage` -> <src>/agents/gmail_inbox_triage
    `app`                           -> <src>
    """
    if not os.path.isdir(WORKFLOWS_SRC_DIR):
        return None
    if app == "app":
        return WORKFLOWS_SRC_DIR
    if not app.startswith("app."):
        return None
    return os.path.join(WORKFLOWS_SRC_DIR, *app.split(".")[1:])


def agent_py_sha(app: str) -> str | None:
    """sha1 of the app's agent.py, so a trace records which team actually ran.

    ADK imports agent.py once at startup and the server has no --reload, so an
    edited team is not necessarily the running team. Recording this is what lets
    the dashboard say the two have diverged.
    """
    src = app_source_dir(app)
    if not src:
        return None
    path = os.path.join(src, "agent.py")
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None


# --- result extraction + validation (§2.5, §3) ------------------------------

def extract_result(events: list) -> dict | None:
    """Pull the structured result out of the event list.

    Never indexes positionally. Prefers the emit_result tool's response, since
    prose from a final text event is not a contract. Falls back to parsing a
    JSON object out of the last text only if no tool response is present.
    """
    if not isinstance(events, list):
        return None

    # Walk backwards: the last emit_result wins.
    for event in reversed(events):
        content = (event or {}).get("content") or {}
        for part in content.get("parts") or []:
            resp = part.get("functionResponse") or part.get("function_response")
            if not resp:
                continue
            if resp.get("name") == "emit_result":
                payload = resp.get("response")
                if isinstance(payload, dict):
                    # ADK may wrap the return value.
                    for key in ("result", "response", "output"):
                        if isinstance(payload.get(key), dict):
                            return payload[key]
                    return payload

    # Fallback: a JSON object embedded in the final text.
    for event in reversed(events):
        content = (event or {}).get("content") or {}
        for part in content.get("parts") or []:
            text = part.get("text")
            if not text:
                continue
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue
    return None


def validate_result(result: dict, schema_path: str | None) -> list:
    """Validate against the team's schema. Returns a list of problems.

    Deliberately dependency-free: this runs inside the Hermes container, which
    does not ship jsonschema. It enforces the structural guarantees the spec
    depends on — required keys, status enum, metrics shape — which is what
    stops malformed output reaching the approval queue.
    """
    problems = []
    if not isinstance(result, dict):
        return ["result is not a JSON object"]

    required = ["status", "items", "needs_review", "errors", "metrics"]
    schema = None
    if schema_path and os.path.exists(schema_path):
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
            required = schema.get("required", required)
        except (OSError, json.JSONDecodeError) as e:
            problems.append(f"could not read schema: {e}")

    for key in required:
        if key not in result:
            problems.append(f"missing required key {key!r}")

    status = result.get("status")
    if status not in ("ok", "partial", "failed"):
        problems.append(f"status {status!r} not one of ok/partial/failed")

    for key in ("items", "needs_review", "errors"):
        if key in result and not isinstance(result[key], list):
            problems.append(f"{key} must be a list, got {type(result[key]).__name__}")

    metrics = result.get("metrics")
    if metrics is not None:
        if not isinstance(metrics, dict):
            problems.append("metrics must be an object")
        else:
            for key in ("input_count", "output_count"):
                if key in metrics and not isinstance(metrics[key], int):
                    problems.append(f"metrics.{key} must be an integer")
    return problems


# --- invocation (§2) --------------------------------------------------------

def _run_once(app: str, payload: dict, timeout_s: int) -> list:
    """One full attempt: fresh session, run, delete session.

    The session is deleted in a finally block so a failed run cannot leak
    sessions into the service — the spec asks for an assertion that none
    accumulate, and this is what makes that true.
    """
    session_id = f"s_{uuid.uuid4().hex[:16]}"
    base = f"/apps/{app}/users/{USER_ID}/sessions/{session_id}"
    _request("POST", base, {}, timeout_s=30)
    try:
        events = _request(
            "POST",
            "/run",
            {
                "appName": app,
                "userId": USER_ID,
                "sessionId": session_id,
                "newMessage": {
                    "role": "user",
                    "parts": [{"text": json.dumps(payload)}],
                },
            },
            timeout_s=timeout_s,
        )
        return events or []
    finally:
        try:
            _request("DELETE", base, timeout_s=30)
        except Exception:
            # Never let cleanup mask the real outcome; the trace records it.
            pass


def apply_model_usage(agents: list, model_usage: list | None) -> None:
    """Fold the run's own per-model record onto the per-agent rows, in place.

    The event stream says how many turns an agent took and what the provider
    reported; only the pipeline knows which model it called, because the model
    name is on the request and never reaches the trace. `app/run_metrics.py`
    records it during the run and returns it on the result, and this is where
    the two halves meet.

    An agent that appears in the usage record but not in the event stream is
    added rather than dropped: it spent tokens, so leaving it out would lose
    them, and a row with turns=0 is a truthful description of an agent whose
    turns were not visible in the events.
    """
    if not model_usage:
        return
    by_name = {a.get("name"): a for a in agents}
    for entry in model_usage:
        name = entry.get("agent")
        if not name:
            continue
        slot = by_name.get(name)
        if slot is None:
            slot = {"name": name, "turns": 0, "function_calls": 0,
                    "prompt_tokens": None, "completion_tokens": None,
                    "cache_read_tokens": None, "reasoning_tokens": None,
                    "model": None}
            agents.append(slot)
            by_name[name] = slot
        slot["model"] = entry.get("model") or slot.get("model")
        slot["api_call_count"] = entry.get("api_call_count")
        # Only fill a token field the event stream left unmeasured. Where both
        # saw it, the event stream already summed every turn and the callback
        # record would be the same number counted a second time.
        for field in ("cache_read_tokens", "reasoning_tokens"):
            if slot.get(field) is None and isinstance(entry.get(field), int):
                slot[field] = entry[field]


class DailyCapExceeded(InvocationError):
    """The day's model spend is already at the cap. Refuse to start."""


def check_daily_cap() -> dict | None:
    """Ask the ADK service what today has cost, and refuse if it is at the cap.

    This is the replacement for the per-key budgets the LiteLLM proxy would have
    enforced. It is a pre-flight check rather than a mid-run abort on purpose: a
    workflow killed halfway leaves partial side effects — a draft created, a
    label applied, a task filed — and the case this exists for is a loop that
    dispatches repeatedly, which a pre-flight check catches on the next pass.

    Scope is WORKFLOWS ONLY. Hermes chat and cron call Anthropic directly and
    never appear in this ledger, so this is not a spend limit for the host; the
    account-level limit at the vendor is. Two different guarantees.

    A service that cannot answer does NOT block the run. The cap is a guard on
    runaway spend, not an authorization gate, and turning "the cost endpoint is
    unreachable" into "no workflow runs today" would be a worse outage than the
    one it protects against — the same reasoning as the ready-wait above.
    """
    try:
        position = _request("GET", "/cost", timeout_s=10)
    except (TransientError, InvocationError) as e:
        print(f"cost check unavailable, proceeding: {e}", file=sys.stderr)
        return None
    if not isinstance(position, dict) or not position.get("over"):
        return position
    raise DailyCapExceeded(
        f"daily model spend cap reached: "
        f"${position.get('spent_today_usd', 0):.4f} of "
        f"${position.get('cap_usd', 0):.2f} spent today. Raise "
        f"WORKFLOWS_DAILY_COST_CAP_USD or wait for the UTC day to roll over."
    )


def derive_honesty(result: dict) -> dict:
    """Compare what a run CLAIMED about itself against what was MEASURED.

    These three fields were declared on the trace record from the beginning and
    read straight off the result payload — `result.get("measured_passed")` and
    friends. Nothing ever put them there: they are optional fields on a schema
    ADK does not enforce, so every model skipped all three. Across 439 recorded
    runs exactly one carried them. The field that exists to catch a workflow
    claiming success while failing was itself silently absent.

    Asking the model for `measured_passed` was the mistake. A measurement the
    subject reports is a claim. So the measured half is derived HERE, from the
    checkpoints, and only the claim is taken from the payload:

    * ``measured_passed`` — every declared checkpoint passed. ``None`` when no
      stage declared one, which is unmeasured rather than failed (the same rule
      `file_health_task` applies to a null score).
    * ``self_reported_status`` — the run's own claim, an explicit
      `self_reported_status` if a workflow offers one, else the `status` it
      emitted. A claim either way; never treated as evidence.
    * ``self_report_accurate`` — whether the two agree, and ``None`` when either
      side is missing. A disagreement is the signal worth having: it means the
      workflow said "ok" while its own checkpoints said otherwise.
    """
    assessment = result.get("self_assessment") or {}
    score = assessment.get("score")
    measured_passed = None if score is None else bool(score >= 1.0)

    claimed = result.get("self_reported_status") or result.get("status")
    claimed = str(claimed).strip().lower() if claimed else None

    # Only a claim the checkpoints can actually adjudicate is judged. Stage
    # checkpoints measure whether the stages worked, NOT how much the run
    # yielded, so a `partial` — every stage healthy, three of ten items
    # extracted — is outside what they can speak to. Scoring it against them
    # would file honest partial runs as dishonest ones, and a review that cries
    # wolf on its most common non-ok status is a review nobody reads.
    if claimed not in ("ok", "failed") or measured_passed is None:
        accurate = None
    else:
        accurate = (claimed == "ok") == measured_passed

    return {
        "measured_passed": measured_passed,
        "self_reported_status": claimed,
        "self_report_accurate": accurate,
    }


def invoke_workflow(app: str, payload: dict, run_id: str, timeout_s: int = 300,
                    trigger: str | None = None) -> dict:
    """Invoke an ADK workflow. Returns the validated result dict.

    `trigger` records what caused the run — 'cron', 'manual', 'chat' — so the
    store can tell scheduled load apart from someone pressing a button.

    It falls back to `$WORKFLOW_TRIGGER` before 'unknown' so a scheduled job can
    label every run it starts by setting one environment variable, without each
    of the six runner scripts under workflows/scripts/ having to grow an
    argument and pass it through. Unset stays 'unknown' rather than defaulting
    to 'manual': a run nobody labelled is unattributed, and quietly filing it
    under manual would misreport how much of the fleet runs unattended.
    """
    trigger = trigger or os.environ.get("WORKFLOW_TRIGGER") or "unknown"
    prior = find_completed_trace(app, run_id)
    if prior:
        return {
            "status": prior.get("status", "ok"),
            "run_id": run_id,
            "app": app,
            "idempotent_hit": True,
            "output_summary": prior.get("output_summary"),
        }

    src = app_source_dir(app)
    schema_path = os.path.join(src, "result_schema.json") if src else ""
    input_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:16]

    # After the idempotency check: replaying a completed run costs nothing, so
    # the cap has no business refusing it.
    try:
        check_daily_cap()
    except DailyCapExceeded as e:
        # Traced like any other failure so the refusal is visible in the same
        # place every other run outcome is. A refusal that only appeared in cron
        # stderr would look like the workflow silently stopped happening.
        write_trace(app, {
            "run_id": run_id, "app": app, "started_at": _now(),
            "duration_ms": 0, "status": "failed", "attempt": 0,
            "trigger": trigger, "input_hash": input_hash,
            "output_summary": None,
            "error": f"DailyCapExceeded: {e}",
            "trace_version": TRACE_VERSION,
            "model_calls": 0, "agents": [], "tool_calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
            "cache_read_tokens": None, "reasoning_tokens": None,
            "total_tokens": 0, "iterations_used": 0,
            "agent_py_sha": agent_py_sha(app), "roster": [],
            "raw_response": None,
        })
        raise SystemExit(f"WORKFLOW REFUSED app={app} run_id={run_id}: {e}")

    started = time.time()
    last_error = None
    last_events = None

    # `attempt` counts calls the server actually answered. A wait for a
    # restarting server is not an attempt — nothing was tried — so it does not
    # advance the counter. `waits` bounds that separately, so a server that
    # flaps up and down cannot loop here forever.
    attempt = 0
    waits = 0
    while attempt < MAX_ATTEMPTS and waits <= MAX_ATTEMPTS:
        attempt += 1
        try:
            events = _run_once(app, payload, timeout_s)
            last_events = events
            result = extract_result(events)
            if result is None:
                # No emit_result and no parseable JSON: a contract violation,
                # not a transient fault. Retrying would just burn quota.
                raise InvocationError("no structured result in run events")

            problems = validate_result(result, schema_path)
            if problems:
                raise InvocationError(f"schema validation failed: {problems}")

            usage = summarize_events(events)
            metrics = result.get("metrics") or {}
            apply_model_usage(usage["agents"], result.get("model_usage"))
            honesty = derive_honesty(result)
            record = {
                "run_id": run_id,
                "app": app,
                "started_at": _now(),
                "duration_ms": int((time.time() - started) * 1000),
                "status": result.get("status", "ok"),
                "attempt": attempt,
                "input_hash": input_hash,
                "trigger": trigger,
                "output_summary": {
                    "items": len(result.get("items") or []),
                    "needs_review": len(result.get("needs_review") or []),
                    "errors": len(result.get("errors") or []),
                },
                "model_calls": usage["model_calls"],
                # No `estimated_cost_usd`. It was written as a literal 0.0 on
                # every run, which read as "this run was free" rather than "no
                # one priced it". Cost belongs to the metrics store, which has
                # the model, the token kinds and a price table, and which
                # reports an unpriced run as unpriced.
                "error": None,
                # --- utilization (measured, not self-reported) ---
                "trace_version": TRACE_VERSION,
                "agents": usage["agents"],
                # What the run touched and produced, in the shared vocabulary
                # from app/run_metrics.py. This is the part that is comparable
                # across pipelines; anything under `extra` is not.
                "metrics": {
                    "touched": metrics.get("touched"),
                    "produced": metrics.get("produced"),
                    "extra": metrics.get("extra"),
                },
                "tool_calls": metrics.get("tool_calls") or usage["tool_calls"],
                "tool_calls_by_name": metrics.get("tool_calls_by_name"),
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "cache_read_tokens": usage["cache_read_tokens"],
                "reasoning_tokens": usage["reasoning_tokens"],
                "total_tokens": usage["total_tokens"],
                "iterations_used": usage["iterations_used"],
                "turns_after_pass": usage["turns_after_pass"],
                # --- self-reported vs measured ---
                # The pipeline's own health check. Recorded on the run so the
                # scorecard can chart it over time, and returned to the caller
                # so a bad score can become a development task. `score` here is
                # measured; anything under self_assessment.self_reports is a
                # model's claim about itself and is kept separate on purpose.
                "self_assessment": result.get("self_assessment"),
                "self_reported_status": honesty["self_reported_status"],
                "measured_passed": honesty["measured_passed"],
                "self_report_accurate": honesty["self_report_accurate"],
                # --- which team actually ran ---
                "agent_py_sha": agent_py_sha(app),
                "roster": [a["name"] for a in usage["agents"]],
            }
            write_trace(app, record)
            result["run_id"] = run_id
            result["app"] = app
            return result

        except ServiceDownError as e:
            # The server is restarting — almost always a hot reload triggered by
            # someone editing an agent. Wait for the port rather than burning an
            # attempt on a six-second backoff that cannot outlast a reload. This
            # does not consume an attempt: nothing about the request was tried.
            last_error = f"{type(e).__name__}: {e}"
            attempt -= 1
            waits += 1
            if not wait_for_service():
                break
            continue
        except TransientError as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt < MAX_ATTEMPTS:
                time.sleep(BACKOFF_BASE_S ** attempt)
                continue
        except InvocationError as e:
            # No retry: a 4xx or a schema violation is a bug, and retrying
            # hides it. Preserve the raw failure in the trace for debugging.
            last_error = f"{type(e).__name__}: {e}"
            break

    failed_usage = summarize_events(last_events or [])
    write_trace(app, {
        "run_id": run_id, "app": app, "started_at": _now(),
        "duration_ms": int((time.time() - started) * 1000),
        "status": "failed", "attempt": attempt, "input_hash": input_hash,
        "trigger": trigger,
        "output_summary": None, "model_calls": failed_usage["model_calls"],
        "error": last_error,
        # A failed run is the one you most want utilization for — it is where
        # the quota went.
        "trace_version": TRACE_VERSION,
        "agents": failed_usage["agents"],
        "tool_calls": failed_usage["tool_calls"],
        "prompt_tokens": failed_usage["prompt_tokens"],
        "completion_tokens": failed_usage["completion_tokens"],
        "cache_read_tokens": failed_usage["cache_read_tokens"],
        "reasoning_tokens": failed_usage["reasoning_tokens"],
        "total_tokens": failed_usage["total_tokens"],
        "iterations_used": failed_usage["iterations_used"],
        "agent_py_sha": agent_py_sha(app),
        "roster": [a["name"] for a in failed_usage["agents"]],
        # §5: preserve the raw response on a validation failure. Without it you
        # are debugging a schema error with no idea what the team actually said.
        "raw_response": (json.dumps(last_events)[:4000] if last_events else None),
    })
    raise SystemExit(f"WORKFLOW FAILED app={app} run_id={run_id}: {last_error}")


# --- development tasks from a bad run ---------------------------------------
#
# The review queue and the Kanban board answer different questions. A queued
# draft asks "is this output good enough to send" — that is the workflow doing
# its job and asking for a decision. A low self-assessment asks "is this
# pipeline working at all", which is nobody's decision and everybody's problem:
# it is development work, and it belongs on the board.
#
# Filed by the wrapper rather than by an agent because these jobs are `no_agent`
# — there is no model in the loop to notice — and because the decision needs no
# judgement: the score is measured and the threshold is a number.
#
# The task is a repair job, not a notification. It opens on the workflow source,
# assigned to an agent, carrying what failed and instructions to fix it: that is
# what closes the loop from "a pipeline noticed it is broken" to "a pipeline got
# fixed". The one thing the instructions forbid is making the score pass without
# making the pipeline work — an assessment that can be edited into agreement is
# worth nothing, so a fault the agent cannot genuinely fix must end as `blocked`
# with a reason rather than as a green checkpoint.

HEALTH_BOARD = os.environ.get("WORKFLOW_HEALTH_BOARD", "default")
# `dev`, not `worker` and not `default`. This is a repair job on the workflows
# source, so it wants the profile that carries GSD's planning discipline and
# owns that tree by role -- see the profile's own description, which is what
# the kanban decomposer routes on.
#
# It used to say `default`, justified as "the profile whose write-safe root
# covers /opt/workflows". That reason was never true: HERMES_WRITE_SAFE_ROOT is
# a container-level env var (/opt/data:/opt/workflows), read identically by
# every profile. Nothing about write access was ever default-specific.
#
# Routing it to `default` had a real cost once GSD was installed there: the
# conversational profile grew a 70-line skill index, and every repair task
# spawned an agent that met gsd-autonomous ("run all remaining phases
# autonomously") while sitting in a git-tracked source tree. Splitting the
# roles fixes both ends -- `default` stays lean, and repairs get the better
# planner.
HEALTH_ASSIGNEE = os.environ.get("WORKFLOW_HEALTH_ASSIGNEE", "dev")
# The agents-cli project as mounted into the gateway container, where the agent
# that picks this task up will actually be running. The first attempt at this
# filed into a `scratch` workspace and the assignee blocked immediately and
# correctly: "does not contain the app.agents project source code". A repair
# task has to open on the code it is meant to repair.
HEALTH_WORKSPACE = os.environ.get("WORKFLOW_HEALTH_WORKSPACE", "dir:/opt/workflows")


def _health_task_body(app: str, run_id: str, assessment: dict) -> str:
    lines = [
        f"Pipeline `{app}` reported a failing self-assessment.",
        "",
        f"- run_id: `{run_id}`",
        f"- measured score: **{assessment.get('score')}** "
        f"({assessment.get('checkpoints_passed')}/{assessment.get('checkpoints_total')} checkpoints)",
        "",
        "## Checkpoints",
    ]
    for c in assessment.get("checkpoints") or []:
        mark = "PASS" if c.get("ok") else "FAIL"
        lines.append(f"- [{mark}] `{c.get('stage')}` — {c.get('detail') or 'no detail'}")

    errors = assessment.get("errors") or []
    if errors:
        lines += ["", "## Errors"] + [f"- {e}" for e in errors]

    reports = assessment.get("self_reports") or []
    if reports:
        lines += [
            "",
            "## What the model stages said about themselves",
            "",
            "_Self-reported, not measured — a lead to check, not evidence._",
            "",
        ]
        for r in reports:
            lines.append(f"**{r.get('agent')}** (self-scored {r.get('score')})")
            if r.get("went_well"):
                lines.append(f"- went well: {r['went_well']}")
            if r.get("could_improve"):
                lines.append(f"- could improve: {r['could_improve']}")
            lines.append("")

    src = app_source_dir(app) or "/opt/workflows/app"
    lines += [
        "",
        "## Fix it",
        "",
        f"The pipeline's source is at `{src}` — stages in `stages.py`, the step order",
        "in `agent.py`, model instructions in `prompt.py`, typed I/O in `schema.py`.",
        "That tree is writable and inside this profile's write-safe root, so you can",
        "edit it directly.",
        "",
        "Work the failing checkpoints above, in order:",
        "",
        "1. Read the stage that failed and its recorded reason.",
        "2. Decide what kind of fault it is. **Only a code fault is yours to fix.**",
        "   A stage failing for want of a credential, a missing mount, an absent",
        "   env var or an unmounted directory is a deployment fault — no edit to",
        "   this repo makes it work, and editing around it makes the pipeline lie",
        "   about its own health. Block instead.",
        "3. For a code fault: make the smallest change that makes the stage do its",
        "   job, and keep the pipeline honest — a stage must still report `ok=False`",
        "   when it has not done its work. Never make a checkpoint pass by removing",
        "   it, loosening it, or reporting success unconditionally. That raises the",
        "   score without fixing anything, which is worse than the original fault.",
        "4. Verify: restart the workflows service so ADK reloads the app",
        "   (`docker compose restart workflows` on the host), then re-run the",
        "   wrapper and check the new `self_assessment.score`.",
        "",
        "If you cannot make it work, that is a legitimate outcome — say so rather",
        "than leaving the task open or guessing:",
        "",
        "```",
        "hermes kanban block <task_id> --kind <capability|needs_input|dependency|transient> \\",
        '    "what you tried, what the actual obstacle is, and what would unblock it"',
        "```",
        "",
        "Use `needs_input` for a missing credential or config, `capability` for",
        "something this environment cannot do, `transient` for a fault you expect to",
        "clear on its own. Name the concrete obstacle — this is read by a human",
        "deciding what to change.",
    ]
    return "\n".join(lines)


# Statuses that mean "nobody is looking at this any more". A health task in one
# of these must NOT suppress a new one: the fault came back, and a fault nobody
# can see is a fault nobody fixes.
CLOSED_STATUSES = {"done", "archived"}


def health_task_title(app: str, failing) -> str:
    """The dedup identity of a health task: the app and what is broken in it.

    Deliberately free of the score. The score is a measurement of one run and
    moves between runs — 0.0 when invocation never happened, 0.667 when two of
    three stages worked — so putting it in the title made two reports of the
    same fault look like two different faults. It belongs in the body, which is
    where the run-specific detail lives.
    """
    return f"[{app}] not working: {', '.join(sorted(failing))}"


def _open_health_task(title: str) -> str | None:
    """Return the id of an *active* task with this title, else None.

    This is the check `hermes kanban create --idempotency-key` cannot do for us.
    Its own dedup matches on `status != 'archived'`, so a task that was worked
    and marked `done` keeps matching forever: the second time a pipeline breaks
    the same way, create quietly returns the id of the closed task and files
    nothing. That is how a ten-minute job failed in the open with an empty
    board. Only an open task should suppress a new one.

    Matching is on title rather than on the idempotency key because the key is
    not exposed by `kanban list --json`. Never raises — an unreachable board
    means "not found", so the caller tries to file rather than staying silent.
    """
    cmd = ["hermes", "kanban", "--board", HEALTH_BOARD, "list", "--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        tasks = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(tasks, list):
        return None
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task.get("title") != title:
            continue
        if (task.get("status") or "").lower() in CLOSED_STATUSES:
            continue
        return task.get("id")
    return None


def file_health_task(
    app: str, run_id: str, assessment: dict | None, threshold: float = 1.0
) -> str | None:
    """Open a development task when a run's measured self-assessment is bad.

    Returns the task id, or None when nothing was filed. Never raises: a
    workflow run must not fail because the board was unreachable.

    Deduplication is on the app and the *set of failing stages*, not on the run.
    A job firing every ten minutes would otherwise open 144 identical tasks a
    day; keyed this way, a persistent fault holds one open task and a new kind
    of fault opens a new one.

    It is scoped to tasks that are still open. A `done` task means somebody
    already fixed this once — if the pipeline is reporting it again, that fix
    did not hold, and that is news rather than a duplicate.

    A score of None means no stage declared a checkpoint — unmeasured, not bad —
    and files nothing. Reporting that as a failure would be the same mistake as
    charting it as zero.
    """
    if not assessment:
        return None
    score = assessment.get("score")
    if score is None or score >= threshold:
        return None

    failing = assessment.get("failed_stages") or ["unknown"]
    title = health_task_title(app, failing)

    already_open = _open_health_task(title)
    if already_open:
        return already_open

    # Run-scoped, so the CLI's own `status != 'archived'` dedup can never match a
    # closed task from a previous outbreak and swallow this one. It still guards
    # the case it needs to: two invocations of the same run retrying against the
    # board file one task, not two. Cross-run dedup is `_open_health_task`'s job.
    key = f"workflow-health:{app}:{','.join(sorted(failing))}:{run_id}"

    cmd = [
        "hermes", "kanban", "--board", HEALTH_BOARD, "create", title,
        "--body", _health_task_body(app, run_id, assessment),
        "--assignee", HEALTH_ASSIGNEE,
        # Opens on the workflow source so the assignee can actually repair it.
        "--workspace", HEALTH_WORKSPACE,
        "--idempotency-key", key,
        "--created-by", "invoke_workflow",
        "--json",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"could not file health task: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"could not file health task: {proc.stderr.strip()[:300]}", file=sys.stderr)
        return None
    try:
        return (json.loads(proc.stdout) or {}).get("id")
    except json.JSONDecodeError:
        return (proc.stdout or "").strip()[:64] or None


def main() -> None:
    p = argparse.ArgumentParser(description="Invoke an ADK workflow over HTTP.")
    p.add_argument("--app", required=True)
    p.add_argument("--run-id", default=None,
                   help="Idempotency key. Defaults to app:<UTC hour>.")
    p.add_argument("--payload", default="{}", help="JSON workflow input.")
    p.add_argument("--timeout", type=int, default=300)
    args = p.parse_args()

    run_id = args.run_id or f"{args.app}:{datetime.now(timezone.utc):%Y-%m-%dT%H:00}"
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        raise SystemExit(f"--payload is not valid JSON: {e}")

    result = invoke_workflow(args.app, payload, run_id, args.timeout)
    # stdout is delivered verbatim by no_agent cron, so keep it readable.
    print(json.dumps(result, indent=2)[:3000])


if __name__ == "__main__":
    main()
