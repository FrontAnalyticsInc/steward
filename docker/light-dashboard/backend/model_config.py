"""The default agent's main model — read and write, against Hermes's own API.

Why this is a proxy and not a config writer
--------------------------------------------
Hermes already has a purpose-built endpoint for this exact job:

    GET  /api/model/info    the resolved provider/model/base_url for the main slot
    POST /api/model/set     assign a provider/model to the main slot

``/api/model/set`` is more than a config write — for a "custom" (local/self-hosted)
endpoint it also registers a ``custom_providers`` entry (dedup'd by base_url) the
same way the ``hermes model`` picker's custom flow does, and it runs an
expensive-model cost check before committing. Reimplementing any of that against
raw ``config.yaml`` would mean owning a second copy of logic Hermes already gets
right — same reasoning as :mod:`channels` and :mod:`mcp_servers`, and the same
session, see :mod:`hermes_api`.

Why only three providers
-------------------------
Hermes supports dozens of providers, each with its own auth flow, base URLs and
model catalog — real judgment that belongs in Hermes's own dashboard (linked from
the frontend). This dashboard exposes only the three defaults most installs
actually pick from: Claude, OpenAI, and a self-hosted/local (Ollama-compatible)
endpoint. Anything else, the operator sets from Hermes's dashboard directly, and
this page just reflects whatever comes back.
"""

import os
from typing import Any, Dict, Optional

from .hermes_api import HermesUnavailable
from .hermes_api import client as _client

ModelConfigUnavailable = HermesUnavailable

# id -> (label, Hermes provider slug, default model, needs an API key, key env var)
PROVIDERS = [
    {
        "id": "claude",
        "label": "Claude (Anthropic)",
        "hermes_provider": "anthropic",
        "default_model": "claude-opus-5",
        "key_env": "ANTHROPIC_API_KEY",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "hermes_provider": "openai-api",
        "default_model": "gpt-5.5",
        "key_env": "OPENAI_API_KEY",
    },
    {
        "id": "local",
        "label": "Local (self-hosted / Ollama-compatible)",
        "hermes_provider": "custom",
        "default_model": "gemma4:12b-it-qat-64k",
        "key_env": None,
    },
]
_BY_ID = {p["id"]: p for p in PROVIDERS}
_BY_HERMES_PROVIDER = {p["hermes_provider"]: p for p in PROVIDERS if p["id"] != "local"}

# The address a container on this compose network reaches a host-run Ollama at.
# Only a placeholder/hint for the base_url field — never written unless the
# operator's own value is empty, since the right address genuinely depends on
# how the box is set up (bridge network vs. host networking).
DEFAULT_LOCAL_BASE_URL = os.getenv("OLLAMA_API_BASE", "http://host.docker.internal:11434/v1")


def _public_providers() -> list:
    return [
        {
            "id": p["id"],
            "label": p["label"],
            "default_model": p["default_model"],
            "needs_key": bool(p["key_env"]),
            "key_env": p["key_env"],
        }
        for p in PROVIDERS
    ]


async def get_model_config() -> dict:
    """The main slot's current provider/model, normalized to our 3-provider id.

    ``provider`` is None when Hermes reports something outside the three this
    dashboard offers a picker for (e.g. openrouter, bedrock) — the frontend
    shows what is actually configured and points at Hermes's own dashboard to
    change it, rather than forcing a choice that doesn't fit.
    """
    resp = await _client.request("GET", "/api/model/info")
    if resp.status_code >= 400:
        raise ModelConfigUnavailable(
            f"The Hermes dashboard returned {resp.status_code} for /api/model/info."
        )
    info = resp.json()
    hermes_provider = (info.get("provider") or "").strip()
    # A local/self-hosted endpoint round-trips as "custom" or "custom:<name>"
    # (the named custom_providers entry) — either way it's our "local" id.
    if hermes_provider == "custom" or hermes_provider.startswith("custom:"):
        matched = _BY_ID["local"]
    else:
        matched = _BY_HERMES_PROVIDER.get(hermes_provider)
    return {
        "provider": matched["id"] if matched else None,
        "hermes_provider": hermes_provider,
        "model": info.get("model") or "",
        "base_url": info.get("base_url") or "",
        "providers": _public_providers(),
    }


async def update_model_config(
    provider_id: str,
    model: str,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    confirm_expensive_model: bool = False,
) -> dict:
    meta = _BY_ID.get(provider_id)
    if meta is None:
        raise ModelConfigUnavailable(f"{provider_id} is not a provider this dashboard exposes.")
    model = (model or "").strip() or meta["default_model"]
    body: Dict[str, Any] = {
        "scope": "main",
        "provider": meta["hermes_provider"],
        "model": model,
        "confirm_expensive_model": bool(confirm_expensive_model),
    }
    if provider_id == "local":
        body["base_url"] = (base_url or "").strip() or DEFAULT_LOCAL_BASE_URL
        if api_key:
            body["api_key"] = api_key.strip()
    resp = await _client.request("POST", "/api/model/set", json=body)
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("detail") or ""
        except ValueError:
            detail = resp.text[:400]
        raise ModelConfigUnavailable(detail or f"Hermes rejected the change ({resp.status_code}).")
    return resp.json()
