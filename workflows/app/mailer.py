"""Outbound mail sent as the assistant. The only module here that can send.

Separate from `gmail_api` on purpose, and the separation is the security control:

  `gmail_api` holds gmail.modify. It reads and relabels Alton's mailbox and the
  API itself refuses a send from that credential — which is why triage can run
  unattended over hostile input. Adding a send function there would widen that
  grant for every caller and delete the property.

  This module holds gmail.send, as **evan@** — the assistant's own identity. Mail
  it produces is visibly from the assistant, never from Alton, so nothing here
  can put words in his mouth. Anything sent *as Alton* still goes through the
  review queue — and what comes out the far side of that queue is
  `mailer_as_principal`, a separate module with its own subject and its own
  audit-log source. Not a flag on this one: the single-identity property above
  is what this module exists for, and a subject parameter would delete it for
  every unattended caller in exchange for saving a file.

Scope is gmail.send alone: it can send and cannot read, so a compromised call
site cannot use this credential to exfiltrate a mailbox.

The recipient allowlist is the other half. An unattended sender reachable from a
pipeline that reads attacker-writable calendar invites must not be able to mail
an arbitrary address, so the destination is pinned by configuration and a send to
anything else is refused here rather than attempted.
"""

from __future__ import annotations

import base64
import logging
import os
from email.message import EmailMessage
from typing import Optional

from app import integration_log, markdown_email

logger = logging.getLogger(__name__)

# Send only. NOT gmail.modify, NOT full mail.google.com. This credential cannot
# read a message, which is the point.
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# Named for the dashboard's module scan. Distinct from `gmail` so the Integrations
# screen shows sending as its own source with its own credential, rather than
# folding it into the mailbox triage reads.
INTEGRATION_SOURCE = "gmail_send"
INTEGRATION_LABEL = "Outbound mail (assistant)"

SERVICE_ACCOUNT_FILE = "MAILER_SERVICE_ACCOUNT_FILE"
SENDER = "MAILER_SENDER"
ALLOWED_RECIPIENTS = "MAILER_ALLOWED_RECIPIENTS"


def configured() -> bool:
    """True when this deployment has given the assistant a sending identity."""
    return bool(os.environ.get(SERVICE_ACCOUNT_FILE) and os.environ.get(SENDER))


def sender() -> Optional[str]:
    return os.environ.get(SENDER)


def allowed_recipients() -> list[str]:
    """Addresses this credential may send to. Empty means none, not all.

    Failing closed matters more than convenience here: an empty or missing
    allowlist on an unattended sender should stop the send, not authorize every
    address on the internet.
    """
    raw = os.environ.get(ALLOWED_RECIPIENTS) or ""
    return [addr.strip().lower() for addr in raw.split(",") if addr.strip()]


def _credentials():
    sa_file = os.environ.get(SERVICE_ACCOUNT_FILE)
    subject = os.environ.get(SENDER)
    if not (sa_file and subject):
        raise RuntimeError(f"no mailer credential: set {SERVICE_ACCOUNT_FILE}+{SENDER}")
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_file(
        sa_file, scopes=SCOPES
    ).with_subject(subject)


def build_service():
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=_credentials(), cache_discovery=False)


def send(to: str, subject: str, body: str) -> str:
    """Send one plain-text message. Returns the sent message id.

    Refuses a recipient outside the allowlist before building the message, so a
    wrong address is a caught error rather than delivered mail. No retry: a
    duplicated briefing is worse than a missing one, and the daily job will try
    again tomorrow.
    """
    allowed = allowed_recipients()
    if to.strip().lower() not in allowed:
        raise ValueError(
            f"recipient {to!r} is not in {ALLOWED_RECIPIENTS}; refusing to send"
        )

    message = EmailMessage()
    message["To"] = to
    message["From"] = sender() or ""
    message["Subject"] = subject
    # multipart/alternative: the markdown source stays the plain-text part, so a
    # client that will not render HTML still gets something readable rather than
    # an empty message. The HTML part is generated from that same text, so the
    # two can never describe different days.
    message.set_content(body)
    message.add_alternative(
        markdown_email.to_html_document(body, title=subject), subtype="html"
    )
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    try:
        service = build_service()
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as exc:
        integration_log.record(
            INTEGRATION_SOURCE, "messages.send", ok=False, capability="send", error=exc
        )
        raise
    integration_log.record(
        INTEGRATION_SOURCE, "messages.send", ok=True, capability="send"
    )
    return sent.get("id", "")
