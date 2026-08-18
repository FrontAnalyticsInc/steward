"""MCP connections for the default profile — read and write, against Hermes's API.

Why this is a proxy and not a writer
------------------------------------
An MCP connection lives in the profile's ``config.yaml`` under ``mcp_servers``,
and adding one is not "write four keys": Hermes validates the command and args
against a suspicious-configuration check, parks bearer tokens in the profile's
``.env`` rather than the config, and keeps its own CLI (``hermes mcp list``)
reading the same data layer. All of that is already implemented behind the
Hermes dashboard container:

    GET    /api/mcp/servers                 the servers, secrets redacted
    POST   /api/mcp/servers                 add one
    DELETE /api/mcp/servers/<name>          remove one
    PUT    /api/mcp/servers/<name>/enabled  turn one on or off
    POST   /api/mcp/servers/<name>/test     connect, list tools, disconnect
    PUT    /api/config                      deep-merge (used for edits)

So this module holds a client, not a config writer. Same reasoning as
:mod:`channels`, and the same session — see :mod:`hermes_api`.

The default profile, and why nothing here takes a profile argument
------------------------------------------------------------------
Every Hermes route above takes an optional ``profile``; omitting it means the
default profile, which is the one the chat on this dashboard talks to. This
page is reached from that conversation's Connections list, so it answers for
that conversation and no other. Naming the profile explicitly would be worse,
not better: ``/opt/data`` *is* the default profile's home here, and a literal
"default" query arg would be a second way of saying the same thing that could
drift from it.

Applying: no restart button
---------------------------
The agent watches ``config.yaml`` and reloads its MCP surface within about five
seconds of the file changing (``mcp.auto_reload_on_config_change``, default
on) — so unlike a channel edit, a connection change lands on the running
conversation by itself. :func:`reload_policy` reads that key so the page can
say which of the two is true rather than guessing.
"""

import os
from typing import Any, Dict, List, Optional

import yaml

from .hermes_api import HermesUnavailable, detail_of
from .hermes_api import client as _client

# The same token directory the Integrations tab reads, and the same reader —
# `hermes mcp login` parks OAuth state there, and whether a server has a live
# token is a fact about the filesystem, not something the config API reports.
# Imported rather than re-implemented so the two screens cannot disagree about
# what "signed in" means.
from .integrations import TOKEN_DIR_NAME, _oauth_status


class MCPUnavailable(HermesUnavailable):
    """Hermes's dashboard could not be reached, or rejected the change."""


class MCPNotFound(MCPUnavailable):
    """No connection by that name on the default profile.

    Split from the general failure because the two are different answers to
    the caller: a 502 says "the link to Hermes is broken, try again", and this
    says "your list is stale — reload it". A page that showed the first when
    the second was true would have someone retrying a delete against a
    connection another window already removed.
    """


def _target(server: Dict[str, Any]) -> str:
    """The one-line "what does this connect to" string.

    http servers are their URL; stdio servers are the command line that spawns
    them. Shown verbatim, placeholders and all: ``${HERMES_HOME}`` resolves
    inside the gateway container but not for a host-side CLI, and silently
    expanding it here would hide that discrepancy — the same rule
    ``main.mcp_servers_for`` follows.
    """
    if server.get("url"):
        return str(server["url"])
    command = server.get("command")
    if not command:
        return ""
    return " ".join([str(command)] + [str(a) for a in (server.get("args") or [])])


def _decorate(server: Dict[str, Any], token_dir: str) -> Dict[str, Any]:
    """Hermes's server summary plus the two things it does not carry."""
    out = dict(server)
    out["target"] = _target(server)
    out["oauth"] = _oauth_status(token_dir, str(server.get("name") or ""), server.get("auth"))
    return out


def reload_policy(data_dir: str) -> Dict[str, Any]:
    """Does the running agent pick up a connection change on its own?

    Read from the profile's config rather than assumed, because the answer
    changes what the page should tell someone who just saved: with auto-reload
    on it is "live in a few seconds", and with it off it is "run /reload-mcp",
    and a page that says the first when the second is true sends people to
    debug a connection that was never loaded.

    A missing ``mcp`` key means auto-reload — that is Hermes's default, and
    this profile's config carries no override.
    """
    path = os.path.join(data_dir, "config.yaml")
    auto = True
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        mcp = cfg.get("mcp")
        if isinstance(mcp, dict):
            auto = mcp.get("auto_reload_on_config_change", True) is not False
    except (OSError, ValueError, yaml.YAMLError):
        # Unreadable config is not a claim about reload behaviour. Report the
        # default rather than raising: the list itself came from Hermes and is
        # still worth showing.
        pass
    return {"auto_reload": auto}


