"""Messaging channels — read and write, against Hermes's own API.

Why this is a proxy and not a reimplementation
----------------------------------------------
Enabling a channel means writing env vars into $HERMES_HOME/.env and keys into
config.yaml, in the shapes the gateway's ``_apply_env_overrides`` expects. All
of that is already implemented, typed and validated behind the Hermes web
dashboard (``hermes dashboard``, the hermes-dashboard container):

    GET /api/messaging/platforms          the catalog + current state
    PUT /api/messaging/platforms/<id>     {enabled, env, clear_env}

Writing those files ourselves would mean owning a second copy of Hermes's env
schema, its per-platform validation and its multiplex port-binding conflict
checks — and being wrong about them silently, on a file the gateway reads at
boot. So this module holds a client, not a writer.

The interactive ``hermes gateway setup`` wizard is not involved. It is a TTY
front-end onto the same env vars, so driving it through a pty would be a
brittle way to reach a contract that is already HTTP.

What this module adds on top of the proxy:
  * the subset of platforms this dashboard exposes, in a fixed order
  * live chats from channel_directory.json, which the catalog does not carry
  * a restart that runs in the right container (see restart_gateway)

The authenticated session itself now lives in :mod:`hermes_api`, shared with
the MCP connections proxy so the two features hold one login rather than two.
"""

import json
import os
import signal
from typing import Any, Dict, List, Optional

from .hermes_api import HermesUnavailable
from .hermes_api import client as _client

# The channels this dashboard exposes, in display order. Hermes's catalog
# carries 33 platforms — most of them (weixin, yuanbao, ntfy, the webhook
# adapters) are not things a person holds a conversation in, and listing all
# of them would make the page a protocol inventory rather than a way to reach
# the agent. The ids are Hermes's own, so nothing here has to translate.
EXPOSED = ["slack", "teams", "telegram", "discord", "signal", "whatsapp"]

# A monogram tint per channel, kept beside the id rather than in the frontend
# so adding a channel is one edit. These are CSS variable names, not hexes, so
# they follow the light/dark theme.
TINTS = {
    "slack": "var(--acc-green)",
    "teams": "var(--acc-lavender)",
    "telegram": "var(--acc-blue)",
    "discord": "var(--acc-mauve)",
    "signal": "var(--acc-sky)",
    "whatsapp": "var(--acc-green)",
}

CHANNEL_DIRECTORY = "channel_directory.json"


# main.py catches this by its old name, and so does every call site here. It
# is the shared error, aliased rather than re-raised: a channel save failing
# because the dashboard is unreachable is the same fact whichever feature asked.
ChannelsUnavailable = HermesUnavailable


def _live_chats(data_dir: str) -> Dict[str, List[dict]]:
    """Where the agent is actually being talked to, per platform.

    The gateway maintains this as it sees traffic, so it answers a question
    the catalog cannot: a platform can be connected and configured and still
    have nobody in it.
    """
    path = os.path.join(data_dir, CHANNEL_DIRECTORY)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return {}
    platforms = payload.get("platforms")
    return platforms if isinstance(platforms, dict) else {}


async def list_channels(data_dir: str) -> dict:
    """The exposed channels, with their real state and their live chats."""
    resp = await _client.request("GET", "/api/messaging/platforms")
    if resp.status_code >= 400:
        raise ChannelsUnavailable(
            f"The Hermes dashboard returned {resp.status_code} for the platform catalog."
        )
    payload = resp.json()
    by_id = {p.get("id"): p for p in payload.get("platforms", [])}
    chats = _live_chats(data_dir)

    out = []
    for cid in EXPOSED:
        entry = by_id.get(cid)
        if not entry:
            # Hermes does not know this platform on this build. Say so rather
            # than dropping the row: a channel that silently disappears reads
            # as a UI bug, and this is a real (if rare) answer.
            out.append(
                {
                    "id": cid,
                    "name": cid.title(),
                    "tint": TINTS.get(cid, "var(--acc-lavender)"),
                    "unknown": True,
                    "state": "unknown",
                    "env_vars": [],
                    "chats": [],
                }
            )
            continue
        out.append(
            {
                "id": cid,
                "name": entry.get("name") or cid.title(),
                "description": entry.get("description") or "",
                "docs_url": entry.get("docs_url"),
                "tint": TINTS.get(cid, "var(--acc-lavender)"),
                "enabled": bool(entry.get("enabled")),
                "configured": bool(entry.get("configured")),
                "state": entry.get("state") or "disabled",
                "error_message": entry.get("error_message"),
                "updated_at": entry.get("updated_at"),
                "home_channel": entry.get("home_channel"),
                # Passed through as Hermes describes them — prompt, help, docs
                # link, whether it is a secret, whether it is advanced. This
                # dashboard deliberately holds no copy of that schema.
                "env_vars": entry.get("env_vars") or [],
                "chats": chats.get(cid) or [],
                "unknown": False,
            }
        )

    return {
        "channels": out,
        "env_path": payload.get("env_path"),
        # Hermes's catalog reports gateway_running from its own container,
        # where the gateway process is not visible. Left out rather than
        # forwarded — a false "not running" on a settings page is worse than
        # no claim at all. The Metrics tab already answers liveness.
    }


