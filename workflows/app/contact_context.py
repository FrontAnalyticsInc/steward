"""What we know about a person, and how to go and find out more.

The shared home for answering "who is this?" before deciding what to do about
them. Inbox triage uses it; the calendar briefing still carries its own copy of
these helpers and should be migrated onto this module — that consolidation was
deliberately not done in the same change as the triage rewrite, to avoid
destabilising a pipeline that is verified and running.

The hard parts below were all learned from real failures, and a second copy will
only ever inherit the first version of them:

  * a semantic search that returns the nearest facts whether or not they concern
    the person asked for, so recall needs a precision gate bolted on;
  * bodies rather than snippets, because a snippet stops before the point;
  * quoted threads and calendar automation crowded out everything a human wrote.

Nothing here writes to the mailbox or the CRM. It reads, and it hands
observations to the graph.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from app import attio_api, fact_distill, gmail_api, memory

logger = logging.getLogger(__name__)

# How old what the graph knows may be before we go and look again. Not zero:
# re-reading everyone on every run would spend the whole budget on people whose
# situation has not changed since yesterday.
MEMORY_FRESH_DAYS = int(os.getenv("CONTACT_MEMORY_FRESH_DAYS", "30"))

MAX_MAIL_PER_CONTACT = int(os.getenv("CONTACT_MAIL_PER_PERSON", "5"))
MAIL_LOOKBACK_DAYS = int(os.getenv("CONTACT_MAIL_LOOKBACK_DAYS", "180"))

# Below this, after quoting is stripped, a message said nothing worth
# remembering — "Yeah definitely! Just grabbed that time :)" is 40 characters.
MIN_SUBSTANCE_CHARS = int(os.getenv("CONTACT_MIN_SUBSTANCE", "160"))

# Enough to carry an explanation. The message that prompted this held its point
# about 800 characters in, past a greeting and a paragraph of preamble.
MAX_SUBSTANCE_CHARS = int(os.getenv("CONTACT_MAX_SUBSTANCE", "1800"))

# Subjects Google generates for calendar activity. Logistics, not correspondence.
_SCHEDULING_PREFIXES = (
    "appointment booked:", "invitation:", "updated invitation:", "accepted:",
    "declined:", "tentative:", "canceled:", "cancelled:", "notification:",
)

_SCHEDULING_SENDERS = ("calendar-notification@google.com", "calendar-server@google.com")

# How many recent subjects a summary line names before it stops being a summary.
MAX_SUBJECTS_IN_FACT = 3

# Bulk mail markers, in header precedence order. RFC 2369 requires
# List-Unsubscribe on list traffic, which is the only reliable separator between
# a publication and a person: a Substack digest's From address is
# indistinguishable from a correspondent's, and treating one as the other is how
# a newsletter came to contribute forty "facts" about its sender.
_BULK_HEADERS = ("list_unsubscribe", "list_id")
_BULK_PRECEDENCE = ("bulk", "list", "junk")


# `identity_tokens` and `relevant_facts` lived here. They filtered the graph's
# answer with substring matching, because Graphiti returned its nearest facts
# whether or not any concerned the person asked for — recall from the search,
# precision from a gate bolted on afterwards.
#
# The wiki keys each document on the address, so `memory.search_facts` returns
# that person's own file directly, and FTS5 refuses to match anything sharing no
# term. Precision is a property of the store now.
#
# One behaviour deliberately did not survive: a token was also matched against
# whitespace-stripped text so the domain `northwindstrategies` would find the
# prose "Northwind Strategies", letting a fact about the ORGANISATION be returned
# for a query about a PERSON. Organisations have their own documents here,
# reached by the `[[wikilink]]` in the person's file, so a fact about the
# employer is one hop away rather than blurred into the employee's history.


def fact_age_days(fact: dict[str, Any], now: datetime) -> Optional[float]:
    """How long ago the graph learned this, or None if it will not say."""
    for key in ("valid_at", "created_at"):
        raw = fact.get(key)
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (now - when).total_seconds() / 86400.0
    return None


def freshest_age(facts: list[dict[str, Any]], now: datetime) -> Optional[float]:
    ages = [a for a in (fact_age_days(f, now) for f in facts) if a is not None]
    return min(ages) if ages else None


def is_scheduling(subject: str, sender: str) -> bool:
    """Is this message logistics rather than something a person said?"""
    subject = (subject or "").strip().lower()
    sender = (sender or "").lower()
    if any(s in sender for s in _SCHEDULING_SENDERS):
        return True
    return any(subject.startswith(p) for p in _SCHEDULING_PREFIXES)


def is_bulk(message: dict[str, Any]) -> bool:
    """Is this list traffic rather than someone writing to us?

    Checked before substance, not after: a newsletter passes every substance
    test there is. It is long, it is not scheduling, and it is not quoted
    thread — it is simply not correspondence, and no amount of it tells us
    anything about the sender beyond that they publish.
    """
    for header in _BULK_HEADERS:
        if (message.get(header) or "").strip():
            return True
    return (message.get("precedence") or "").strip().lower() in _BULK_PRECEDENCE


def strip_quoted(body: str) -> str:
    """What this person wrote THIS time, without the thread underneath.

    A reply carries the whole conversation below it, so storing the raw body
    refiles the same sentences on every message in a thread and buries the new
    part.
    """
    source = (body or "").splitlines()
    lines: list[str] = []
    for index, raw in enumerate(source):
        line = raw.rstrip()
        stripped = line.lstrip()
        low = stripped.lower()
        if stripped.startswith(">"):
            continue
        if low.startswith("-----original message-----"):
            break
        # "On Thu, Jul 30, 2026 at 9:29 AM Someone <x@y.com> wrote:" — which
        # Gmail WRAPS, so "wrote:" routinely lands on the following line and
        # neither half matches alone. Look ahead one line before deciding.
        if low.startswith("on "):
            following = source[index + 1].strip().lower() if index + 1 < len(source) else ""
            if low.endswith("wrote:") or following.endswith("wrote:"):
                break
        lines.append(line)
    return "\n".join(lines).strip()


def mail_context(
    service, address: str, limit: Optional[int] = None
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Recent mail with one person, split into (correspondence, publications).

    Reads BODIES, not snippets: a snippet is ~130 characters and the message
    that exposed this put its point — a cloud computing cooperative the sender
    had been considering for years — well past that. Skips calendar automation,
    which otherwise consumes the fetch budget and yields facts about meeting
    links rather than about people.

    The split is the second thing this learned the hard way. List traffic used
    to come back as correspondence, so a Substack digest was stored as "what
    this person has written recently, in their own words" and its whole body
    became facts about them. A publication is worth exactly one fact — that they
    publish it — so its subjects are returned separately and its bodies are
    dropped here rather than carried and filtered later.
    """
    keep = MAX_MAIL_PER_CONTACT if limit is None else limit
    query = f"(from:{address} OR to:{address}) newer_than:{MAIL_LOOKBACK_DAYS}d"
    # Wider than the keep limit: filtered-out logistics would otherwise use the
    # whole budget and leave no room for the real message.
    ids = gmail_api.search_ids(service, query, keep * 3)
    if not ids:
        return [], []
    fetched, _errors = gmail_api.fetch_messages(service, ids)

    out: list[dict[str, str]] = []
    bulk: list[dict[str, str]] = []
    for m in fetched:
        subject = (m.get("subject") or "(no subject)")[:200]
        sender = (m.get("sender") or "")[:200]
        when = str(m.get("received_at") or "")[:10]
        if is_scheduling(subject, sender):
            continue
        if is_bulk(m):
            if len(bulk) < keep:
                bulk.append({"subject": subject, "sender": sender, "date": when})
            continue
        substance = strip_quoted(m.get("body") or "")
        if len(substance) < MIN_SUBSTANCE_CHARS:
            continue
        out.append(
            {
                "subject": subject,
                "sender": sender,
                "date": when,
                "snippet": substance[:MAX_SUBSTANCE_CHARS],
            }
        )
        if len(out) >= keep:
            break
    return out, bulk


