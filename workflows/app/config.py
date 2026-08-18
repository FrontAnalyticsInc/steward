"""Model configuration — the one place a model is chosen.

Every agent calls `build_model()`; nothing else names a model. Agents ask for a
ROLE — `drafting`, `extraction`, `fast` — and `app/model_aliases.py` decides
which vendor model that is, from a file the operator console can edit. Swapping
a model is one line in one file and no code change.

Calls go straight to the vendor through the LiteLLM SDK, in this process. There
is no proxy: the proxy's documented floor is 4 CPU / 8 GB, its query engine
holds memory as a high-water mark that never returns to the OS, and it needs a
Postgres beside it — none of which fits a small box. What we actually wanted
from it was the maintained price map, and `litellm.completion_cost` is that,
without the container.

Every call is priced and recorded by `app/cost_ledger.py` through the callback
registered here, so cost lands in the same `fact_llm_usage` shape the dashboard
already reads.

`ollama` is kept for local development against a host GPU; `gemini` is kept
warm. Neither is used in a cloud deployment.
"""

from __future__ import annotations

import logging
import os

from google.adk.models import Gemini
from google.adk.models.lite_llm import LiteLlm
from google.genai import types

from app import model_aliases

logger = logging.getLogger(__name__)

# "anthropic" (deployed: SDK straight to the vendor) | "ollama" | "gemini"
MODEL_PROVIDER = os.environ.get("WORKFLOWS_MODEL_PROVIDER", "anthropic").strip().lower()

# The role an agent gets when it does not ask for one. Open-ended judgement is
# the common case for the agents here, so the default is the capable tier
# rather than the cheap one — an agent that wants `fast` says so.
DEFAULT_ALIAS = os.environ.get("WORKFLOWS_DEFAULT_ALIAS", "drafting")

# Pinned. The earlier prototype on this host referenced a model tag that had
# since been deleted (`gemma4:e4b`) and failed silently, so keep this aligned
# with `ollama list`.
OLLAMA_MODEL = os.environ.get("WORKFLOWS_OLLAMA_MODEL", "gemma4:12b-it-qat-64k")
OLLAMA_API_BASE = os.environ.get("OLLAMA_API_BASE", "http://127.0.0.1:11434")

GEMINI_MODEL = os.environ.get("WORKFLOWS_GEMINI_MODEL", "gemini-3.6-flash")

# LiteLLM's ollama provider reads this from the environment, not an argument.
os.environ.setdefault("OLLAMA_API_BASE", OLLAMA_API_BASE)

if MODEL_PROVIDER == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
    # Warned once at import rather than raised: every agent module builds its
    # model at import time and twelve test modules import those agents, so
    # raising would turn one missing env var into a dozen collection errors.
    logger.warning(
        "ANTHROPIC_API_KEY is unset; model calls will fail with an auth error. "
        "Set it in docker/.env."
    )


def _register_cost_callback() -> None:
    """Price and record every completion, once per process.

    A LiteLLM success callback rather than a wrapper around ADK's LiteLlm: ADK
    owns the call site, and hooking the SDK catches every call regardless of who
    made it — an agent, an eval run, or a script. LiteLLM has already computed
    `response_cost` by the time this fires, so the common path costs nothing
    beyond a file append.
    """
    try:
        import litellm

        from app import cost_ledger
    except ImportError:  # pragma: no cover - litellm is a hard dependency
        return

    def _on_success(kwargs, completion_response, start_time, end_time):  # noqa: ANN001
        try:
            model = kwargs.get("model") or ""
            provider = kwargs.get("custom_llm_provider")
            # LiteLLM strips the provider prefix off `model` by the time the
            # callback fires, so it is put back here — the metrics store reads
            # the vendor out of `provider/model` for cost attribution.
            qualified = f"{provider}/{model}" if provider and "/" not in model else model
            cost_ledger.record(
                response=completion_response,
                model=qualified,
                precomputed_cost=kwargs.get("response_cost"),
                task=(kwargs.get("litellm_params") or {}).get("metadata", {}).get("task", ""),
            )
        except Exception:  # noqa: BLE001
            # A callback that raises would surface as a failed model call.
            # Bookkeeping must never do that.
            logger.warning("cost_ledger: callback failed", exc_info=True)

    callbacks = list(getattr(litellm, "success_callback", None) or [])
    if not any(getattr(c, "__name__", "") == "_on_success" for c in callbacks):
        callbacks.append(_on_success)
        litellm.success_callback = callbacks


_register_cost_callback()


def build_model(alias: str | None = None):
    """Return the model instance for an agent, by ROLE not by model name.

    `alias` is a key in the alias file — `drafting`, `extraction`, `fast`.
    Existing callers pass nothing and get DEFAULT_ALIAS, so adopting a cheaper
    tier for a specific agent is a one-word change at that agent.
    """
    if MODEL_PROVIDER == "anthropic":
        # Raises on an unknown alias rather than falling back: a typo answered
        # silently by the drafting model is a cost bug that shows up weeks later
        # as a surprising invoice.
        return LiteLlm(model=model_aliases.resolve(alias or DEFAULT_ALIAS))
    if MODEL_PROVIDER == "gemini":
        return Gemini(
            model=GEMINI_MODEL,
            retry_options=types.HttpRetryOptions(attempts=3),
        )
    if MODEL_PROVIDER == "ollama":
        # ollama_chat/ (not ollama/) is the chat-completions path, which is the
        # one that supports structured output and tool schemas.
        return LiteLlm(model=f"ollama_chat/{OLLAMA_MODEL}")
    raise ValueError(
        f"unknown WORKFLOWS_MODEL_PROVIDER {MODEL_PROVIDER!r}; "
        "expected 'anthropic', 'ollama' or 'gemini'"
    )


def model_string(alias: str | None = None) -> str:
    """The LiteLLM model id for a role, for code that calls the SDK directly.

    `build_model` returns an ADK model object, which only an LlmAgent can use.
    A plain library function doing one extraction — `app/fact_distill.py` — has
    no agent, no session and no runner, and building all three to ask one
    question would be ceremony around a single HTTP call.

    Going through the SDK directly is not a bypass: the success callback
    registered above hooks LiteLLM itself, so a call made here is priced and
    recorded exactly like one made by an agent.
    """
    if MODEL_PROVIDER == "anthropic":
        return model_aliases.resolve(alias or DEFAULT_ALIAS)
    if MODEL_PROVIDER == "gemini":
        return f"gemini/{GEMINI_MODEL}"
    if MODEL_PROVIDER == "ollama":
        return f"ollama_chat/{OLLAMA_MODEL}"
    raise ValueError(
        f"unknown WORKFLOWS_MODEL_PROVIDER {MODEL_PROVIDER!r}; "
        "expected 'anthropic', 'ollama' or 'gemini'"
    )


def model_available() -> bool:
    """Whether a model call has any chance of succeeding.

    Checked before distillation so a missing key degrades to the deterministic
    path with one log line, rather than one auth exception per contact per run.
    """
    if MODEL_PROVIDER == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if MODEL_PROVIDER == "gemini":
        return bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    return MODEL_PROVIDER == "ollama"


def describe_model(alias: str | None = None) -> str:
    """Human-readable model identity, for logs and the README."""
    if MODEL_PROVIDER == "anthropic":
        name = alias or DEFAULT_ALIAS
        try:
            return f"{name} -> {model_aliases.resolve(name)}"
        except KeyError:
            return f"{name} -> (unresolved)"
    if MODEL_PROVIDER == "gemini":
        return f"gemini:{GEMINI_MODEL}"
    return f"{MODEL_PROVIDER}:{OLLAMA_MODEL}"
