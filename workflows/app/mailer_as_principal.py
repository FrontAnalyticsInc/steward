"""Outbound mail sent as the human, after a human approved it.

This is the far side of the review queue. `mailer.py` says "Anything sent *as
Alton* still goes through the approval queue" — this module is what comes out
the other end of that queue, and nothing else may call it.

Why a separate module rather than a subject argument on `mailer.py`:

  `mailer.py` sends as evan@ and only evan@. That is not a default, it is the
  property the module exists to hold — mail it produces is visibly from the
  assistant, so an injected instruction reaching that sender cannot put words in
  Alton's mouth. Making its subject a parameter would delete that property for
  every caller, including the unattended ones that depend on it, in exchange for
  saving a file.

  So the two senders are two modules with two identities and two audit-log
  sources. Which one sent a message is answerable from the Integrations screen
  without reading call sites.

The credential is the same writer service account, which holds domain-wide
delegation for gmail.send across the domain; the subject is chosen at runtime.
Scope is gmail.send alone — this can send and cannot read, so a compromised call
site cannot use it to walk a mailbox.

Two deliberate differences from `mailer.py`:

  No recipient allowlist. `mailer.py` fails closed because it is driven by
  attacker-writable calendar text with no human in the loop, so the set of
  addresses it may reach has to be pinned in advance. This module is only ever
  reached from a specific message a specific person read and approved, and a
  reply goes to whoever wrote in — an allowlist would refuse nearly every real
  item in the queue and teach everyone to widen it until it meant nothing.

  The control here is instead: a human read the body, the executor holds
  `approved/` read-only so it can never approve its own work, and the route that
  writes `approved/` requires a same-origin request. If any of those three go,
  this module is the thing they were protecting.

No retry, for the same reason `mailer.py` has none, and more sharply: a
duplicated reply to a client cannot be recalled. The executor treats a send that
fails ambiguously as one that happened.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from email.message import EmailMessage
from typing import Optional

from app import integration_log, markdown_email

logger = logging.getLogger(__name__)

# Send only. Not gmail.modify, not mail.google.com.
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# Its own source, so the Integrations screen shows "mail sent as the human"
# separately from "mail sent as the assistant". Folding them together would put
# one row in the audit log for two very different acts.
INTEGRATION_SOURCE = "gmail_send_as_principal"
INTEGRATION_LABEL = "Outbound mail (as the human, approved)"

SERVICE_ACCOUNT_FILE = "PRINCIPAL_MAILER_SERVICE_ACCOUNT_FILE"
SENDER = "PRINCIPAL_MAILER_SENDER"


def configured() -> bool:
    return bool(os.environ.get(SERVICE_ACCOUNT_FILE) and os.environ.get(SENDER))


def sender() -> Optional[str]:
    return os.environ.get(SENDER)


def _credentials():
    sa_file = os.environ.get(SERVICE_ACCOUNT_FILE)
    subject = os.environ.get(SENDER)
    if not (sa_file and subject):
        raise RuntimeError(
            f"no principal mailer credential: set {SERVICE_ACCOUNT_FILE}+{SENDER}"
        )
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_file(
        sa_file, scopes=SCOPES
    ).with_subject(subject)


def build_service():
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=_credentials(), cache_discovery=False)


_RE_PREFIX = re.compile(r"^\s*re\s*:", re.IGNORECASE)


def _reply_subject(subject: str) -> str:
    """`Re: ` on a reply, exactly once.

    Cosmetic on the Gmail side — the draft threads by threadId whatever this
    says — but it is what the recipient reads, and a reply whose subject is
    bare is the tell that a machine wrote it. Not stacked: an already-prefixed
    subject is returned untouched, so a thread does not accumulate `Re: Re: Re:`
    over several rounds. The rest of the subject is preserved verbatim, which
    also keeps Gmail's own rule satisfied: a message posted to a threadId must
    carry that thread's subject, and Gmail normalizes the prefix when it checks.
    """
    subject = subject.strip()
    if not subject or _RE_PREFIX.match(subject):
        return subject
    return f"Re: {subject}"


def build_message(
    *,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
) -> EmailMessage:
    """Build the message. Shared with the draft path on purpose.

    A reviewer is offered "create a draft" and "send" for the same item. If the
    two produced different mail, the choice between them would silently be a
    choice about formatting, so both go through this one function.

    multipart/alternative: the markdown source stays the plain-text part, so a
    client that will not render HTML still gets something readable, and the HTML
    is generated from that same text so the two can never disagree.
    """
    message = EmailMessage()
    message["To"] = to
    message["From"] = sender() or ""
    message["Subject"] = _reply_subject(subject or "") if in_reply_to else (subject or "")
    if in_reply_to:
        # Both headers: References is what threads the conversation in most
        # clients, In-Reply-To is what marks the direct parent. The caller must
        # pass the parent's RFC Message-ID here, not a Gmail API message id —
        # the latter names nothing outside our own mailbox, so the recipient's
        # client finds no parent and starts a new conversation.
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message.set_content(body)
    message.add_alternative(
        markdown_email.to_html_document(body, title=subject or ""), subtype="html"
    )
    return message


def encode(message: EmailMessage) -> str:
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def send(
    *,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    thread_id: str | None = None,
) -> str:
    """Send one approved message as the human. Returns the sent message id."""
    if not to or "@" not in to:
        raise ValueError(f"refusing to send to {to!r}: not an address")

    payload = {"raw": encode(build_message(to=to, subject=subject, body=body, in_reply_to=in_reply_to))}
    if thread_id:
        payload["threadId"] = thread_id

    try:
        service = build_service()
        sent = service.users().messages().send(userId="me", body=payload).execute()
    except Exception as exc:
        integration_log.record(
            INTEGRATION_SOURCE, "messages.send", ok=False, capability="send", error=exc
        )
        raise
    integration_log.record(
        INTEGRATION_SOURCE, "messages.send", ok=True, capability="send"
    )
    return sent.get("id", "")