_TRANSIENT_CRM_FIELDS = ("web_url", "record_id", "last_interaction", "next_interaction")


def observations(
    address: str,
    mail: list[dict[str, str]],
    crm: Optional[list[dict[str, str]]] = None,
    bulk: Optional[list[dict[str, str]]] = None,
) -> str:
    """The raw material a distillation reads. NOT what gets stored.

    This used to be `episode_body`, and the rename is the fix. Graphiti ran an
    LLM over this text and stored what it concluded; the docstring said so —
    "the graph does its own extraction". When the graph was deleted the text
    started going to `memory.add_episode`, which splits on newlines, so these
    raw bodies became the stored facts themselves. `app/fact_distill.py` is the
    extractor put back; this function's only job is to feed it.

    Excluded on purpose: last_interaction / next_interaction (calendar one-offs,
    true today and noise next week), web_url / record_id (identifiers for our
    tooling, which produced facts about our CRM rather than about a person), and
    tasks, which are never passed in because they turn over constantly.
    """
    lines: list[str] = []
    for record in crm or []:
        durable = {
            k: v for k, v in record.items() if k not in _TRANSIENT_CRM_FIELDS
        }
        fields = ", ".join(f"{k}: {v}" for k, v in durable.items())
        if fields:
            lines.append(f"- CRM record for {address} -> {fields}")
    if mail:
        lines.append(f"What {address} has written recently, in their own words:")
        for item in mail:
            lines.append(f"- Subject: {item['subject']}")
            lines.append(f"  From: {item['sender']}")
            lines.append(f"  {item['snippet']}")
    if bulk:
        subjects = "; ".join(item["subject"] for item in bulk[:MAX_SUBJECTS_IN_FACT])
        lines.append(
            f"{address} also sends bulk/list mail. Recent issue subjects: {subjects}. "
            "Their bodies are deliberately not included — a publication tells us "
            "that the sender publishes, and nothing else about them."
        )
    if not lines:
        return ""
    return "\n".join(lines)


