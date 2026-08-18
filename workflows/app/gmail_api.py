"""Deterministic Gmail access over the Gmail REST API.

Replaces the MCP path this pipeline started with. MCP is a protocol for handing
tools to a model; it is the wrong shape for a batch fetch, where nothing is being
decided. Two concrete costs it imposed: every message was a separate stdio
round-trip through a Node process, and the credential lived in a server designed
to be driven by an LLM. Here the fetch is one HTTP batch and the credential is
loaded by this module alone.

Credential, deliberately separate from the Hermes gateway's. Hermes holds a
human-scoped OAuth grant for interactive use; this runs unattended at much higher
volume and needs its own identity, its own quota, and narrower scopes. Two ways
to supply it, checked in this order:

  GMAIL_SERVICE_ACCOUNT_FILE + GMAIL_DELEGATED_USER
      A service account with domain-wide delegation, impersonating a mailbox in
      the Workspace domain. Preferred for anything scheduled: no consent flow, no
      refresh token to expire, and scopes are pinned by a Workspace admin rather
      than by whoever last ran the consent screen.

  GMAIL_TOKEN_FILE
      An authorized-user token (the file `scripts/gmail_auth.py` writes). Fine
      for a single mailbox and a laptop; it carries a refresh token, which is a
      long-lived secret sitting on disk.

Scope is gmail.modify: read plus label changes, no send and no delete. The API
itself refuses a send from this credential, which is the point — the label move
this pipeline makes automatically is reversible, and nothing else should be
reachable even if the calling code is wrong.
"""

from __future__ import annotations

import base64
import logging
import os
import random
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable, Iterator, Sequence

from app import integration_log

logger = logging.getLogger(__name__)

# Read and modify labels. NOT gmail.send, NOT full mail.google.com (which allows
# permanent delete). Widening this list is a security decision, not a config tweak.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

USER_ID = "me"

# Gmail's documented batch ceiling is 100; it recommends 50 and returns 429 more
# often above that. Each inner request still costs its own quota units — batching
# saves round-trips, not quota.
BATCH_SIZE = int(os.getenv("GMAIL_BATCH_SIZE", "50"))

MAX_RETRIES = int(os.getenv("GMAIL_MAX_RETRIES", "5"))

SERVICE_ACCOUNT_FILE = "GMAIL_SERVICE_ACCOUNT_FILE"
DELEGATED_USER = "GMAIL_DELEGATED_USER"
TOKEN_FILE = "GMAIL_TOKEN_FILE"


def configured() -> bool:
    """True when this deployment has given the pipeline its own credential."""
    if os.environ.get(SERVICE_ACCOUNT_FILE) and os.environ.get(DELEGATED_USER):
        return True
    return bool(os.environ.get(TOKEN_FILE))


def _credentials():
    sa_file = os.environ.get(SERVICE_ACCOUNT_FILE)
    subject = os.environ.get(DELEGATED_USER)
    if sa_file and subject:
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(
            sa_file, scopes=SCOPES
        ).with_subject(subject)

    token_file = os.environ.get(TOKEN_FILE)
    if token_file:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        if creds.expired and creds.refresh_token:
            # Refreshed in place; the file is rewritten by scripts/gmail_auth.py,
            # not here — this module never writes a credential to disk.
            creds.refresh(Request())
        return creds

    raise RuntimeError(
        f"no Gmail credential: set {SERVICE_ACCOUNT_FILE}+{DELEGATED_USER}, or {TOKEN_FILE}"
    )


def build_service():
    """An authorized Gmail API client. `cache_discovery=False` — no writable cache dir."""
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=_credentials(), cache_discovery=False)


