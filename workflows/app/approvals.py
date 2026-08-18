"""Write items into the review queue.

The queue is a filesystem contract: one JSON file per item in `pending/`, and a
decision moves it to `approved/` or `rejected/`. This module only ever writes
`pending/`. It has no code path to `approved/` on purpose — an agent that could
write there would make the gate theater, since an injected instruction could
place its own item straight into the approved directory. Only the queue UI
backend, acting on a human keystroke, writes that.

The container backs this with a mount of just `pending/`, so the invariant holds
even if this module is wrong.

Approving now has consequences. A separate executor watches `approved/` and
performs the outcome — creating a Gmail draft, sending mail as alton@, applying
labels — so an item queued here is no longer inert until a human reads it. That
raises the stakes on this module rather than changing its rules: the executor
mounts `approved/` read-only and cannot approve anything, this module can only
write `pending/` and cannot execute anything, and the dashboard acting on a
keystroke is the only thing that moves an item between them.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Matches the queue UI's mount. `/approvals/pending` is the only writable path.
PENDING_DIR = Path(os.getenv("APPROVALS_PENDING_DIR", "/approvals/pending"))


def queue_available() -> bool:
    return PENDING_DIR.is_dir()


def _producer(agent: str | None, stage: str | None) -> dict[str, Any] | None:
    """Who made this item, as a structured field rather than free text.

    A reviewer deciding whether to trust an output needs to know what produced
    it and what that thing could reach. `evidence.source` cannot answer that: it
    is free text already used for unrelated values, so reading an agent name out
    of it would put a confident wrong attribution next to a decision about
    sending mail.

    `consumer` is the key the Integrations screen groups grants under, and it is
    the same string this pipeline passes to `integration_log.consumer_scope()`.
    Keeping them identical is what lets the review screen show the producer's
    access without a mapping table in between.
    """
    if not agent:
        return None
    return {
        "agent": agent,
        "stage": stage,
        "consumer": agent,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _envelope(review_type: str, channel: str | None = None, **fields: Any) -> dict[str, Any]:
    """Build the queue's item shape.

    `review_type` is what the reviewer renders and enforces against. `channel`
    is its predecessor and is still written for one release: a dashboard rolled
    back to the previous image reads `channel` and nothing else, and the items
    this writes will outlive the deploy that wrote them — they sit in the queue
    for days. New types should pass `channel=None` and let it be absent.

    `decision`/`decided_at`/`rejection_reason`/`edited_body` are initialised to
    null because the UI expects the keys present and writes them on decision.
    `producer` is present-but-null for an item whose caller did not name itself,
    which reads correctly in the UI as "unknown" — the honest answer for
    anything written before producers were recorded.

    Note what is deliberately NOT here: the list of actions a reviewer may take.
    That is derived by the reviewer from `review_type`. This module runs on
    attacker-reachable input, and an action list written here would be a
    capability this process granted itself — exactly what the pending-only mount
    exists to prevent. `suggested_action` is the most it may say, and the
    reviewer treats that as a hint it validates before honouring.
    """
    item_id = uuid.uuid4().hex[:8]
    item = {
        "id": item_id,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "review_type": review_type,
        "decision": None,
        "decided_at": None,
        "rejection_reason": None,
        "edited_body": None,
    }
    if channel is not None:
        item["channel"] = channel
    item.update(fields)
    return item


def review_item(
    review_type: str,
    *,
    title: str,
    summary: str,
    fields: list[dict[str, Any]] | None = None,
    body: str | None = None,
    evidence: dict[str, Any] | None = None,
    suggested_action: str | None = None,
    producer_agent: str | None = None,
    producer_stage: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A review item of any type, renderable with no frontend change.

    This is the constructor for everything that is not email or a Gmail filter —
    a memory fact, a CRM todo, a page or post awaiting publication. The reviewer
    has a generic template keyed off exactly these fields, so a new kind of thing
    becomes reviewable by calling this and nothing else:

        review_item(
            "memory_fact",
            title="Acme moved to Denver",
            summary="Stated on their about page, contradicts the graph.",
            fields=[{"label": "Source", "value": url},
                    {"label": "Confidence", "value": "0.62"}],
        )

    A hand-written template can come later, when the shape has earned one. What
    it must never do is block the item from reaching a human in the meantime.
    """
    return _envelope(
        review_type,
        producer=_producer(producer_agent, producer_stage),
        title=title,
        summary=summary,
        reason=summary,
        fields=fields or [],
        body=body,
        suggested_action=suggested_action,
        evidence=evidence or {},
        **extra,
    )


# The reviewer is a different user from the writer. `hermes-workflows` runs as
# root; the dashboard that renders this queue runs as uid 1000. An item only
# root can read is an item the human never sees.
ITEM_MODE = 0o644


