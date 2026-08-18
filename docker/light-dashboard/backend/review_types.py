"""What each kind of review item is, and what a human is allowed to do with it.

This table is the enforcement point for the whole review queue, so it lives with
the reviewer rather than with the producer. The producer — an ADK agent whose
input is arbitrary inbound mail, scraped web pages and calendar text — is the
untrusted side of the boundary. That is why it can only write ``pending/`` (the
mount at docker-compose.yml enforces it) and why it does not get to say what may
be done with what it wrote.

An action list is a capability grant. ``"actions": ["send"]`` written by an agent
that just read an attacker's email is precisely the escalation the pending-only
mount exists to prevent, so the legal set is derived here from ``review_type``
and the client's requested action is checked against it before any file moves.

A producer MAY set ``suggested_action`` to hint which button should be primary.
It is a hint: validated against this table, ignored when illegal.

Secondary reason for deriving rather than storing: items sit in this queue for
days. A list frozen at write time means every already-queued item is stuck with
the capabilities that existed the morning it was created.
"""

from __future__ import annotations

import os

# --- Capabilities -------------------------------------------------------------
#
# What credentials this deployment actually holds, named so an action can say
# what it needs and the UI can say why a button is greyed out rather than
# failing at execution time with a Google error nobody can act on.
#
# Verified against the writer service account
# (local-hermes-adk-write-access, client_id 118085083097359663968) by minting a
# token for each scope/subject pair:
#
#   gmail.modify         as alton@  OK    - triage already uses it
#   gmail.send           as alton@  OK    - domain-wide delegation is per client,
#                                           the subject is chosen at runtime
#   gmail.send           as evan@   OK    - what mailer.py uses
#   gmail.settings.basic as alton@  FAIL  - unauthorized_client
#
# DWD scope matching is literal, not hierarchical: a scope absent from the admin
# console fails even when a broader granted scope would imply it. So
# gmail.settings.basic — and therefore real Gmail filter creation — is not
# available until someone adds it there.
CAP_GMAIL_MODIFY = "gmail_modify"
CAP_GMAIL_SEND_AS_PRINCIPAL = "gmail_send_as_principal"
CAP_GMAIL_SETTINGS = "gmail_settings_basic"

CAPABILITY_LABELS = {
    CAP_GMAIL_MODIFY: "gmail.modify as alton@",
    CAP_GMAIL_SEND_AS_PRINCIPAL: "gmail.send as alton@",
    CAP_GMAIL_SETTINGS: "gmail.settings.basic as alton@",
}

# Overridable so a deployment without the writer service account can turn the
# send button off without a code change, and so tests can pin it. Empty string
# means "none"; unset means the verified default above.
DEFAULT_CAPABILITIES = f"{CAP_GMAIL_MODIFY},{CAP_GMAIL_SEND_AS_PRINCIPAL}"


def available_capabilities() -> set:
    raw = os.environ.get("REVIEW_CAPABILITIES")
    if raw is None:
        raw = DEFAULT_CAPABILITIES
    return {part.strip() for part in raw.split(",") if part.strip()}


# --- Review types -------------------------------------------------------------

GENERIC_TYPE = "unknown"

# Items written before `review_type` existed carry only `channel`. Mapping is
# read-only and one-way: nothing on disk is rewritten.
LEGACY_CHANNEL_TO_TYPE = {
    "email": "email_draft",
    "gmail_filter": "gmail_filter",
}

REVIEW_TYPE_LABELS = {
    "email_draft": "Email draft",
    "gmail_filter": "Gmail filter",
    "code_review": "Code review",
    "memory_fact": "Memory fact",
    "crm_todo": "CRM todo",
    # Written by beacon_crm_review. Unlabelled until now, so the queue showed
    # the raw wire value — "crm_task" — as the heading of the item's own page.
    "crm_task": "CRM task",
    "crm_update": "CRM update",
    "content_publish": "Content",
    GENERIC_TYPE: "Review item",
}