def _fact(text: str, links: Optional[list[str]] = None) -> str:
    """One bounded, linked wiki line."""
    text = " ".join(str(text or "").split())
    if len(text) > fact_distill.MAX_FACT_CHARS:
        text = text[: fact_distill.MAX_FACT_CHARS].rsplit(" ", 1)[0].rstrip(",;: ") + "…"
    return fact_distill.link_terms(text, links or [])


def deterministic_facts(
    address: str,
    mail: list[dict[str, str]],
    crm: Optional[list[dict[str, str]]] = None,
    bulk: Optional[list[dict[str, str]]] = None,
) -> list[str]:
    """A short, linked record built without a model.

    Not a placeholder. `WORKFLOWS_MODEL_PROVIDER=anthropic` with no
    `ANTHROPIC_API_KEY` is a real deployment state, and the wrong response to it
    is to store the raw material instead — that is precisely the bug being
    fixed. So this path is allowed to know less, and never allowed to say more:
    every line here is assembled from structured CRM fields and message
    metadata, and no message body reaches the store through it.

    The employer is linked because the CRM gives us its name exactly, which
    makes it the one edge that can be drawn with no inference at all.
    """
    facts: list[str] = []
    record = (crm or [{}])[0]
    name = (record.get("name") or "").strip()
    title = (record.get("job_title") or "").strip()
    company = (record.get("company") or "").strip()
    subject_name = name or address

    if title and company:
        facts.append(_fact(f"{subject_name} is {title} at {company}.", [company]))
    elif company:
        facts.append(_fact(f"{subject_name} works at {company}.", [company]))
    elif title:
        facts.append(_fact(f"{subject_name} is {title}."))
    elif name:
        facts.append(_fact(f"{address} is {name}."))

    location = (record.get("primary_location") or "").strip()
    if location:
        facts.append(_fact(f"{subject_name} is based in {location}."))

    # The CRM's own description is already a human-written summary, which makes
    # it the highest-density line available. Kept verbatim, bounded.
    description = (record.get("description") or "").strip()
    if description:
        facts.append(_fact(description))

    linkedin = (record.get("linkedin") or "").strip()
    if linkedin:
        facts.append(_fact(f"{subject_name} on LinkedIn: {linkedin}"))

    if mail:
        dates = sorted(item.get("date", "") for item in mail if item.get("date"))
        span = f" between {dates[0]} and {dates[-1]}" if len(dates) > 1 else (
            f" on {dates[0]}" if dates else ""
        )
        subjects = "; ".join(
            f'"{item["subject"][:60]}"' for item in mail[:MAX_SUBJECTS_IN_FACT]
        )
        facts.append(
            _fact(
                f"Exchanged {len(mail)} substantive message(s) with us{span}. "
                f"Recent subjects: {subjects}."
            )
        )

    if bulk:
        dates = sorted(item.get("date", "") for item in bulk if item.get("date"))
        span = f", {dates[0]} to {dates[-1]}" if len(dates) > 1 else ""
        facts.append(
            _fact(
                f"Sends list mail rather than correspondence: {len(bulk)} issue(s) "
                f"received{span}. Message bodies are not recorded."
            )
        )

    return facts