# Which scope class each API method exercises. Read off the method name rather
# than passed in at every call site, so a new call cannot quietly land in the
# wrong bucket — and so the Integrations screen can show that this credential
# reads and relabels but never sends.
_CAPABILITIES = (
    ("batchModify", "modify"),
    ("modify", "modify"),
    ("labels.create", "modify"),
    # Writing a draft into the mailbox. Not a send — this credential cannot
    # send — but it is not a read either, and falling through to the default
    # would log mail composition as if it were a list call.
    ("drafts.create", "modify"),
    ("labels", "read"),
    ("messages.list", "read"),
    ("messages.get", "read"),
    ("threads.get", "read"),
)


def _capability(what: str) -> str:
    for needle, capability in _CAPABILITIES:
        if needle in what:
            return capability
    return "read"


def _retry(call: Callable[[], Any], what: str) -> Any:
    """Execute with backoff on the transient failures Gmail actually returns.

    429 (rate limit) and 5xx are retried; 4xx is not, because a bad query or a
    revoked scope will fail identically every time and retrying hides it.

    Every Gmail call in this module funnels through here, which makes it the one
    honest place to record whether the credential still works. The record is
    written once per logical call, after retries settle: three 429s followed by
    a success is one working call, and logging each attempt would show a healthy
    integration as intermittently broken.
    """
    from googleapiclient.errors import HttpError

    capability = _capability(what)

    def note(ok: bool, error: object = None) -> None:
        integration_log.record(
            source="gmail", operation=what, ok=ok, capability=capability, error=error
        )

    for attempt in range(MAX_RETRIES):
        try:
            result = call()
        except HttpError as exc:
            status = getattr(exc.resp, "status", 0)
            if status != 429 and status < 500:
                # A permanent failure — 401 on a revoked grant, 403 on a scope
                # that was narrowed. Exactly what the status screen exists to
                # surface, so it is recorded with its status code.
                note(False, f"HTTP {status}: {exc}")
                raise
            if attempt == MAX_RETRIES - 1:
                note(False, f"HTTP {status} after {MAX_RETRIES} attempts: {exc}")
                raise
            delay = min(2**attempt, 32) + random.uniform(0, 1)
            logger.warning("%s: HTTP %s, retrying in %.1fs", what, status, delay)
            time.sleep(delay)
        except Exception as exc:
            # Network, DNS, credential-load failure. Still an outbound call that
            # did not work, and still the answer to "can we reach Gmail".
            note(False, exc)
            raise
        else:
            note(True)
            return result
    raise RuntimeError("unreachable")


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def search_ids(service, query: str, limit: int) -> list[str]:
    """Message ids matching `query`, newest first, at most `limit`.

    Paginates because Gmail caps a page at 500 and returns fewer than asked for
    routinely — a single call is not a batch, it is the first page of one.
    """
    ids: list[str] = []
    page_token = None
    while len(ids) < limit:
        response = _retry(
            lambda: service.users()
            .messages()
            .list(
                userId=USER_ID,
                q=query,
                maxResults=min(500, limit - len(ids)),
                pageToken=page_token,
            )
            .execute(),
            "messages.list",
        )
        ids.extend(m["id"] for m in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return ids[:limit]


def _decode(data: str | None) -> str:
    if not data:
        return ""
    return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", errors="replace")


def _plain_text(payload: dict) -> str:
    """Best text/plain body from a MIME tree, falling back to text/html.

    Walks depth-first rather than assuming `parts[0]`: a real message is often
    multipart/alternative nested inside multipart/mixed, and the naive read
    returns an empty string or a base64 attachment.
    """
    if not payload:
        return ""
    stack = [payload]
    html: str = ""
    while stack:
        part = stack.pop(0)
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if mime == "text/plain" and body.get("data"):
            return _decode(body["data"])
        if mime == "text/html" and body.get("data") and not html:
            html = _decode(body["data"])
        stack.extend(part.get("parts") or [])
    return html


def _headers(payload: dict) -> dict[str, str]:
    return {h["name"].lower(): h.get("value", "") for h in (payload.get("headers") or [])}


def _parse_date_header(value: str | None) -> str | None:
    """RFC 5322 ``Date:`` -> UTC ISO-8601, or None if it can't be trusted.

    The header is attacker-controlled: it is whatever the sending client wrote.
    Anything unparseable degrades to None so callers simply omit the timestamp,
    rather than showing a model a malformed date or raising mid-pipeline.
    """
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    # A `Date:` with no offset is naive; read it as UTC, the only assumption
    # that doesn't invent a timezone for the sender.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _normalize(raw: dict) -> dict[str, Any]:
    """One API message -> the flat shape the pipeline passes downstream.

    Everything in the return value except `id` and `label_ids` is UNTRUSTED.
    """
    payload = raw.get("payload") or {}
    headers = _headers(payload)
    return {
        "id": raw.get("id"),
        "thread_id": raw.get("threadId"),
        # Gmail's mailbox timestamp in milliseconds since epoch. Unlike the
        # sender-controlled Date header, this is reliable for ordering the inbox
        # work queue.
        "internal_date": int(raw["internalDate"]) if raw.get("internalDate") else None,
        # The RFC 5322 header, distinct from `id` above. `id` is an API handle
        # that means nothing outside this mailbox; this is what a reply's
        # In-Reply-To/References must name for any other mail client to thread
        # it. Pulling it here is the only place it is available — the pipeline
        # downstream sees this dict, never the raw payload.
        "rfc_message_id": headers.get("message-id"),
        "sender": headers.get("from"),
        "to": headers.get("to"),
        "subject": headers.get("subject"),
        "date": headers.get("date"),
        # The raw `date` above is whatever offset the sender wrote, in RFC 5322
        # form. Parsing it once here — rather than at each render site — means
        # the pipeline, the evals and the playground all agree, and downstream
        # code never has to reason about sender-supplied offsets.
        "received_at": _parse_date_header(headers.get("date")),
        "snippet": raw.get("snippet"),
        # Bulk-mail markers. RFC 2369 requires List-Unsubscribe on list traffic,
        # which makes it the one reliable way to tell a newsletter from a person
        # writing to you — the From address of a Substack digest looks exactly
        # like a correspondent's. Memory reads this to decide whether a sender
        # is someone we know or a publication we receive.
        "list_unsubscribe": headers.get("list-unsubscribe"),
        "list_id": headers.get("list-id"),
        "precedence": headers.get("precedence"),
        "body": _plain_text(payload),
        "label_ids": raw.get("labelIds") or [],
    }


def fetch_messages(service, ids: Sequence[str]) -> tuple[list[dict], dict[str, str]]:
    """Fetch many messages in batched HTTP requests.

    This is the part that makes it a batch: one HTTP request carries up to
    BATCH_SIZE `messages.get` calls, instead of one round-trip per message. For a
    25-message triage run that is 1 request rather than 25.

    Returns (messages in the order given, {id: error}). A message that fails is
    reported, never silently dropped — a batch that quietly returns 24 of 25 is
    indistinguishable from a quiet inbox.
    """
    fetched: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def callback(request_id: str, response: dict, exception: Exception | None) -> None:
        if exception is not None:
            errors[request_id] = str(exception)
        else:
            fetched[request_id] = response

    for chunk in _chunks(list(ids), BATCH_SIZE):
        def run(chunk: Sequence[str] = chunk) -> None:
            batch = service.new_batch_http_request()
            for mid in chunk:
                batch.add(
                    service.users().messages().get(userId=USER_ID, id=mid, format="full"),
                    request_id=mid,
                    callback=callback,
                )
            batch.execute()

        _retry(run, f"messages.get batch of {len(chunk)}")

    messages = [_normalize(fetched[mid]) for mid in ids if mid in fetched]
    return messages, errors


def get_thread(service, thread_id: str) -> dict[str, Any]:
    """One thread, with its messages, for deciding whether we already replied.

    Gmail has no `is:unanswered` operator, so "awaiting a reply" has to be read
    off the thread: if the newest message carries the SENT label, we answered.
    `format="metadata"` keeps this cheap — headers and labels are all the caller
    needs, and not pulling bodies keeps a thread of forty messages affordable.
    """
    return _retry(
        lambda: service.users()
        .threads()
        .get(
            userId=USER_ID,
            id=thread_id,
            format="metadata",
            metadataHeaders=["From", "Date"],
        )
        .execute(),
        "threads.get",
    )


def label_map(service) -> dict[str, str]:
    """Label name -> id, so callers resolve 'Check Later' at runtime."""
    response = _retry(
        lambda: service.users().labels().list(userId=USER_ID).execute(), "labels.list"
    )
    return {l["name"]: l["id"] for l in response.get("labels", []) if l.get("name")}


def ensure_label(service, name: str) -> str | None:
    """The id of `name`, creating the label if this mailbox does not have it.

    Triage depends on being able to mark a message handled. If the label is
    missing, `label_map` returns nothing for it, no label is applied, and the
    message stays unread and in the inbox — so the next run triages it again,
    drafts another reply, and queues another approval, indefinitely. Creating it
    is the difference between an idempotent pipeline and a duplicate factory.

    Returns None if creation fails, so the caller can report a real problem
    rather than proceeding as if the message had been marked.
    """
    existing = label_map(service).get(name)
    if existing:
        return existing
    try:
        created = _retry(
            lambda: service.users()
            .labels()
            .create(
                userId=USER_ID,
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute(),
            "labels.create",
        )
    except Exception:  # noqa: BLE001 — reported by the caller, not fatal here
        logger.exception("could not create label %r", name)
        return None
    return created.get("id")


def create_draft(service, raw: str, thread_id: str | None = None) -> dict[str, Any]:
    """Place a composed message in this mailbox's Drafts. Returns {id, thread_id}.

    This is the safe half of what an approved email item can become: the message
    lands in Drafts under the human's own account, where they send it — or do
    not — with the normal Gmail controls. Nothing leaves the building.

    It is available on gmail.modify, which this credential already holds, and
    that is the whole reason "approve and create a draft" works here while
    "approve and send" needs a different credential entirely. Google refuses
    messages.send on this scope; drafts.create it allows, because a draft is
    reversible and a send is not.

    `thread_id` threads the draft under the message being replied to. Gmail
    requires the message's References/In-Reply-To headers to agree with it, so
    the caller builds the message with both and passes the id here.

    Retried, unlike a send: a duplicate draft is a nuisance someone deletes.
    """
    body: dict[str, Any] = {"message": {"raw": raw}}
    if thread_id:
        body["message"]["threadId"] = thread_id
    # No integration_log call here: _retry writes the record for every call in
    # this module, once per logical call after retries settle.
    created = _retry(
        lambda: service.users().drafts().create(userId=USER_ID, body=body).execute(),
        "drafts.create",
    )
    return {
        "draft_id": created.get("id"),
        "thread_id": (created.get("message") or {}).get("threadId"),
        "message_id": (created.get("message") or {}).get("id"),
    }


def batch_modify(
    service, ids: Sequence[str], add: Iterable[str] = (), remove: Iterable[str] = ()
) -> None:
    """Apply label changes to many messages in one call per chunk.

    `messages.batchModify` is a single endpoint taking up to 1000 ids — the right
    primitive for "file these twelve", rather than twelve modify calls. It has no
    per-message result: it succeeds or raises for the whole chunk, so a caller
    that needs to know which message failed must fall back to singles.

    There is no delete here, and no path to one. The most destructive thing this
    module can do is move a message out of the inbox.
    """
    body = {"ids": [], "addLabelIds": list(add), "removeLabelIds": list(remove)}
    for chunk in _chunks(list(ids), 1000):
        payload = {**body, "ids": list(chunk)}
        _retry(
            lambda payload=payload: service.users()
            .messages()
            .batchModify(userId=USER_ID, body=payload)
            .execute(),
            f"messages.batchModify of {len(chunk)}",
        )