# `executor` names the handler in workflows/app/review_executors.py. None means
# the decision is the whole outcome — recording it is all that was ever asked.
#
# `destructive` marks an action that cannot be walked back. The UI requires a
# confirmation step for these and refuses to bind them to a bare keystroke: a
# duplicate draft is recoverable, a sent email is not.
REVIEW_ACTIONS = {
    "email_draft": [
        {
            "id": "create_draft",
            "label": "Approve & create draft",
            "hint": "Lands in your Gmail Drafts, threaded under the original. You send it.",
            "primary": True,
            "executor": "create_draft",
            "requires": CAP_GMAIL_MODIFY,
            "destructive": False,
        },
        {
            "id": "send",
            "label": "Approve & send",
            "hint": "Sends immediately, as you. There is no undo.",
            "primary": False,
            "executor": "send",
            "requires": CAP_GMAIL_SEND_AS_PRINCIPAL,
            "destructive": True,
        },
    ],
    "gmail_filter": [
        {
            "id": "apply_labels",
            "label": "Approve & file the examples",
            "hint": "Applies the label to the example messages now. Reversible.",
            "primary": True,
            "executor": "apply_labels",
            "requires": CAP_GMAIL_MODIFY,
            "destructive": False,
        },
        {
            "id": "create_filter",
            "label": "Approve & create the filter",
            "hint": "Creates a standing Gmail filter for future mail.",
            "primary": False,
            "executor": "create_filter",
            "requires": CAP_GMAIL_SETTINGS,
            "destructive": False,
        },
        {
            "id": "record",
            "label": "Approve & record only",
            "hint": "Notes that the rule is good. Changes nothing in Gmail.",
            "primary": False,
            "executor": None,
            "requires": None,
            "destructive": False,
        },
    ],
    # A kanban task whose worker finished code it will not self-certify. The
    # store is kanban.db rather than approvals/ — see kanban_review.py — but the
    # capability argument is the same one this table exists to make: the worker
    # asks to be reviewed, and does not get to say what the review may do. It
    # cannot mark itself done, and it cannot decide that "changes requested"
    # means "done with a note".
    #
    # No `executor`: both outcomes are database transitions this backend owns,
    # not work handed to the approval executor. Rejection is not a dead end here
    # — REJECT_ACTION already requires a reason, and that reason becomes the
    # comment the worker reads when the task returns to the board — so the
    # generic reject flow is the request-changes flow, and there is no second
    # button that would let it be rejected without saying why.
    "code_review": [
        {
            "id": "approve_done",
            "label": "Approve & mark done",
            "hint": "Completes the task. The board moves on to whatever was waiting for it.",
            "primary": True,
            "executor": None,
            "requires": None,
            "destructive": False,
        },
    ],
    GENERIC_TYPE: [
        {
            "id": "record",
            "label": "Approve",
            "hint": "Records the approval. No automated action is wired for this type yet.",
            "primary": True,
            "executor": None,
            "requires": None,
            "destructive": False,
        },
    ],
}

REJECT_ACTION = {
    "id": "reject",
    "label": "Reject",
    "primary": False,
    "executor": None,
    "requires": None,
    "destructive": False,
    "requires_reason": True,
}


# What rejecting *means* differs by type, and the button should say so. For a
# draft it is "this does not go out" and the item is finished. For a task it is
# "not yet, here is what to change" — the reason becomes a comment and the task
# goes back to the board, so calling it Reject would describe the wrong outcome.
# Only the wording varies: the reason is still required, and the action id the
# server checks is still `reject`.
REJECT_LABELS = {
    "code_review": "Request changes",
}


def reject_action_for(review_type: str) -> dict:
    action = dict(REJECT_ACTION)
    action["label"] = REJECT_LABELS.get(review_type, REJECT_ACTION["label"])
    return action


def review_type_of(item) -> str:
    """The item's type, for items old and new alike.

    ``review_type`` is authoritative when present. ``channel`` is the fallback
    for the items written before it existed. Anything matching neither is
    ``unknown``: it still renders, through the generic template, because an item
    a human was asked to look at must never vanish from the queue just because
    this reviewer does not recognise it.
    """
    if not isinstance(item, dict):
        return GENERIC_TYPE
    declared = item.get("review_type")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return LEGACY_CHANNEL_TO_TYPE.get(item.get("channel"), GENERIC_TYPE)


def actions_for(review_type: str, *, suggested_action=None, capabilities=None):
    """The legal actions for a type, annotated with what this host can do.

    Returns copies: callers attach per-item state and must not mutate the table.
    """
    caps = available_capabilities() if capabilities is None else set(capabilities)
    template = REVIEW_ACTIONS.get(review_type) or REVIEW_ACTIONS[GENERIC_TYPE]

    out = []
    for action in template:
        entry = dict(action)
        needed = entry.get("requires")
        if needed and needed not in caps:
            entry["available"] = False
            entry["unavailable_reason"] = (
                f"This deployment has no {CAPABILITY_LABELS.get(needed, needed)} credential. "
                "Add the scope to the service account's domain-wide delegation to enable it."
            )
        else:
            entry["available"] = True
            entry["unavailable_reason"] = None
        out.append(entry)

    # The hint may only move the highlight between actions that are already
    # legal for this type. It cannot introduce one.
    if suggested_action and any(a["id"] == suggested_action for a in out):
        for entry in out:
            entry["primary"] = entry["id"] == suggested_action

    # An unavailable action must never be the primary one — the big green button
    # would be the disabled one. Promote the first available action instead.
    if not any(a["primary"] and a["available"] for a in out):
        for entry in out:
            entry["primary"] = False
        for entry in out:
            if entry["available"]:
                entry["primary"] = True
                break

    return out


def find_action(review_type: str, action_id: str, *, capabilities=None):
    for entry in actions_for(review_type, capabilities=capabilities):
        if entry["id"] == action_id:
            return entry
    return None


def legal_action_ids(review_type: str):
    return [a["id"] for a in actions_for(review_type)] + [REJECT_ACTION["id"]]
