"""What this service is allowed to reach, and where its output is allowed to go.

Read-only, and never values that are secret. The dashboard has no way to answer
this on its own: the credentials live in the compose environment of *this*
container, so a screen in another container can either be told by this process
or guess. It was guessing — the Integrations tab could see that `attio_api.py`
reads `ATTIO_API_KEY` and could not see whether that key was set.

Two things this deliberately does not do:

  * It does not restate whether a credential works. `configured()` means the
    inputs are present, which is a different claim from "the token is valid and
    the Workspace admin granted the delegation". A screen that conflated them
    would call a service account with no domain-wide delegation healthy right
    up until the first unauthorized_client.

  * It does not accept writes. These come from docker/.env and take effect when
    the container is recreated; a settings page that edited them would be
    describing a future state of a process that had not read them yet.

The scope notes travel with the code that holds each credential rather than
living in the dashboard, because the reason Attio's token here is read-only is
a property of this service, not of the screen that draws it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from app import approvals, attio_api, calendar_api, gmail_api, mailer, wiki

# Kinds control display, not storage. `secret` is never sent at all — presence
# only. `address` and `path` are sent verbatim: an identity you cannot read is
# the one thing this page exists to show, and a credential path is how you find
# the file when it is the thing that is missing.
SECRET = "secret"
ADDRESS = "address"
PATH = "path"
PLAIN = "plain"


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _var(name: str, kind: str) -> Dict[str, Any]:
    """One environment variable, described without leaking it."""
    raw = _env(name)
    out: Dict[str, Any] = {"name": name, "kind": kind, "set": raw is not None}
    if raw is not None and kind != SECRET:
        out["value"] = raw
    if kind == PATH and raw is not None:
        # Set-but-missing is the common failure and it is invisible from the
        # variable alone: the compose file supplies a default of "" for every
        # credential path, so a typo reads as configured right up to the call.
        out["file_present"] = Path(raw).is_file()
    return out


def _access() -> List[Dict[str, Any]]:
    """Systems this service reads. One entry per credential, not per module."""
    return [
        {
            "key": "gmail",
            "label": "Gmail (mailbox read + relabel)",
            "module": "gmail_api",
            "configured": gmail_api.configured(),
            "scope": "gmail.modify — reads and relabels, and the API itself "
                     "refuses a send from this credential",
            "guardrail": "Its own credential, never the gateway's under "
                         "~/.hermes/gmail-mcp. That grant is human-scoped and "
                         "interactive; this one is unattended and higher-volume, "
                         "and sharing them would put one identity in the audit "
                         "log for two very different actors.",
            "vars": [
                _var(gmail_api.SERVICE_ACCOUNT_FILE, PATH),
                _var(gmail_api.DELEGATED_USER, ADDRESS),
                _var(gmail_api.TOKEN_FILE, PATH),
            ],
        },
        {
            "key": "calendar",
            "label": "Calendar (day read)",
            "module": "calendar_api",
            "configured": calendar_api.configured(),
            "scope": "calendar.readonly — cannot move or delete an event",
            "guardrail": "A different service account from Gmail's on purpose: a "
                         "briefing needs to see the day and nothing more, so the "
                         "identity it uses cannot change one even if the code "
                         "asking were wrong.",
            "vars": [
                _var(calendar_api.SERVICE_ACCOUNT_FILE, PATH),
                _var(calendar_api.DELEGATED_USER, ADDRESS),
                _var("CALENDAR_BRIEFING_OWNER", ADDRESS),
                # Optional: defaults to the Hermes-wide `timezone` setting.
                # Only set it when the briefed calendar lives in a different
                # zone than the user.
                _var("CALENDAR_BRIEFING_TIMEZONE", PLAIN),
            ],
        },
        {
            "key": "attio",
            "label": "Attio CRM (record read)",
            "module": "attio_api",
            "configured": attio_api.configured(),
            "scope": "record_permission:read + object_configuration:read",
            "guardrail": "Read-only by issuance, not by convention — the "
                         "guarantee that a bug here cannot write to the CRM comes "
                         "from the token's scope. Never the OAuth grant behind "
                         "the gateway's attio MCP server.",
            "vars": [_var(attio_api.API_KEY, SECRET)],
        },
    ]


def _outputs() -> List[Dict[str, Any]]:
    """Where an automation's results can land, and what bounds each one."""
    pending = approvals.PENDING_DIR
    recipients = mailer.allowed_recipients()
    return [
        {
            "key": "mail",
            "label": "Outbound mail",
            "module": "mailer",
            "configured": mailer.configured(),
            "target": mailer.sender(),
            "scope": "gmail.send only — this credential can send and cannot read",
            "guardrail": "Sends as the assistant's own identity, never as a "
                         "person, so nothing here can put words in someone's "
                         "mouth. An empty recipient allowlist means mail goes "
                         "nowhere, which is the correct default for a sender "
                         "driven by attacker-writable input.",
            "recipients": recipients,
            "vars": [
                _var(mailer.SERVICE_ACCOUNT_FILE, PATH),
                _var(mailer.SENDER, ADDRESS),
                _var(mailer.ALLOWED_RECIPIENTS, PLAIN),
            ],
        },
        {
            "key": "approvals",
            "label": "Human approval queue",
            "module": "approvals",
            "configured": pending.is_dir(),
            "target": str(pending),
            "scope": "pending/ only",
            "guardrail": "The container mounts only pending/, so an agent has no "
                         "path to approved/ even if this code were wrong. Anything "
                         "sent as a person goes through here first.",
            "vars": [_var("APPROVALS_PENDING_DIR", PATH)],
        },
        {
            "key": "wiki",
            "label": "Wiki memory",
            "module": "memory",
            "configured": True,
            "target": str(wiki.wiki_dir()),
            "scope": "append-only markdown, one file per entity",
            "guardrail": "Each document is keyed on the address or Attio record "
                         "id, so a fact is filed under the entity it is about "
                         "rather than resolved to one by a model. Sections are "
                         "appended and never rewritten, so a later observation "
                         "that disagrees sits beside the earlier one instead of "
                         "replacing it silently.",
            "vars": [_var("HERMES_WIKI_DIR", PATH)],
        },
    ]


