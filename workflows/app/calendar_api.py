"""Deterministic Google Calendar reads over the Calendar REST API.

Mirrors `gmail_api` in shape and for the same reasons: the fetch is plumbing, it
runs unattended, and the credential belongs to the code that makes the call
rather than to a server an LLM drives.

Credential, and this is the important difference from `gmail_api`: this module
holds the **read-only** service account. A briefing needs to know what is on the
calendar and nothing more, so the identity it uses cannot create, move or delete
an event even if the code asking were wrong. The two service accounts are split
exactly along that line — see the writer/reader pair in `docker/.env`.

    CALENDAR_SERVICE_ACCOUNT_FILE + CALENDAR_DELEGATED_USER
        Domain-wide delegation, impersonating the mailbox whose calendar is being
        read. There is no token-file fallback here: unlike Gmail there is no
        single-mailbox laptop case for a scheduled briefing, and offering one
        would put a refresh token on disk for no gain.

Scope is calendar.readonly. Widening it is a security decision, not a config
tweak — the same rule `gmail_api` states about gmail.modify.
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Callable, Optional

from app import integration_log

logger = logging.getLogger(__name__)

# Read only. NOT calendar.events, NOT calendar — either would let a wrong call
# mutate a real schedule, and nothing in a briefing needs that.
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# Read by the dashboard's module scan (docker/light-dashboard/backend/adk_live.py
# and integrations.py) so this integration names itself rather than needing an
# entry in a hardcoded registry.
INTEGRATION_SOURCE = "calendar"
INTEGRATION_LABEL = "Google Calendar"

SERVICE_ACCOUNT_FILE = "CALENDAR_SERVICE_ACCOUNT_FILE"
DELEGATED_USER = "CALENDAR_DELEGATED_USER"

# Calendar's list endpoints page at 250; a day of meetings is far short of that,
# and a cap means one pathological day cannot produce an unbounded fetch.
MAX_EVENTS = int(os.getenv("CALENDAR_MAX_EVENTS", "50"))

MAX_RETRIES = int(os.getenv("CALENDAR_MAX_RETRIES", "5"))


def configured() -> bool:
    """True when this deployment has given the pipeline a calendar credential."""
    return bool(
        os.environ.get(SERVICE_ACCOUNT_FILE) and os.environ.get(DELEGATED_USER)
    )


def _credentials():
    sa_file = os.environ.get(SERVICE_ACCOUNT_FILE)
    subject = os.environ.get(DELEGATED_USER)
    if not (sa_file and subject):
        raise RuntimeError(
            f"no Calendar credential: set {SERVICE_ACCOUNT_FILE}+{DELEGATED_USER}"
        )
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_file(
        sa_file, scopes=SCOPES
    ).with_subject(subject)


def build_service():
    """An authorized Calendar client. `cache_discovery=False` — no writable cache."""
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=_credentials(), cache_discovery=False)


def _retry(call: Callable[[], Any], what: str) -> Any:
    """Execute with backoff, recording one outcome per logical call.

    Same rule as `gmail_api._retry`: 429 and 5xx are transient and retried, 4xx
    is not — a revoked scope fails identically every time and retrying hides it.
    The integration record is written once, after retries settle, so three 429s
    then a success reads as one working call rather than an intermittent fault.
    """
    from googleapiclient.errors import HttpError

    attempt = 0
    while True:
        try:
            result = call()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            transient = status == 429 or (status is not None and 500 <= status < 600)
            if transient and attempt < MAX_RETRIES:
                delay = min(2**attempt, 32) + random.uniform(0, 1)
                logger.warning("calendar %s: %s, retrying in %.1fs", what, status, delay)
                time.sleep(delay)
                attempt += 1
                continue
            integration_log.record(
                INTEGRATION_SOURCE, what, ok=False, capability="read", error=exc
            )
            raise
        except Exception as exc:  # network, auth, anything else terminal
            integration_log.record(
                INTEGRATION_SOURCE, what, ok=False, capability="read", error=exc
            )
            raise
        integration_log.record(INTEGRATION_SOURCE, what, ok=True, capability="read")
        return result


def _is_declined(event: dict, owner: Optional[str]) -> bool:
    """Did the owner say no? A declined meeting is not on their day.

    Checked here rather than left to the model: attendance is a fact recorded in
    the event, and a briefing that prepares someone for a meeting they declined
    is worse than one that omits it.
    """
    for attendee in event.get("attendees") or []:
        if attendee.get("self") or (owner and attendee.get("email") == owner):
            return attendee.get("responseStatus") == "declined"
    return False


def _normalize(event: dict, calendar_id: str, owner: Optional[str]) -> dict[str, Any]:
    """The subset of an event the briefing is allowed to see.

    Deliberately narrow. Attachments, conference links, private extended
    properties and per-attendee metadata stay out: the drafting stage feeds this
    to a model, and every field included is a field an attacker who can put text
    in a calendar invite gets to write.
    """
    start = event.get("start") or {}
    end = event.get("end") or {}
    return {
        "event_id": event.get("id"),
        "calendar_id": calendar_id,
        "title": event.get("summary"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "location": event.get("location"),
        "organizer": (event.get("organizer") or {}).get("email"),
        "attendees": [
            a.get("email") for a in (event.get("attendees") or []) if a.get("email")
        ],
        "description": event.get("description"),
        "notes": None,
    }


def list_calendar_ids(service) -> list[str]:
    """Every calendar the delegated user can read."""
    resp = _retry(lambda: service.calendarList().list().execute(), "calendarList.list")
    return [c["id"] for c in (resp.get("items") or []) if c.get("id")]


def list_events(
    service,
    time_min: str,
    time_max: str,
    calendar_ids: Optional[list[str]] = None,
    owner: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Events in a window, across calendars, normalized and sorted by start.

    Returns (events, skipped_reasons). `singleEvents=True` expands recurrence, so
    a weekly standup appears as the one instance that falls in the window rather
    than as a rule the caller would have to evaluate itself.

    One calendar failing does not fail the briefing: its reason is returned and
    the remaining calendars still produce a day. A morning with one unreadable
    calendar is worth more than no morning at all.
    """
    ids = calendar_ids if calendar_ids is not None else list_calendar_ids(service)
    events: list[dict[str, Any]] = []
    skipped: list[str] = []

    for calendar_id in ids:
        try:
            resp = _retry(
                lambda cid=calendar_id: service.events()
                .list(
                    calendarId=cid,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=MAX_EVENTS,
                )
                .execute(),
                "events.list",
            )
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            logger.warning("calendar %s unreadable: %s", calendar_id, exc)
            skipped.append(f"{calendar_id}: {type(exc).__name__}")
            continue

        for raw in resp.get("items") or []:
            if raw.get("status") == "cancelled":
                continue
            if _is_declined(raw, owner):
                continue
            events.append(_normalize(raw, calendar_id, owner))

    events.sort(key=lambda e: (e.get("start") or ""))
    return events[:MAX_EVENTS], skipped
