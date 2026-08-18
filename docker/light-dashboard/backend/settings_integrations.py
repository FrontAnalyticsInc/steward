"""The three read-only halves of the integrations settings page.

The fourth — the assistant's MCP connections — is :mod:`mcp_servers`, and it is
the only one that writes. The split is not arbitrary: an MCP server is Hermes
configuration this dashboard can edit through Hermes's own API, while everything
here is the *workflows* service's compose environment, which takes effect when
that container is recreated. Offering a save button over the second kind would
be describing a future state of a process that had not read it yet.

Where each answer comes from, and why it cannot come from anywhere else:

  workflow access   the workflows service, over /integration-config. Only that
  automation outputs process can see its own credentials — they are environment
                    variables in another container, absent from every file this
                    one mounts. It was previously guessed at: the Integrations
                    tab could see that attio_api.py reads ATTIO_API_KEY and had
                    no way to know whether the key was set.

  email identity    both sides. The workflows half comes from the same snapshot;
                    the assistant's half is the OAuth token under
                    ~/.hermes/gmail-mcp, which this container does mount.

Which agents actually use each client is folded in from the source scan the
Integrations tab already does, so "what has access" and "who uses it" are one
answer rather than two screens.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from .integrations import declared_adk_modules

# Where `hermes mcp login gmail` parks the assistant's token. Same directory the
# Integrations tab reads for MCP auth state; named here rather than imported
# because this is a specific server's credential, not the token dir as a whole.
GMAIL_MCP_DIR = "gmail-mcp"
GMAIL_MCP_CREDENTIALS = "credentials.json"


def _workflow_snapshot(workflows_url: str) -> Dict[str, Any]:
    """Ask the workflows service what it is allowed to reach.

    A service that is down is reported as unreachable, never as "nothing is
    configured". They look identical in an empty list and only one of them means
    someone should go and look at a container.
    """
    url = f"{(workflows_url or '').rstrip('/')}/integration-config"
    data = None
    if workflows_url:
        try:
            resp = httpx.get(url, timeout=8.0)
            if resp.status_code < 400:
                data = resp.json()
        except (httpx.HTTPError, ValueError):
            data = None
    if not isinstance(data, dict):
        return {
            "reachable": False,
            "error": (
                f"The workflows service did not answer at {url}. Its credentials "
                "live in that container's environment, so nothing else on this "
                "host can report them."
            ),
            "access": [],
            "outputs": [],
            "identities": [],
            "source": None,
        }
    return {
        "reachable": True,
        "error": None,
        "access": data.get("access") or [],
        "outputs": data.get("outputs") or [],
        "identities": data.get("identities") or [],
        "source": data.get("source"),
    }


def _with_users(rows: List[dict], src_dir: str) -> List[dict]:
    """Fold in which agents import each client module.

    A credential nothing uses is worth seeing as plainly as one that is
    missing — it is usually a grant issued and forgotten, which is exactly the
    kind of access that outlives its reason.
    """
    declared = declared_adk_modules(src_dir)
    by_module = {
        entry.get("module"): entry
        for entry in declared.values()
        if isinstance(entry, dict)
    }
    out = []
    for row in rows:
        row = dict(row)
        entry = by_module.get(row.get("module")) or {}
        row["used_by"] = entry.get("used_by") or []
        # The module's own docstring line, when the scan found one. Deliberately
        # not a fallback for the label: an empty summary is fine, a wrong one is
        # not.
        row["summary"] = entry.get("summary")
        out.append(row)
    return out


def _assistant_identity(db_dir: str) -> Dict[str, Any]:
    """The mailbox the default agent is signed into, from its token file alone.

    Reads scopes and expiry and nothing else — never the access or refresh
    token. The token file does not carry the address, so this reports what it
    can prove (signed in, with these scopes, until then) and leaves naming the
    account to Google, which is the only party that can.
    """
    path = os.path.join(db_dir, GMAIL_MCP_DIR, GMAIL_MCP_CREDENTIALS)
    out: Dict[str, Any] = {
        "key": "assistant_mail",
        "label": "The assistant reads and sends as",
        "address": None,
        "how": "Interactive OAuth, through the gmail MCP server.",
        "note": (
            "Human-scoped and consented once in a browser. This is the grant the "
            "chat uses when you ask it to check mail — deliberately not the "
            "unattended one the workflows hold."
        ),
        "signed_in": False,
        "scopes": [],
        "expires_at": None,
        "expired": None,
        "has_refresh_token": None,
        # The field the UI should render, and the reason it is not `expired`:
        # a Google access token lasts an hour, so this file is expired most of
        # the time and the MCP server refreshes it on the next call without
        # anyone noticing. A page that showed "expired" would be raising an
        # alarm on almost every load about something that is working. What
        # actually needs a person is an expired token with nothing to refresh
        # it from.
        "needs_reauth": False,
        "change_with": "hermes mcp login gmail",
    }
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return out
    out["signed_in"] = True
    scope = data.get("scope")
    if isinstance(scope, str):
        # Google returns them space-separated; the short tail is what a person
        # reads ("gmail.modify"), so keep both halves available to the UI.
        out["scopes"] = [s.rsplit("/", 1)[-1] for s in scope.split() if s]
    out["has_refresh_token"] = bool(data.get("refresh_token"))
    expires_at = data.get("expiry_date")
    if isinstance(expires_at, (int, float)):
        # Google's expiry_date is milliseconds; every other timestamp this
        # dashboard passes to the frontend is seconds, and mixing the two puts
        # a token's expiry in the year 57000.
        seconds = expires_at / 1000.0 if expires_at > 1e11 else float(expires_at)
        out["expires_at"] = seconds
        out["expired"] = seconds <= time.time()
    out["needs_reauth"] = bool(out["expired"]) and not out["has_refresh_token"]
    return out


def build(db_dir: str, src_dir: str, workflows_url: str) -> Dict[str, Any]:
    """Everything the settings page needs that is not an MCP server."""
    snap = _workflow_snapshot(workflows_url)
    identities = [_assistant_identity(db_dir)] + [
        dict(row) for row in snap["identities"]
    ]
    return {
        "workflow_access": _with_users(snap["access"], src_dir),
        "outputs": _with_users(snap["outputs"], src_dir),
        "identities": identities,
        "workflows": {
            "reachable": snap["reachable"],
            "error": snap["error"],
            "source": snap["source"],
        },
    }