def _identities() -> List[Dict[str, Any]]:
    """Which mailbox each actor in this service acts as.

    The separation is the security model, so it is stated as a list of actors
    rather than a list of variables: the useful question is "who does this send
    as", and that spans two modules and three variables.
    """
    return [
        {
            "key": "workflow_read",
            "label": "Reads mail as",
            "address": _env(gmail_api.DELEGATED_USER),
            "how": "Service account with domain-wide delegation, unattended.",
            "note": "gmail.modify: reads and relabels. Cannot send.",
        },
        {
            "key": "workflow_send",
            "label": "Sends mail as",
            "address": mailer.sender(),
            "how": "Service account, gmail.send only.",
            "note": "The assistant's own identity. Mail as a person is not sent "
                    "from here — it goes to the approval queue.",
        },
        {
            "key": "calendar_read",
            "label": "Reads the calendar of",
            "address": _env("CALENDAR_BRIEFING_OWNER") or _env(calendar_api.DELEGATED_USER),
            "how": "Read-only service account.",
            "note": "Sees the day; cannot change it.",
        },
    ]


def snapshot() -> Dict[str, Any]:
    """Everything above, in one read. Safe to serve unauthenticated on loopback."""
    return {
        "access": _access(),
        "outputs": _outputs(),
        "identities": _identities(),
        # Where these come from, so the page can say how to change them without
        # the dashboard hardcoding a path into another repo's deployment.
        "source": {
            "kind": "compose-env",
            "file": "docker/.env",
            "service": "hermes-workflows",
            "apply": "docker compose up -d hermes-workflows",
        },
    }
