"""One authenticated session against Hermes's own dashboard API.

Two features here write host state that Hermes already owns — messaging
channels (:mod:`channels`) and MCP connections (:mod:`mcp_servers`). Both reach
it the same way, so the cookie session lives here rather than once per feature:
two clients would mean two logins, two independent expiries, and a rate-limit
on ``/auth/password-login`` that neither of them could see coming.

The dashboard authenticates with a cookie minted by ``/auth/password-login``,
not a bearer token, so the session is held across calls and re-minted on the
first 401 rather than on a timer — nothing here knows the cookie's lifetime,
and guessing it would mean either re-logging in constantly or failing once per
expiry.
"""

import os
import time
from typing import Optional

import httpx

# Hermes's own dashboard. Every container here runs network_mode: host, so a
# loopback address reaches it from this one.
HERMES_DASHBOARD_URL = os.getenv(
    "HERMES_DASHBOARD_URL", "http://127.0.0.1:9119"
).rstrip("/")
# Same variable names the hermes-dashboard service is configured with, so a
# deployment sets the credential once and both containers agree.
DASHBOARD_USER = os.getenv("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "admin")
DASHBOARD_PASSWORD = os.getenv("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "")


class HermesUnavailable(RuntimeError):
    """Hermes's dashboard could not be reached or would not authenticate.

    Carries the operator-facing reason. This is deliberately distinct from "no
    channels are configured" / "no connections exist": one is a broken link
    between two containers, the other is a real answer, and rendering the first
    as the second is how a settings page comes to claim everything is off.
    """


class _Client:
    """A logged-in session against the Hermes dashboard."""

    def __init__(self) -> None:
        self._cookies: Optional[httpx.Cookies] = None
        self._authed_at: float = 0.0

    async def _login(self, client: httpx.AsyncClient) -> None:
        if not DASHBOARD_PASSWORD:
            raise HermesUnavailable(
                "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD is not set for this "
                "container, so it cannot sign in to the Hermes dashboard that "
                "owns this configuration."
            )
        try:
            resp = await client.post(
                f"{HERMES_DASHBOARD_URL}/auth/password-login",
                json={
                    "provider": "basic",
                    "username": DASHBOARD_USER,
                    "password": DASHBOARD_PASSWORD,
                },
            )
        except httpx.RequestError as exc:
            raise HermesUnavailable(
                f"Could not reach the Hermes dashboard at {HERMES_DASHBOARD_URL}: {exc}"
            ) from exc
        if resp.status_code == 401:
            raise HermesUnavailable(
                "The Hermes dashboard rejected these credentials "
                "(HERMES_DASHBOARD_BASIC_AUTH_USERNAME / _PASSWORD)."
            )
        if resp.status_code == 429:
            raise HermesUnavailable(
                "The Hermes dashboard is rate-limiting sign-in attempts; try again shortly."
            )
        if resp.status_code >= 400:
            raise HermesUnavailable(
                f"Sign-in to the Hermes dashboard failed ({resp.status_code})."
            )
        self._cookies = client.cookies
        self._authed_at = time.time()

    async def request(self, method: str, path: str, **kw) -> httpx.Response:
        # Probing an MCP server spawns it and lists its tools, which for a cold
        # `npx` stdio server is tens of seconds. The default here is the ceiling
        # for every call, so it is generous enough for that; callers with a
        # tighter expectation pass their own.
        kw.setdefault("timeout", 60.0)
        timeout = kw.pop("timeout")
        async with httpx.AsyncClient(timeout=timeout, cookies=self._cookies) as client:
            if self._cookies is None:
                await self._login(client)
            try:
                resp = await client.request(
                    method, f"{HERMES_DASHBOARD_URL}{path}", **kw
                )
            except httpx.RequestError as exc:
                raise HermesUnavailable(
                    f"Could not reach the Hermes dashboard at {HERMES_DASHBOARD_URL}: {exc}"
                ) from exc
            if resp.status_code in (401, 403):
                # Cookie expired underneath us. One retry, then give up — a
                # second 401 means the credential is wrong, not stale.
                self._cookies = None
                await self._login(client)
                try:
                    resp = await client.request(
                        method, f"{HERMES_DASHBOARD_URL}{path}", **kw
                    )
                except httpx.RequestError as exc:
                    raise HermesUnavailable(
                        f"Could not reach the Hermes dashboard at {HERMES_DASHBOARD_URL}: {exc}"
                    ) from exc
            self._cookies = client.cookies
            return resp


client = _Client()


def detail_of(resp, fallback: str) -> str:
    """Hermes's own error text for a failed response, or `fallback`.

    Its handlers put the operator-facing reason in ``detail`` — "Server 'x'
    already exists", "suspicious command/args configuration". Forwarding that
    verbatim is the difference between a settings page that says what is wrong
    and one that says 400.
    """
    try:
        payload = resp.json()
    except ValueError:
        text = (resp.text or "").strip()
        return text[:400] or fallback
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if isinstance(detail, list) and detail:
            # FastAPI validation errors arrive as a list of dicts.
            first = detail[0]
            if isinstance(first, dict) and first.get("msg"):
                return str(first["msg"])
    return fallback
