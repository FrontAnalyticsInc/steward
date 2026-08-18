"""Perform the outcome a human approved.

One function per action id in the reviewer's action table. Each takes the item
as decided and returns a result dict; raising means the action failed and the
item lands in `failed/` for a person to look at.

The whole module is written so it can be unit-tested with fake service objects:
nothing here builds a credential at import time, and every Google call goes
through an injected `service`.

Retryability is not uniform, and the difference is the point:

  create_draft   retryable. A duplicate draft sits in Drafts until deleted.
  apply_labels   retryable. Applying a label twice is applying it once.
  send           NOT retryable. There is no unsend. A send that failed in a way
                 we cannot interpret is treated as one that happened.

That asymmetry is why the executor asks each handler rather than applying one
retry policy over the top.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ExecutionError(Exception):
    """A handler failed. `retryable` decides whether a human may re-run it."""

    def __init__(self, message: str, *, kind: str = "error", retryable: bool = True):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


def _body_of(item: dict[str, Any]) -> str:
    """What to send: the reviewer's edit if they made one, else the draft.

    `edited_body` is only written when it differs, so its presence means someone
    changed the words and its absence means they were happy with them.
    """
    edited = item.get("edited_body")
    if isinstance(edited, str) and edited.strip():
        return edited
    return item.get("body") or ""


def _recipient_of(item: dict[str, Any]) -> str:
    recipient = item.get("recipient") or {}
    address = (recipient.get("address") or "").strip()
    if not address:
        raise ExecutionError(
            "This item has no recipient address, so there is nothing to send to.",
            kind="invalid_item",
            retryable=False,
        )
    return address


def _reply_headers(item: dict[str, Any]):
    """(header value for In-Reply-To/References, Gmail threadId).

    Two different identifiers for the same parent, and they are not swappable.
    The first goes in the message the recipient receives, so it has to be the
    parent's RFC Message-ID; the second goes to the Gmail API, so it has to be
    the API's own thread handle. Items queued before `rfc_message_id` was
    carried have no header value — Gmail still threads those by thread_id, so
    they degrade to what they already did rather than failing.
    """
    parent = item.get("in_reply_to") or {}
    return parent.get("rfc_message_id"), parent.get("thread_id")


def execute_create_draft(item: dict[str, Any], *, gmail_service=None) -> dict[str, Any]:
    """Put the approved message in the human's Drafts, threaded under its parent."""
    from app import gmail_api, mailer_as_principal

    to = _recipient_of(item)
    subject = item.get("subject") or ""
    in_reply_to, thread_id = _reply_headers(item)

    # Built by the same function the send path uses, so choosing "draft" over
    # "send" is a choice about who presses the button, not about what the mail
    # looks like.
    message = mailer_as_principal.build_message(
        to=to, subject=subject, body=_body_of(item), in_reply_to=in_reply_to
    )
    service = gmail_service or gmail_api.build_service()
    return gmail_api.create_draft(
        service, mailer_as_principal.encode(message), thread_id=thread_id
    )


def execute_send(item: dict[str, Any], *, mailer=None) -> dict[str, Any]:
    """Send the approved message, as the human, now."""
    from app import mailer_as_principal

    sender = mailer or mailer_as_principal
    if not sender.configured():
        raise ExecutionError(
            "No credential is configured to send as the principal. Set "
            "PRINCIPAL_MAILER_SERVICE_ACCOUNT_FILE and PRINCIPAL_MAILER_SENDER.",
            kind="capability_missing",
            retryable=False,
        )

    to = _recipient_of(item)
    in_reply_to, thread_id = _reply_headers(item)
    sent_id = sender.send(
        to=to,
        subject=item.get("subject") or "",
        body=_body_of(item),
        in_reply_to=in_reply_to,
        thread_id=thread_id,
    )
    return {"message_id": sent_id, "thread_id": thread_id, "to": to}


def execute_apply_labels(item: dict[str, Any], *, gmail_service=None) -> dict[str, Any]:
    """File the filter's example messages under its label.

    What an approved filter can actually do here. The standing rule would need
    gmail.settings.basic, which this service account has not been delegated, so
    the reviewer offers this instead: the same disposition, applied to the
    messages the proposal was derived from, and reversible from Gmail.
    """
    from app import gmail_api

    rule = item.get("rule") or {}
    label = rule.get("add_label")
    ids = [str(i) for i in (rule.get("example_message_ids") or []) if i]
    if not label:
        raise ExecutionError(
            "This filter proposal names no label to apply.",
            kind="invalid_item",
            retryable=False,
        )
    if not ids:
        # Not a failure: the rule was approved, there was simply nothing already
        # in the mailbox matching it. Saying so beats reporting an error for a
        # correct outcome.
        return {"labeled_message_ids": [], "note": "No example messages to file."}

    service = gmail_service or gmail_api.build_service()
    label_id = gmail_api.ensure_label(service, label)
    if not label_id:
        raise ExecutionError(
            f"Could not find or create the label {label!r}.", kind="label_missing"
        )

    remove = ["INBOX"] if rule.get("remove_from_inbox") else []
    gmail_api.batch_modify(service, ids, add=[label_id], remove=remove)
    return {"labeled_message_ids": ids, "label": label, "removed_from_inbox": bool(remove)}


def execute_create_filter(item: dict[str, Any], **_kwargs) -> dict[str, Any]:
    """Not reachable today, and says so rather than half-doing it.

    The reviewer marks this action unavailable because gmail.settings.basic is
    not in the service account's domain-wide delegation, so it should never
    arrive here. If it does — a hand-written item, a stale client — failing
    loudly and unretryably is the honest answer.
    """
    raise ExecutionError(
        "Creating a standing Gmail filter needs the gmail.settings.basic scope, "
        "which is not in this service account's domain-wide delegation.",
        kind="capability_missing",
        retryable=False,
    )


# Keyed by the `executor` name in the reviewer's action table.
HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "create_draft": execute_create_draft,
    "send": execute_send,
    "apply_labels": execute_apply_labels,
    "create_filter": execute_create_filter,
}

# Actions safe to re-run. Anything absent is retried only by a human who has
# decided the duplicate risk is acceptable, and `send` is never in here.
RETRYABLE = {"create_draft", "apply_labels"}

# What each action actually produced, in the fleet-wide vocabulary. This is the
# point where a queued intention becomes a counted side effect: the producer
# counts a `review_item` when it queues something, and the effect itself is
# counted here, once, only after the handler returned without raising.
#
# `send` is `approved_email` and never `auto_email` — a human read this one and
# approved it, and `auto_email` is reserved for mail that went out with nobody
# looking. Conflating them would break the one number that says how much
# unsupervised sending happens.
#
# `apply_labels` and `record` produce nothing: filing mail into a label changes
# nothing outside this system and creates no artifact. `apply_labels` reports
# what it touched instead, which the caller fills in from the handler's result.
PRODUCED_BY_ACTION: dict[str, str] = {
    "create_draft": "draft_email",
    "send": "approved_email",
}


def run(item: dict[str, Any], **kwargs) -> dict[str, Any]:
    """Dispatch one approved item to its handler."""
    executor = (item.get("execution") or {}).get("executor")
    handler = HANDLERS.get(executor)
    if handler is None:
        raise ExecutionError(
            f"No executor named {executor!r}.", kind="unknown_executor", retryable=False
        )
    return handler(item, **kwargs)
