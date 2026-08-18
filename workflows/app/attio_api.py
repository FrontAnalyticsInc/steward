"""Read-only Attio lookups for the workflow pipelines.

Its OWN credential, deliberately separate from the Attio MCP server the gateway
agent uses — the same split as `gmail_api` versus the gateway's gmail-mcp. That
grant is interactive and human-scoped; this one is unattended, higher-volume, and
read-only, and sharing them would leave one identity in the audit log for two
very different actors.

A bare token rather than a credential file, so it lives in `docker/.env` as
ATTIO_API_KEY rather than in the mounted /secrets directory.

Read-only is enforced at the token, not here. This module only ever issues the
record *query* endpoint, but the guarantee that a bug cannot write to the CRM
comes from the key being issued with `record_permission:read` — code can be
wrong, a scope cannot.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Optional

from app import integration_log

logger = logging.getLogger(__name__)

INTEGRATION_SOURCE = "attio"
INTEGRATION_LABEL = "Attio CRM"

API_KEY = "ATTIO_API_KEY"
BASE_URL = os.getenv("ATTIO_BASE_URL", "https://api.attio.com")
TIMEOUT = float(os.getenv("ATTIO_TIMEOUT", "15"))

# One record is what a briefing needs — "who is this person" has one answer.
# A larger page would mostly be a way to accidentally page the whole CRM.
MAX_RECORDS = int(os.getenv("ATTIO_MAX_RECORDS", "3"))

# Values worth carrying into a briefing. Attio records can hold dozens of
# attributes and most are workspace-specific; these are the ones that describe
# who someone is rather than how this workspace files them.
_INTERESTING = (
    "name",
    "job_title",
    "company",
    "description",
    "primary_location",
    "linkedin",
    # The interaction history is often the only populated part of a record, and
    # it is the most briefing-relevant part when it is: "last emailed today,
    # next meeting Monday" beats an empty job title.
    "last_interaction",
    "next_interaction",
)


def configured() -> bool:
    """True when this deployment has given the pipelines a CRM token."""
    return bool(os.environ.get(API_KEY))


def _post(path: str, payload: dict, what: str) -> Any:
    token = os.environ.get(API_KEY)
    if not token:
        raise RuntimeError(f"no Attio credential: set {API_KEY}")
    req = urllib.request.Request(
        f"{BASE_URL.rstrip('/')}{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8") or "{}"
        result = json.loads(body)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        integration_log.record(
            INTEGRATION_SOURCE, what, ok=False, capability="read", error=exc
        )
        raise
    integration_log.record(INTEGRATION_SOURCE, what, ok=True, capability="read")
    return result


def _first_value(values: Any) -> Optional[str]:
    """Attio attributes are arrays of time-versioned values; take the current one.

    Each entry carries active_from/active_until, so an attribute that changed
    keeps its history. A briefing wants what is true now, which is the entry
    with no active_until — falling back to the first if none says.
    """
    if isinstance(values, str):
        return values
    if not isinstance(values, list) or not values:
        return None
    current = next((v for v in values if isinstance(v, dict) and not v.get("active_until")), None)
    entry = current or values[0]
    if not isinstance(entry, dict):
        return str(entry)
    # An interaction is a type plus a date, and neither alone is worth reading.
    if entry.get("interacted_at"):
        when = str(entry["interacted_at"])[:10]
        kind = entry.get("interaction_type") or "interaction"
        return f"{kind} on {when}"

    # NOT target_record_id. A record reference resolves to a UUID, and a UUID in
    # a briefing is noise a model may try to read meaning into — references are
    # resolved by name in `summarize_person` or dropped.
    for key in (
        "value",
        "full_name",
        "option",
        "email_address",
        "status",
    ):
        found = entry.get(key)
        if isinstance(found, dict):
            found = found.get("title") or found.get("name") or found.get("value")
        if isinstance(found, str) and found.strip():
            return found.strip()
    return None


def _get(path: str, what: str) -> Any:
    token = os.environ.get(API_KEY)
    if not token:
        raise RuntimeError(f"no Attio credential: set {API_KEY}")
    req = urllib.request.Request(
        f"{BASE_URL.rstrip('/')}{path}",
        method="GET",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        integration_log.record(
            INTEGRATION_SOURCE, what, ok=False, capability="read", error=exc
        )
        raise
    integration_log.record(INTEGRATION_SOURCE, what, ok=True, capability="read")
    return result


# Resolved references, for the life of the process. A morning's attendees share
# employers more often than not, and the name of a company does not change
# between two lookups a second apart.
_REFERENCE_NAMES: dict[str, str] = {}


def _reference_name(value: Any) -> Optional[str]:
    """Turn a record reference into the referenced record's name.

    Returns None rather than the UUID when it cannot be resolved: an
    unresolvable reference is better absent than present as an opaque
    identifier, which reads to a model as though it means something.
    """
    if not isinstance(value, list) or not value:
        return None
    entry = next((v for v in value if isinstance(v, dict) and not v.get("active_until")), None)
    entry = entry or (value[0] if isinstance(value[0], dict) else None)
    if not entry or entry.get("attribute_type") != "record-reference":
        return None
    target_object = entry.get("target_object")
    record_id = entry.get("target_record_id")
    if not (target_object and record_id):
        return None
    cache_key = f"{target_object}/{record_id}"
    if cache_key in _REFERENCE_NAMES:
        return _REFERENCE_NAMES[cache_key] or None
    try:
        result = _get(f"/v2/objects/{target_object}/records/{record_id}", "records.get")
        values = ((result or {}).get("data") or {}).get("values") or {}
        name = _first_value(values.get("name"))
    except Exception:  # noqa: BLE001 — a missing company name is not a failed briefing
        logger.warning("could not resolve %s reference %s", target_object, record_id)
        name = None
    _REFERENCE_NAMES[cache_key] = name or ""
    return name


def summarize_person(record: dict[str, Any]) -> dict[str, str]:
    """The handful of fields a briefing can actually use, flattened."""
    values = record.get("values") or {}
    out: dict[str, str] = {}
    for key in _INTERESTING:
        raw = values.get(key)
        found = _reference_name(raw) or _first_value(raw)
        if found:
            out[key] = found[:300]
    web_url = record.get("web_url")
    if isinstance(web_url, str):
        out["web_url"] = web_url
    # Kept so open tasks can be queried against this person. Stripped before the
    # block reaches the model — see _render_attendees.
    record_id = ((record.get("id") or {}).get("record_id"))
    if isinstance(record_id, str):
        out["record_id"] = record_id
    return out


def find_person(email: str) -> list[dict[str, str]]:
    """People records matching one email address, newest-shaped first.

    Returns [] when the CRM has never heard of them, which is a real answer and
    the common one — most meeting attendees are not in the CRM.
    """
    payload = {
        "filter": {"email_addresses": {"email_address": email}},
        "limit": MAX_RECORDS,
    }
    result = _post("/v2/objects/people/records/query", payload, "records.query")
    records = result.get("data") if isinstance(result, dict) else None
    if not isinstance(records, list):
        return []
    return [summarize_person(r) for r in records[:MAX_RECORDS] if isinstance(r, dict)]


# Bounded: a person with fifty open tasks is a project, and a briefing that
# listed them all would bury the meeting it exists to prepare.
MAX_TASKS = int(os.getenv("ATTIO_MAX_TASKS", "8"))


def _task_summary(task: dict[str, Any]) -> dict[str, str]:
    """One task, reduced to what a briefing can act on."""
    out: dict[str, str] = {}
    content = task.get("content_plaintext")
    if isinstance(content, str) and content.strip():
        out["content"] = content.strip()[:300]
    deadline = task.get("deadline_at")
    if isinstance(deadline, str) and deadline:
        out["deadline"] = deadline[:10]
    # Who it belongs to is recorded but never used to filter. A task on someone
    # else's plate is still a thing that is owed to this person and still worth
    # walking into the meeting knowing about.
    assignees = task.get("assignees")
    if isinstance(assignees, list) and assignees:
        ids = [
            str(a.get("referenced_actor_id"))[:8]
            for a in assignees
            if isinstance(a, dict) and a.get("referenced_actor_id")
        ]
        if ids:
            out["assignee_count"] = str(len(ids))
    return out


def open_tasks(person_record_id: str) -> list[dict[str, str]]:
    """Outstanding tasks linked to one person, whoever owns them.

    `is_completed=false` is applied by the API rather than by filtering here, so
    a workspace with a long completed history does not page through it.

    Deliberately NOT written to the knowledge graph by callers: task state turns
    over constantly, and a graph that recorded it would spend its life recording
    yesterday's to-do list. Read it fresh, use it, discard it.
    """
    if not person_record_id:
        return []
    result = _get(
        f"/v2/tasks?linked_object=people&linked_record_id={person_record_id}"
        f"&is_completed=false&limit={MAX_TASKS}",
        "tasks.list",
    )
    records = result.get("data") if isinstance(result, dict) else None
    if not isinstance(records, list):
        return []
    summaries = [_task_summary(t) for t in records[:MAX_TASKS] if isinstance(t, dict)]
    return [t for t in summaries if t.get("content")]


# --- random contact sampling ------------------------------------------------

# One page of the people object. 500 is Attio's per-request ceiling.
_PAGE_LIMIT = 500

# How much of the CRM a daily sample is allowed to page through. A sample drawn
# from the first page only is not random — it is "whoever Attio sorts first",
# which is the same handful of people every morning. Paging the whole object
# once a day is affordable for a CRM of this size; the cap is a runaway guard,
# not a sampling decision.
POOL_LIMIT = int(os.getenv("ATTIO_POOL_LIMIT", "2000"))


def _email_addresses(record: dict[str, Any]) -> list[str]:
    """Every current email on a people record, lowercased.

    Not routed through `_first_value`: that collapses an attribute to one value,
    and a contact whose first listed address is a dead work address but whose
    second is current would be dropped entirely rather than reached.
    """
    values = (record.get("values") or {}).get("email_addresses")
    if isinstance(values, str):
        return [values.strip().lower()]
    if not isinstance(values, list):
        return []
    out = []
    for entry in values:
        if not isinstance(entry, dict):
            continue
        if entry.get("active_until"):
            continue
        address = entry.get("email_address") or entry.get("value")
        if isinstance(address, str) and "@" in address:
            out.append(address.strip().lower())
    return out


def list_people(pool_limit: int = POOL_LIMIT) -> list[dict[str, Any]]:
    """Page the people object, carrying only what selection needs.

    Each entry is `{record_id, emails, record}` — the raw record kept so a
    chosen one can be flattened later without being re-fetched.

    Deliberately NOT `summarize_person` per record. That resolves record
    references by fetching the referenced record, which is one extra HTTP call
    per person: fine for the three attendees of a meeting, and a sequential
    crawl when paging the whole object. Measured at 139s for a single page of
    500 — for a job with a timeout, on a pipeline that needs three contacts.
    Selection needs an address and an id; the expensive flattening is done by
    `summarize_person` for the handful of records actually drawn.

    Stops on a short page (the end of the object) or at `pool_limit`.
    """
    people: list[dict[str, Any]] = []
    offset = 0
    while offset < pool_limit:
        limit = min(_PAGE_LIMIT, pool_limit - offset)
        result = _post(
            "/v2/objects/people/records/query",
            {"limit": limit, "offset": offset},
            "records.query",
        )
        records = result.get("data") if isinstance(result, dict) else None
        if not isinstance(records, list) or not records:
            break
        for record in records:
            if not isinstance(record, dict):
                continue
            people.append(
                {
                    "record_id": ((record.get("id") or {}).get("record_id")),
                    "emails": _email_addresses(record),
                    "record": record,
                }
            )
        if len(records) < limit:
            break
        offset += len(records)
    return people
