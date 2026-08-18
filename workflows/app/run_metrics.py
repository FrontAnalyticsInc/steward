"""What a run did, in words every pipeline uses the same way.

Two problems live here, and they are the same problem at two levels.

**What the run produced.** Every pipeline already returns a `metrics` dict, and
every pipeline invented its own keys for it — `pages_seen` in one,
`input_count`/`output_count` in another, `meeting_count` in a third. Each is
readable on its own and none of them add up, so there is no answer to "how many
drafts did the fleet write this week" short of reading nine stage files. The
vocabulary below is frozen for exactly that reason: `Touched` and `Produced` are
enums, not free strings, so a typo is a validation error rather than a metric
that silently counts nothing. Anything genuinely specific to one pipeline goes
in `extra`, which is stored and shown but never summed across agents.

The distinction the vocabulary is built around is how much supervision the mail
that leaves this system got. `draft_email` is composed and sitting in a mailbox,
delivered to nobody. `approved_email` went out because a human read it and said
so. `auto_email` left the building unattended. Folding them into "emails" would
erase the only number in this system that says how much unsupervised sending is
happening, and the gap between the last two is the one worth watching.

These are counted where the effect becomes real, which is not always where the
work was decided: a reply queued by the triage agent is a `review_item` in that
run, and becomes a `draft_email` or an `approved_email` later, in whichever run
of the review executor actually carried the decision out.

**What the run spent.** ADK reports token usage on the event stream, but only
what the provider chose to report, and only in two buckets — which is how
`estimated_cost_usd` came to be hardcoded to `0.0`: nothing recorded which model
an agent actually ran on, so nothing could price it. The callbacks here sit on
the model call itself, where both facts are available: `LlmRequest.model` going
out, and `LlmResponse.usage_metadata` coming back, including the cached and
reasoning counts that never reached the trace.

They are deliberately additive to whatever else an agent already does — ADK 2.6
accepts a list of callbacks, so a stage keeps `apply_user_context` and gains
these by appending, not by wrapping.

One rule throughout: **absent is not zero.** LiteLLM against Ollama routinely
omits usage entirely, and a run whose tokens went unreported did not spend zero
of them. Every counter here is `None` until something measures it, and a model
call with no usage still records that the call happened.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# State keys. Verbose on purpose — they share a namespace with pipeline data,
# the same reason app/self_assessment.py spells its keys out.
MODEL_USAGE_KEY = "run_metrics_model_usage"
PENDING_MODEL_KEY = "run_metrics_pending_model"


class Touched(StrEnum):
    """Data elements a run read or considered.

    Volume of input, not of work: a triage run that reads 40 messages and files
    2 has touched 40. Reading is what costs tokens, so this is the denominator
    for almost every efficiency question worth asking.
    """

    email = "email"
    calendar_event = "calendar_event"
    contact = "contact"
    company = "company"
    web_page = "web_page"
    graph_node = "graph_node"
    document = "document"
    kanban_task = "kanban_task"


class Produced(StrEnum):
    """Artifacts a run created — meaning a side effect actually happened.

    Count something here only once the effect is real. A draft that was composed
    and then dropped because a later stage failed is not a `draft_email`; it is
    nothing, and counting it would make the fleet look more productive the more
    it broke.
    """

    # A draft that exists in Gmail. Reversible: nothing has been delivered, and
    # a human still has to press send in the mail client.
    #
    # This counts the draft, NOT the intention to write one. Queuing a reply for
    # review is `review_item`; it becomes a `draft_email` only when the executor
    # has actually created it. The two were once emitted together at queue time,
    # which made every queued item look like a draft that existed in a mailbox
    # when nothing had been created at all.
    draft_email = "draft_email"
    # Sent, unattended, with no human in the loop. Not reversible. Kept
    # separate from draft_email permanently and on purpose.
    auto_email = "auto_email"
    # Sent, irreversibly, because a human read it and approved it. The third
    # case, and it needs its own name: folding it into `auto_email` would
    # overstate how much goes out unwatched, and leaving it as `draft_email`
    # would report delivered mail as a reversible draft sitting in a queue.
    # Together these three answer "how much mail left, and who was watching".
    approved_email = "approved_email"
    document = "document"
    crm_task = "crm_task"
    kanban_task = "kanban_task"
    graph_node = "graph_node"
    graph_edge = "graph_edge"
    approval_item = "approval_item"
    review_item = "review_item"
    calendar_event = "calendar_event"


class RunMetrics(BaseModel):
    """The typed replacement for each pipeline's bespoke `metrics` dict.

    `touched` and `produced` are comparable across every agent; `extra` is not,
    and is kept apart so that nothing downstream is tempted to add
    `pages_discovered` to `meeting_count` just because both are numbers.
    """

    touched: dict[Touched, int] = Field(default_factory=dict)
    produced: dict[Produced, int] = Field(default_factory=dict)
    extra: dict[str, float] = Field(default_factory=dict)

    @field_validator("touched", "produced")
    @classmethod
    def _no_negatives(cls, value: dict) -> dict:
        """A count below zero is a bug in the caller, not a small number.

        Rejecting it here means the store never has to defend against it, and
        the stack trace names the stage that produced it.
        """
        bad = {k: v for k, v in value.items() if v < 0}
        if bad:
            raise ValueError(f"counts cannot be negative: {bad}")
        return value

    def model_dump_trace(self) -> dict:
        """The shape written into the trace record.

        Enum keys become their string values so the JSONL is readable and the
        store can unnest it without knowing this module exists.
        """
        return {
            "touched": {str(k): v for k, v in self.touched.items()},
            "produced": {str(k): v for k, v in self.produced.items()},
            "extra": dict(self.extra),
        }


# --- model and token capture ------------------------------------------------


def _usage_fields(usage: Any) -> dict:
    """Pull the four token counts off a provider's usage metadata.

    Every one stays `None` when the provider did not report it. Gemini fills all
    four; LiteLLM against Ollama frequently fills none, and this is the layer
    that has to keep those two cases distinguishable — a zero here would become
    a confident zero on a cost report.

    Note `prompt_token_count` already includes cached tokens on Gemini, so
    `cache_read_tokens` is a *breakdown* of the input, not an addition to it.
    Anything summing these must not add cache reads to input.
    """
    def get(*names: str) -> Optional[int]:
        for name in names:
            value = getattr(usage, name, None)
            if isinstance(value, int):
                return value
        return None

    return {
        "input_tokens": get("prompt_token_count", "promptTokenCount"),
        "output_tokens": get("candidates_token_count", "candidatesTokenCount"),
        "cache_read_tokens": get("cached_content_token_count", "cachedContentTokenCount"),
        "reasoning_tokens": get("thoughts_token_count", "thoughtsTokenCount"),
    }


def _read_state(callback_context: Any, key: str, default):
    try:
        value = callback_context.state.get(key)
    except Exception:
        return default
    return default if value is None else value


def record_model_request(callback_context: Any, llm_request: Any) -> None:
    """`before_model_callback` that notes which model is about to be called.

    The model name is only reliably available on the way out. `LlmResponse`
    carries `model_version` when the provider sets it, but LiteLLM often does
    not, so the request is the one place the identity is always known — and
    without it the whole cost side is unpriceable, which is precisely the state
    the trace was in.

    Keyed by agent rather than stored as a single value because a pipeline can
    fan out, and two agents mid-call at once would otherwise overwrite each
    other's model.
    """
    try:
        pending = dict(_read_state(callback_context, PENDING_MODEL_KEY, {}))
        pending[callback_context.agent_name] = getattr(llm_request, "model", None)
        callback_context.state[PENDING_MODEL_KEY] = pending
    except Exception as exc:  # bookkeeping must never break a run
        logger.debug("run_metrics: could not record model request: %s", exc)
    return None


def record_model_response(callback_context: Any, llm_response: Any) -> None:
    """`after_model_callback` that records one model call and what it cost.

    Appends rather than accumulates so nothing is lost to a lost update, and so
    the trace keeps a call count even when every token field is None: "this
    agent made four calls and the provider reported no usage" is a different and
    more actionable fact than "this agent used no tokens".
    """
    try:
        agent = callback_context.agent_name
        pending = dict(_read_state(callback_context, PENDING_MODEL_KEY, {}))
        model = getattr(llm_response, "model_version", None) or pending.get(agent)

        entry = {"agent": agent, "model": model,
                 "input_tokens": None, "output_tokens": None,
                 "cache_read_tokens": None, "reasoning_tokens": None}
        usage = getattr(llm_response, "usage_metadata", None)
        if usage is not None:
            entry.update(_usage_fields(usage))

        calls = list(_read_state(callback_context, MODEL_USAGE_KEY, []))
        calls.append(entry)
        callback_context.state[MODEL_USAGE_KEY] = calls
    except Exception as exc:
        logger.debug("run_metrics: could not record model response: %s", exc)
    return None


# Attach to any LlmAgent whose spend should be attributable, which is all of
# them. Kept as a pair of module-level lists so a stage reads
# `before_model_callback=[apply_user_context, *MODEL_CAPTURE_BEFORE]` rather
# than remembering two import names in the right order.
MODEL_CAPTURE_BEFORE = [record_model_request]
MODEL_CAPTURE_AFTER = [record_model_response]


def summarize_model_usage(state: Any) -> list[dict]:
    """Per agent and model, merged from the individual calls.

    Returns one row per (agent, model) — the grain the store's usage ledger
    wants, and the grain a price table can be joined to. `api_call_count` is
    always a real number; the token fields stay None unless at least one call
    reported them, and partial reporting sums only what was reported rather than
    treating a silent call as zero.
    """
    calls = state.get(MODEL_USAGE_KEY) if hasattr(state, "get") else None
    if not calls:
        return []

    merged: dict[tuple, dict] = {}
    for call in calls:
        key = (call.get("agent"), call.get("model"))
        slot = merged.setdefault(key, {
            "agent": call.get("agent"), "model": call.get("model"),
            "api_call_count": 0, "input_tokens": None, "output_tokens": None,
            "cache_read_tokens": None, "reasoning_tokens": None,
        })
        slot["api_call_count"] += 1
        for field in ("input_tokens", "output_tokens",
                      "cache_read_tokens", "reasoning_tokens"):
            value = call.get(field)
            if isinstance(value, int):
                slot[field] = (slot[field] or 0) + value
    return list(merged.values())