def write_pending(item: dict[str, Any]) -> Path | None:
    """Atomically place one item in pending/. Returns its path, or None if no queue.

    Written to a temp file and renamed, so the UI never reads a partial item —
    the same contract the UI's own decision handler uses.

    The chmod is not cosmetic. `mkstemp` creates 0600 by design and `os.replace`
    preserves it, so every item this container wrote arrived in the queue as
    `root:root 0600` — present on disk, counted by the pipeline as queued for
    approval, and unreadable by the process that shows it to a human. A review
    gate whose items cannot be read is worse than no gate, because the pipeline
    reports the work as awaiting review and nothing ever awaits it.
    """
    if not queue_available():
        return None
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{item['created_at']}--{item['id']}.json"
    dest = PENDING_DIR / name
    fd, tmp = tempfile.mkstemp(dir=PENDING_DIR, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(item, fh, indent=2)
        # Before the rename, so the item is never visible at the wrong mode.
        os.chmod(tmp, ITEM_MODE)
        os.replace(tmp, dest)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return dest


def email_draft(
    *,
    body: str,
    reason: str,
    recipient_name: str | None = None,
    recipient_address: str | None = None,
    subject: str | None = None,
    source: str = "gmail_inbox_triage",
    conversation_notes: str = "",
    enrichment: str = "",
    scores: dict[str, int] | None = None,
    message_id: str | None = None,
    thread_id: str | None = None,
    rfc_message_id: str | None = None,
    producer_agent: str | None = None,
    producer_stage: str | None = None,
) -> dict[str, Any]:
    """An outbound reply awaiting approval — the queue's primary item type.

    `message_id`/`thread_id` name the message being replied to. They matter now
    that approving does something: without them a created draft is a new
    conversation rather than a reply, and a sent message carries no In-Reply-To,
    so it lands in the recipient's inbox detached from the thread they wrote in.
    Cold outreach has no such parent and leaves them None.

    `rfc_message_id` is the parent's Message-ID header and is NOT interchangeable
    with `message_id`: that one is a Gmail API handle, meaningless to any other
    mail system. Gmail threads our draft by `thread_id` and will do so whichever
    value the headers carry, which is exactly why this was wrong for a while
    without looking wrong — the recipient on Outlook is the one who sees the
    reply arrive detached.

    `body_format` is declared rather than guessed. The body is authored as
    markdown and rendered to multipart/alternative on the way out, and the two
    actions this item offers — create a draft, or send — have to produce the
    same mail as each other for offering both to be honest.
    """
    return _envelope(
        "email_draft",
        channel="email",
        producer=_producer(producer_agent, producer_stage),
        recipient={
            "name": recipient_name,
            "address": recipient_address,
            "org": None,
        },
        subject=subject,
        body=body,
        body_format="markdown",
        in_reply_to=(
            {
                "message_id": message_id,
                "thread_id": thread_id,
                "rfc_message_id": rfc_message_id,
            }
            if (message_id or thread_id)
            else None
        ),
        reason=reason,
        evidence={
            "source": source,
            "conversation_notes": conversation_notes,
            "enrichment": enrichment,
            "scores": scores or {},
        },
    )


def filter_proposal(
    *,
    name: str,
    rule: dict[str, Any],
    rationale: str,
    example_message_ids: list[str],
    producer_agent: str | None = None,
    producer_stage: str | None = None,
) -> dict[str, Any]:
    """A proposed Gmail filter awaiting approval.

    The rule travels as structured data and the reviewer renders it as a table.
    It used to be flattened into `body` as well, because `body` was the only
    thing the UI could show — but `body` is the editable field, so that put a
    textarea in front of a reviewer over text nothing parses back. Editing it
    changed nothing and said nothing about changing nothing. `body` is None here
    now; the rule is the item.

    What "approve" does to a filter is deliberately smaller than it sounds. This
    deployment's service account has gmail.modify but not gmail.settings.basic,
    so the available action files the example messages — visible, reversible,
    and covering mail already seen. Creating a standing filter needs a scope
    nobody has granted, and the reviewer shows that action greyed out with the
    reason rather than pretending.
    """
    return _envelope(
        "gmail_filter",
        channel="gmail_filter",
        producer=_producer(producer_agent, producer_stage),
        recipient={"name": None, "address": None, "org": None},
        subject=f"Proposed Gmail filter: {name}",
        title=f"Proposed Gmail filter: {name}",
        summary=rationale,
        body=None,
        reason=rationale,
        rule=rule,
        evidence={
            "source": "gmail_inbox_triage/propose_filters",
            "conversation_notes": f"Derived from {len(example_message_ids)} auto-filed messages.",
            "enrichment": "Example message ids: " + (", ".join(example_message_ids) or "none"),
            "scores": {},
        },
    )