async def list_servers(data_dir: str) -> dict:
    """Every MCP connection on the default profile, with its auth state."""
    resp = await _client.request("GET", "/api/mcp/servers", timeout=20.0)
    if resp.status_code >= 400:
        raise MCPUnavailable(
            detail_of(resp, f"The Hermes dashboard returned {resp.status_code} for the MCP servers.")
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise MCPUnavailable("The Hermes dashboard returned a malformed MCP server list.") from exc
    token_dir = os.path.join(data_dir, TOKEN_DIR_NAME)
    servers = [
        _decorate(s, token_dir)
        for s in payload.get("servers") or []
        if isinstance(s, dict)
    ]
    return {
        "servers": servers,
        "profile": "default",
        **reload_policy(data_dir),
    }


async def add_server(
    name: str,
    url: Optional[str],
    command: Optional[str],
    args: List[str],
    env: Dict[str, str],
    auth: Optional[str],
    bearer_token: Optional[str],
) -> dict:
    """Add one connection. Hermes validates it; this only forwards.

    ``bearer_token`` is provisioning input, not config: Hermes writes it to the
    profile's ``.env`` and stores a ``${VAR}`` reference in the header, so it
    never lands in config.yaml and never comes back out of the read endpoint.
    """
    body: Dict[str, Any] = {"name": name, "args": args or [], "env": env or {}}
    if url:
        body["url"] = url
    if command:
        body["command"] = command
    if auth:
        body["auth"] = auth
    if bearer_token:
        body["bearer_token"] = bearer_token
    resp = await _client.request("POST", "/api/mcp/servers", json=body, timeout=30.0)
    if resp.status_code >= 400:
        raise MCPUnavailable(detail_of(resp, f"Hermes rejected the new connection ({resp.status_code})."))
    try:
        return resp.json()
    except ValueError:
        return {"ok": True, "name": name}


async def remove_server(name: str) -> dict:
    """Remove one connection from the profile's config."""
    resp = await _client.request(
        "DELETE", f"/api/mcp/servers/{_quote(name)}", timeout=20.0
    )
    if resp.status_code == 404:
        raise MCPNotFound(f"There is no connection called '{name}' on the default profile.")
    if resp.status_code >= 400:
        raise MCPUnavailable(detail_of(resp, f"Hermes rejected the removal ({resp.status_code})."))
    return {"ok": True, "name": name}


async def set_enabled(name: str, enabled: bool) -> dict:
    """Turn a connection on or off, keeping its settings either way."""
    resp = await _client.request(
        "PUT",
        f"/api/mcp/servers/{_quote(name)}/enabled",
        json={"enabled": bool(enabled)},
        timeout=20.0,
    )
    if resp.status_code == 404:
        raise MCPNotFound(f"There is no connection called '{name}' on the default profile.")
    if resp.status_code >= 400:
        raise MCPUnavailable(detail_of(resp, f"Hermes rejected the change ({resp.status_code})."))
    return {"ok": True, "name": name, "enabled": bool(enabled)}


async def edit_server(name: str, fields: Dict[str, Any]) -> dict:
    """Change where an existing connection points, leaving its secrets alone.

    Sent through ``PUT /api/config``, which deep-merges into config.yaml, and
    NOT through the whole-map replace endpoint. The read endpoint redacts env
    values ("sk-1…9f2c"), so a replace built from what this page can see would
    write those redactions over the real credentials. A merge of only the keys
    that were edited cannot: what it does not mention, it does not touch.

    The same property is the limitation, and the UI says so: a merge can add or
    change an env key but never delete one, and it cannot turn an http server
    into a stdio one. Both of those are remove-and-re-add.
    """
    if not fields:
        return {"ok": True, "name": name}
    resp = await _client.request(
        "PUT",
        "/api/config",
        json={"config": {"mcp_servers": {name: fields}}},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise MCPUnavailable(detail_of(resp, f"Hermes rejected the edit ({resp.status_code})."))
    return {"ok": True, "name": name}


async def test_server(name: str) -> dict:
    """Connect, list tools, disconnect. Hermes's own probe, forwarded whole.

    Never raises on a failed probe — "cannot connect" is the answer the button
    was pressed to get. Only a broken link to the dashboard itself raises,
    which :class:`MCPUnavailable` already means everywhere else here.
    """
    resp = await _client.request(
        "POST", f"/api/mcp/servers/{_quote(name)}/test", timeout=120.0
    )
    if resp.status_code == 404:
        raise MCPNotFound(f"There is no connection called '{name}' on the default profile.")
    if resp.status_code >= 400:
        raise MCPUnavailable(detail_of(resp, f"The probe could not be run ({resp.status_code})."))
    try:
        return resp.json()
    except ValueError as exc:
        raise MCPUnavailable("The Hermes dashboard returned a malformed probe result.") from exc


def _quote(name: str) -> str:
    from urllib.parse import quote

    return quote(name, safe="")
