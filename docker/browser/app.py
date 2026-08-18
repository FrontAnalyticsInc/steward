"""Rendering service: one URL in, fully-rendered HTML out.

Speaks the same wire contract browserless did — POST /content?token=… with
{"url": …, "gotoOptions": {"waitUntil": …, "timeout": …}} — so the workflows
side did not have to change when we moved off it. Keep that contract stable;
it is the only thing kestrel_site_ingest knows about this service.

Why this exists rather than a stock renderer image: we need Chrome extensions,
and every off-the-shelf option fails on that. browserless gates extensions
behind its paid tier, chrome-headless-shell is the old headless binary and
cannot load them at all, and Playwright's own `run-server` cannot either —
extensions require launch_persistent_context(), which is a local launch, and
connect() does not do persistent contexts. So the container that owns the
browser has to own the launch, and that is this file.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

TOKEN = os.getenv("TOKEN", "")
PROFILE_DIR = os.getenv("PROFILE_DIR", "/profile")
EXTENSIONS_DIR = os.getenv("EXTENSIONS_DIR", "/extensions")
CONCURRENT = int(os.getenv("CONCURRENT", "2"))
DEFAULT_TIMEOUT_MS = int(os.getenv("TIMEOUT", "60000"))

# Off by default, and it must stay off on the shared crawler instance.
# /session puts real session cookies into the persistent profile, and this
# service renders arbitrary URLs in one shared context: a logged-in cookie here
# is reachable, cookie-bearing, by every page any workflow later crawls. Enable
# it only on an instance dedicated to a single authenticated site, with its own
# profile directory and its own token.
ALLOW_SESSION_INJECTION = os.getenv("ALLOW_SESSION_INJECTION", "") == "1"

# Puppeteer's vocabulary, which the caller still speaks. Playwright dropped the
# distinction between "no connections for 500ms" and "at most 2 connections";
# both map onto its single networkidle.
WAIT_UNTIL = {
    "load": "load",
    "domcontentloaded": "domcontentloaded",
    "networkidle": "networkidle",
    "networkidle0": "networkidle",
    "networkidle2": "networkidle",
}

app = FastAPI()
_state: dict[str, Any] = {"context": None, "playwright": None, "extensions": []}
_slots = asyncio.Semaphore(CONCURRENT)


def _extension_paths() -> list[str]:
    """Every immediate subdirectory of EXTENSIONS_DIR holding a manifest.

    Unpacked only. Chrome cannot side-load a .crx from the command line, and
    Google removed the flags that used to allow it from Chrome and Edge — which
    is also why this runs Playwright's bundled Chromium rather than stock
    Chrome.
    """
    root = pathlib.Path(EXTENSIONS_DIR)
    if not root.is_dir():
        return []
    return sorted(
        str(child.resolve())
        for child in root.iterdir()
        if child.is_dir() and (child / "manifest.json").is_file()
    )


@app.on_event("startup")
async def _start() -> None:
    extensions = _extension_paths()
    args = [
        "--disable-dev-shm-usage",  # /dev/shm in a container is 64MB; Chromium wants more
        "--no-sandbox",
    ]
    if extensions:
        joined = ",".join(extensions)
        # Both flags are required: --load-extension alone is ignored unless the
        # allow-list flag names the same paths.
        args += [f"--disable-extensions-except={joined}", f"--load-extension={joined}"]

    pathlib.Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)

    pw = await async_playwright().start()
    # channel="chromium" is new headless. The old headless mode cannot run
    # extensions; this is the whole reason for the channel pin.
    context = await pw.chromium.launch_persistent_context(
        PROFILE_DIR,
        channel="chromium",
        headless=True,
        args=args,
        ignore_https_errors=False,
    )
    _state.update(playwright=pw, context=context, extensions=extensions)


@app.on_event("shutdown")
async def _stop() -> None:
    if _state.get("context"):
        with contextlib.suppress(Exception):
            await _state["context"].close()
    if _state.get("playwright"):
        with contextlib.suppress(Exception):
            await _state["playwright"].stop()


def _authorize(request: Request) -> None:
    # An unset TOKEN refuses everything rather than allowing everything.
    #
    # It used to return early here, which meant a deployment that forgot the
    # variable got a service with no authentication at all — and this service
    # fetches any URL it is handed and executes that page's JavaScript from
    # inside the stack's network. Unauthenticated, that is an open proxy to
    # every other service, and nothing about it looks broken: it serves 200s.
    #
    # 503 rather than 401 on purpose. The operator has not supplied a bad
    # credential, they have not configured the service, and those want
    # different fixes. It also makes the compose healthcheck fail, so an
    # unconfigured renderer is visibly down instead of quietly permissive.
    if not TOKEN:
        raise HTTPException(
            status_code=503,
            detail="TOKEN is not configured; refusing to serve. "
                   "Set BROWSER_TOKEN (or BROWSER_LINKEDIN_TOKEN) in docker/.env.",
        )
    supplied = request.query_params.get("token") or request.headers.get("x-token", "")
    if supplied != TOKEN:
        raise HTTPException(status_code=401, detail="bad or missing token")


@app.get("/json/version")
async def version(request: Request) -> dict[str, Any]:
    """Healthcheck. Named to match what browserless served, so the compose
    healthcheck and anything else pointing here did not need rewriting."""
    _authorize(request)
    context = _state.get("context")
    if context is None:
        raise HTTPException(status_code=503, detail="browser not started")
    return {
        "Browser": "playwright-chromium",
        "extensions": [pathlib.Path(p).name for p in _state["extensions"]],
    }


@app.post("/content")
async def content(request: Request) -> Response:
    _authorize(request)
    context = _state.get("context")
    if context is None:
        raise HTTPException(status_code=503, detail="browser not started")

    body = await request.json()
    url = (body or {}).get("url")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")

    goto = (body or {}).get("gotoOptions") or {}
    wait_until = WAIT_UNTIL.get(str(goto.get("waitUntil", "load")).lower(), "load")
    timeout = float(goto.get("timeout") or DEFAULT_TIMEOUT_MS)

    async with _slots:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout)
            html = await page.content()
            # The post-redirect URL. A caller reading an authenticated site
            # cannot otherwise tell a real page from a login wall: both are
            # HTTP 200 with plausible HTML, and silently scraping the login
            # page is the failure mode this prevents.
            final_url = page.url
        except PlaywrightError as exc:
            # A render failure is the caller's problem to report, not a crash
            # here. 502 keeps it distinguishable from a bad request.
            raise HTTPException(status_code=502, detail=str(exc).split("\n")[0]) from exc
        finally:
            with contextlib.suppress(Exception):
                await page.close()

    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"X-Final-Url": final_url},
    )


@app.get("/session")
async def session_status(request: Request) -> dict[str, Any]:
    """Which cookie names/domains this profile holds. Never their values."""
    _authorize(request)
    context = _state.get("context")
    if context is None:
        raise HTTPException(status_code=503, detail="browser not started")
    cookies = await context.cookies()
    return {
        "session_injection_enabled": ALLOW_SESSION_INJECTION,
        "cookies": sorted({f"{c['domain']}:{c['name']}" for c in cookies}),
    }


@app.post("/session")
async def session_add(request: Request) -> dict[str, Any]:
    """Add cookies to the persistent profile, so an authenticated site can be
    read without this service ever seeing a password.

    Gated behind ALLOW_SESSION_INJECTION for the reason given at that constant.
    """
    _authorize(request)
    if not ALLOW_SESSION_INJECTION:
        raise HTTPException(
            status_code=403,
            detail="session injection is disabled; set ALLOW_SESSION_INJECTION=1 "
            "on a dedicated instance with its own profile",
        )
    context = _state.get("context")
    if context is None:
        raise HTTPException(status_code=503, detail="browser not started")

    cookies = (await request.json() or {}).get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise HTTPException(status_code=400, detail="cookies must be a non-empty list")
    try:
        await context.add_cookies(cookies)
    except PlaywrightError as exc:
        raise HTTPException(status_code=400, detail=str(exc).split("\n")[0]) from exc
    return {"added": len(cookies)}