def record_observations(
    address: str,
    mail: list[dict[str, str]],
    crm: Optional[list[dict[str, str]]] = None,
    bulk: Optional[list[dict[str, str]]] = None,
    *,
    source_description: str,
) -> dict[str, Any]:
    """Distil what we just read about someone and append it to their document.

    The single write path for contact memory, shared by inbox triage and the
    calendar briefing. Both used to build their own body text and hand it
    straight to storage, which is how the same defect existed twice.

    Order matters: the model is asked first, and the deterministic record is the
    fallback rather than an addition. Writing both would file the same employer
    twice in different words, and a store that says a thing two ways is one a
    reader has to reconcile.
    """
    raw = observations(address, mail, crm, bulk)
    if not raw:
        return {"written": 0, "distilled": False}

    distillation = fact_distill.distill(address, raw)
    if distillation is not None:
        lines = fact_distill.to_lines(distillation)
        # An empty distillation is a verdict, not a failure: the model read a
        # newsletter and found nothing durable. Falling back to deterministic
        # facts here would overrule it with a line it already rejected.
        written = memory.add_facts(address, lines, source_description) if lines else 0
        return {"written": written, "distilled": True, **fact_distill.summarise(distillation)}

    lines = deterministic_facts(address, mail, crm, bulk)
    written = memory.add_facts(address, lines, f"{source_description} (no model)")
    return {"written": written, "distilled": False, "facts": len(lines)}


def render_contact(profile: dict[str, Any]) -> str:
    """One person's context, fenced as data for a model to read."""
    lines = [f"contact: {profile['address']}"]
    age = profile.get("memory_age_days")
    lines.append("<known_facts>")
    if profile.get("facts"):
        lines.append(f"(known for {age if age is not None else 'unknown'} days)")
        lines.extend(f"- {f}" for f in profile["facts"])
    else:
        lines.append("(nothing on record)")
    lines.append("</known_facts>")
    if profile.get("crm"):
        lines.append("<crm_record>")
        for record in profile["crm"]:
            for k, v in record.items():
                if k == "record_id":
                    continue
                lines.append(f"- {k}: {v}")
        lines.append("</crm_record>")
    if profile.get("tasks"):
        lines.append("<open_crm_tasks>")
        for task in profile["tasks"]:
            due = f" | due {task['deadline']}" if task.get("deadline") else ""
            lines.append(f"- {task.get('content', '')}{due}")
        lines.append("</open_crm_tasks>")
    if profile.get("recent_mail"):
        lines.append("<recent_mail>")
        for m in profile["recent_mail"]:
            lines.append(f"- {m['subject']} | from {m['sender']}")
            if m.get("snippet"):
                lines.append(f"  {m['snippet']}")
        lines.append("</recent_mail>")
    return "\n".join(lines)


def enrich(
    address: str,
    gmail_service=None,
    now: Optional[datetime] = None,
    include_tasks: bool = True,
) -> tuple[dict[str, Any], list[str], bool]:
    """Everything known about one person, refreshing the graph if it is stale.

    Returns (profile, problems, submitted). Best-effort throughout: a caller
    with thin context can still do its job, and a run that failed because the
    graph was briefly down would be a worse outcome than a thinner answer. Every
    failure is collected in `problems` rather than raised.

    `submitted` means an observation was handed to Graphiti — NOT that the graph
    now knows it. Ingestion is asynchronous and a 202 is an acceptance.
    """
    now = now or datetime.now(timezone.utc)
    problems: list[str] = []
    crm: list[dict[str, str]] = []
    tasks: list[dict[str, str]] = []
    mail: list[dict[str, str]] = []
    submitted = False

    facts: list[dict[str, Any]] = []
    try:
        facts = memory.search_facts(address)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"memory lookup failed for {address}: {type(exc).__name__}")

    # The CRM is read every time rather than only when memory is stale: open
    # tasks are the part that changes between one run and the next, and a
    # 30-day-fresh memory says nothing about today's outstanding work.
    if attio_api.configured():
        try:
            crm = attio_api.find_person(address)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"crm lookup failed for {address}: {type(exc).__name__}")
        if include_tasks:
            for record in crm:
                record_id = record.get("record_id")
                if not record_id:
                    continue
                try:
                    tasks.extend(attio_api.open_tasks(record_id))
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"task lookup failed for {address}: {type(exc).__name__}")

    age = freshest_age(facts, now)
    stale = not facts or age is None or age > MEMORY_FRESH_DAYS
    bulk: list[dict[str, str]] = []
    if stale:
        try:
            service = gmail_service or gmail_api.build_service()
            mail, bulk = mail_context(service, address)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"mail lookup failed for {address}: {type(exc).__name__}")
        if mail or bulk or crm:
            try:
                result = record_observations(
                    address,
                    mail,
                    crm,
                    bulk,
                    source_description="contact refresh from Attio and Gmail",
                )
                submitted = bool(result.get("written"))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"memory refresh failed for {address}: {type(exc).__name__}")

    profile = {
        "address": address,
        "facts": [str(f.get("fact") or "")[:300] for f in facts[:6] if f.get("fact")],
        "memory_age_days": None if age is None else round(age, 1),
        "crm": crm,
        "tasks": tasks,
        "recent_mail": mail,
    }
    return profile, problems, submitted