async def update_channel(
    channel_id: str, enabled: Optional[bool], env: Dict[str, str], clear_env: List[str]
) -> dict:
    """Save one channel. Returns Hermes's own response body."""
    if channel_id not in EXPOSED:
        raise ChannelsUnavailable(f"{channel_id} is not a channel this dashboard exposes.")
    body: Dict[str, Any] = {"env": env or {}, "clear_env": clear_env or []}
    if enabled is not None:
        body["enabled"] = enabled
    resp = await _client.request(
        "PUT", f"/api/messaging/platforms/{channel_id}", json=body
    )
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("detail") or ""
        except ValueError:
            detail = resp.text[:400]
        raise ChannelsUnavailable(
            detail or f"Hermes rejected the change ({resp.status_code})."
        )
    try:
        return resp.json()
    except ValueError:
        return {"ok": True}


# Matched as one contiguous string against the supervised process's cmdline:
# "/opt/hermes/.venv/bin/python3 /opt/hermes/.venv/bin/hermes gateway run
# --replace". Testing for "hermes gateway run" and "/bin/hermes" separately
# looked equivalent and was not — any command line mentioning both matched,
# including a diagnostic looking for this very process, which is how it was
# caught. The s6 wrapper ("…/main-wrapper.sh gateway run") does not contain
# this, and it is the other process that must never match.
GATEWAY_CMDLINE = "/bin/hermes gateway run"


def _find_gateway_pid() -> Optional[int]:
    """PID of the supervised gateway, or None if it is not visible here.

    Visible means the compose file shares the gateway's PID namespace with
    this container (``pid: service:hermes-gateway``). Without that this
    container's /proc holds only its own processes and the scan finds
    nothing — which is a configuration answer, not a "gateway is down" one,
    and the caller says so.
    """
    try:
        # Numerically, not in readdir order: if two processes ever match, the
        # older one is the supervised gateway and the newer is something that
        # just started. Picking whichever the directory happened to list first
        # would make the choice depend on nothing.
        pids = sorted((int(p) for p in os.listdir("/proc") if p.isdigit()))
    except OSError:
        return None
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if GATEWAY_CMDLINE in cmdline:
            return pid
    return None


async def restart_gateway() -> dict:
    """Restart the gateway so saved channel changes come up.

    SIGUSR1, which the gateway installs as its in-band restart: it drains any
    turn in flight, then exits 75 (EX_TEMPFAIL) so s6 brings it straight back.
    That is the same path `/restart` and `hermes gateway restart` take, so this
    inherits the drain rather than dropping live conversations.

    Two paths were tried first and are recorded here because both look right:

    * Hermes's own POST /api/gateway/restart runs `hermes gateway restart`
      inside the hermes-dashboard container, which is not where the gateway
      lives. It would find no gateway there and start a second one, which the
      gateway treats as a fatal token collision (EX_CONFIG, exit 78). Its
      `gateway_running: false` is that same blind spot showing through.

    * Sending "/restart" over the gateway's API server does not restart
      anything. The API server platform does not dispatch it as a gateway
      command — it reaches the model, which answers conversationally
      ("Restarted context on my side") and returns 200. A caller that trusts
      the status code reports a restart that never happened.

    Needs `pid: service:hermes-gateway` on this service so the process is
    visible, and root in that namespace to signal it. Both are narrower than
    mounting the docker socket, which would trade one restart for control of
    every container on the host.
    """
    pid = _find_gateway_pid()
    if pid is None:
        raise ChannelsUnavailable(
            "The gateway process is not visible from this container, so it "
            "cannot be restarted from here. Add `pid: service:hermes-gateway` "
            "to the light-dashboard service and recreate it — or restart it "
            "yourself with `docker restart hermes-gateway`."
        )
    try:
        os.kill(pid, signal.SIGUSR1)
    except PermissionError as exc:
        raise ChannelsUnavailable(
            f"Not permitted to signal the gateway (pid {pid}): {exc}"
        ) from exc
    except ProcessLookupError:
        raise ChannelsUnavailable(
            "The gateway exited between finding it and signalling it; it is "
            "probably restarting already."
        )
    return {"ok": True, "pid": pid}
