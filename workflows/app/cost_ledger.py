"""What each model call actually cost, and the ceiling that stops a runaway.

Two jobs, kept together because they share the ledger:

  RECORD   every call, priced from LiteLLM's maintained cost map rather than a
           rate table we would have to keep current ourselves. `completion_cost`
           reads real usage off the response, so cached reads and reasoning
           tokens are priced as billed instead of estimated from a token count.

  REFUSE   dispatch once the day's spend passes a cap. This replaces the
           per-key budgets the proxy enforced. It is deliberately a pre-flight
           check rather than a mid-run abort: killing a workflow halfway leaves
           partial side effects — a draft created, a label applied — and the
           runaway case this exists for is a loop that dispatches repeatedly,
           which a pre-flight check catches on the next iteration.

Rows are JSONL under `${ADK_STATE_DIR}/usage/`, in the column shape the
dashboard's `fact_llm_usage` already defines. That view has had a
`_gateway_usage_sql()` branch reading exactly this path since before the
gateway existed, guarded so the store is correct with only some producers
present. So this needs no schema change and no downstream edit: the Metrics tab
picks it up the first time a file appears.

The one column worth dwelling on is `cost_status`. The store refuses to sum
costs that are not comparable, and distinguishes a model priced at zero from a
model with no price. A call priced here is `metered` with a real figure; if
pricing ever fails we write `unpriced` with NULL rather than a zero that would
masquerade as free.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ADK_STATE_DIR = os.environ.get("ADK_STATE_DIR", "/code/adk-state")
USAGE_DIR = Path(os.environ.get("USAGE_LOG_DIR", os.path.join(ADK_STATE_DIR, "usage")))

# Refuse to dispatch once the day's spend passes this. 0 disables the check.
#
# Covers WORKFLOWS ONLY. Hermes chat and cron call the vendor directly and are
# invisible here, so this is not a spend limit for the host — the account-level
# limit at the vendor is. Two different guarantees, and this is the weaker one.
DAILY_CAP_USD = float(os.environ.get("WORKFLOWS_DAILY_COST_CAP_USD", "0") or 0)

_lock = threading.Lock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _path_for(day: str) -> Path:
    return USAGE_DIR / f"usage-{day}.jsonl"


def price(response: Any, model: str) -> tuple[Optional[float], str, Optional[str]]:
    """(cost_usd, cost_status, cost_source) for one completion.

    Never raises. A pricing failure must not fail a workflow that has already
    done its work — it downgrades the row to `unpriced`, which the store models
    explicitly and refuses to add to metered spend.
    """
    try:
        import litellm

        cost = litellm.completion_cost(completion_response=response, model=model)
        if cost is None:
            return None, "unpriced", None
        return float(cost), "metered", "litellm.completion_cost"
    except Exception as exc:  # noqa: BLE001
        logger.warning("cost_ledger: could not price %s (%s)", model, type(exc).__name__)
        return None, "unpriced", None


def _usage_field(response: Any, *names: str) -> Optional[int]:
    """Read a token count off whatever shape the response is.

    LiteLLM returns a pydantic object; a cached or replayed response may be a
    plain dict. Both appear here, so neither is assumed.
    """
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None
    for name in names:
        value = getattr(usage, name, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(name)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def record(
    *,
    response: Any,
    model: str,
    activity_id: str = "",
    component: str = "adk",
    task: str = "",
    precomputed_cost: Optional[float] = None,
) -> Dict[str, Any]:
    """Append one priced call. Returns the row as written.

    `precomputed_cost` is LiteLLM's own `response_cost`, which it has already
    worked out by the time a success callback fires. Reusing it avoids pricing
    the same completion twice, and it is the same function underneath — so the
    fallback below is for callers outside the callback path, not a second
    opinion.
    """
    if precomputed_cost is not None:
        cost, status, source = float(precomputed_cost), "metered", "litellm.response_cost"
    else:
        cost, status, source = price(response, model)
    now = datetime.now(timezone.utc).isoformat()

    row: Dict[str, Any] = {
        "activity_id": activity_id or None,
        "component": component,
        "model": model,
        # The vendor, read off LiteLLM's `provider/model` addressing — the same
        # thing the store derives for every other producer.
        "billing_provider": model.split("/", 1)[0] if "/" in model else None,
        "billing_base_url": None,
        "billing_mode": "api",
        "task": task or None,
        "api_call_count": 1,
        "input_tokens": _usage_field(response, "prompt_tokens", "input_tokens"),
        "output_tokens": _usage_field(response, "completion_tokens", "output_tokens"),
        "cache_read_tokens": _usage_field(response, "cache_read_input_tokens"),
        "cache_write_tokens": _usage_field(response, "cache_creation_input_tokens"),
        "reasoning_tokens": _usage_field(response, "reasoning_tokens"),
        # Estimated stays NULL on purpose. This figure is priced from real usage
        # off the response, so writing it into both columns would erase the
        # distinction the store exists to preserve.
        "estimated_cost_usd": None,
        "actual_cost_usd": cost,
        "cost_status": status,
        "cost_source": source,
        "pricing_version": None,
        "observed_by": "workflows",
        "profile": None,
        "first_seen": now,
        "last_seen": now,
    }

    try:
        USAGE_DIR.mkdir(parents=True, exist_ok=True)
        # Append under a lock: ADK serves requests on several threads, and two
        # interleaved partial writes would produce a line neither valid JSON nor
        # recoverable. One line per call, so a torn file loses one row.
        with _lock:
            with _path_for(_today()).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")
    except OSError as exc:
        # Never fail a run over bookkeeping.
        logger.warning("cost_ledger: could not append usage row (%s)", exc)

    return row


def spend_today() -> float:
    """Metered spend so far today, in USD.

    Unpriced rows contribute nothing rather than zero — the same refusal the
    store makes. A day of calls nobody could price reads as $0 spent and that
    is honest: we do not know what it cost, so we cannot cap on it.
    """
    path = _path_for(_today())
    if not path.is_file():
        return 0.0
    total = 0.0
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    # A torn final line from a crash mid-append. Skip it rather
                    # than refusing to report any spend at all.
                    continue
                value = row.get("actual_cost_usd")
                if isinstance(value, (int, float)):
                    total += float(value)
    except OSError as exc:
        logger.warning("cost_ledger: could not read today's usage (%s)", exc)
    return total


class DailyCapExceeded(RuntimeError):
    """Raised before dispatch when the day's cap is already spent."""

    def __init__(self, spent: float, cap: float) -> None:
        super().__init__(
            f"daily model spend cap reached: ${spent:.4f} of ${cap:.2f} used today. "
            f"Raise WORKFLOWS_DAILY_COST_CAP_USD or wait for the UTC day to roll over."
        )
        self.spent = spent
        self.cap = cap


def check_cap(cap_usd: Optional[float] = None) -> Dict[str, Any]:
    """Pre-flight check. Raises DailyCapExceeded when over.

    Returns the current position when under, so a caller can log or surface it.
    """
    cap = DAILY_CAP_USD if cap_usd is None else cap_usd
    spent = spend_today()
    if cap > 0 and spent >= cap:
        raise DailyCapExceeded(spent, cap)
    return {
        "spent_today_usd": round(spent, 6),
        "cap_usd": cap,
        "enabled": cap > 0,
        "remaining_usd": round(cap - spent, 6) if cap > 0 else None,
    }
