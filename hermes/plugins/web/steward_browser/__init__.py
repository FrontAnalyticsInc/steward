"""Steward renderer as a web *extract* backend.

Every Steward install ships `docker/browser` — a Playwright/Chromium service
that renders a URL and returns the finished HTML. Nothing called it. Every
extract-capable provider in the bundled registry (firecrawl, tavily, exa,
parallel) needs a paid key, so `web.extract_backend` shipped empty and the
agent could list search results without being able to read any of them.

This plugin is the gap between those two facts. It speaks the renderer's
`POST /content` contract directly — that service is NOT a CDP endpoint, so
Hermes' built-in `browser_*` tools cannot talk to it and `browser.cdp_url`
is the wrong wire.

Extract-only. Pair it with a search backend (`web.search_backend: ddgs`).

Installed as a USER plugin under `$HERMES_HOME/plugins/web/steward_browser`
by `hermes/seed.sh`, which means it is opt-in and must be named in
`plugins.enabled` — `hermes/config.yaml.template` names it.
"""

from __future__ import annotations

from .provider import StewardBrowserWebProvider


def register(ctx) -> None:
    """Register the Steward renderer provider with the plugin context."""
    ctx.register_web_search_provider(StewardBrowserWebProvider())
