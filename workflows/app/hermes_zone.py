"""The user's display timezone, for workflows running outside the agent.

Why this is a mirror and not shared code
----------------------------------------
The agent resolves the user's zone from ``timezone`` in ``~/.hermes/config.yaml``
(see ``hermes_time`` upstream). This container deliberately cannot read that
file: it holds provider credentials, and this is the container that reads
untrusted email. The narrow mounts in docker-compose.yml are the point, so the
answer is to *ask* the gateway rather than to widen them.

Resolution order:

1. ``HERMES_TIMEZONE`` — an explicit override, and what tests set.
2. The gateway's ``/health``, which publishes the zone it resolved. Cached for
   the process lifetime; workflow runs are short-lived processes, so one probe
   per run is the right granularity.
3. ``TZ`` — the container's own zone, if someone set one.
4. ``UTC``.

Every step fails open. A workflow must never crash, or block on a network
timeout, because it could not determine a *display* preference.

Note this is the DISPLAY zone. Cron runs on UTC regardless, by design — see
cron/clock.py upstream — so nothing here should be used as a scheduling clock.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# Both the gateway and this service run with network_mode: host, so the API
# server is reachable on loopback. Short timeout: a slow or absent gateway must
# cost a workflow ~1s, not a stall.
_GATEWAY_URL = os.getenv("HERMES_API_SERVER_URL", "http://127.0.0.1:8642").rstrip("/")
_GATEWAY_TIMEOUT_SECONDS = 1.5

_UNSET = object()
_cached_name: object = _UNSET


def _probe_gateway() -> str:
    """Ask the gateway which zone it resolved. Empty string on any failure."""
    try:
        with urllib.request.urlopen(
            f"{_GATEWAY_URL}/health", timeout=_GATEWAY_TIMEOUT_SECONDS
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        logger.debug("timezone probe to %s failed: %s", _GATEWAY_URL, exc)
        return ""
    info = payload.get("timezone")
    if isinstance(info, dict):
        name = info.get("name")
        return name.strip() if isinstance(name, str) else ""
    return ""


def zone_name() -> str:
    """Return the IANA name of the user's display timezone (cached)."""
    global _cached_name
    if _cached_name is not _UNSET:
        return _cached_name  # type: ignore[return-value]

    resolved = os.getenv("HERMES_TIMEZONE", "").strip()
    if not resolved:
        resolved = _probe_gateway()
    if not resolved:
        resolved = os.getenv("TZ", "").strip()
    _cached_name = resolved or "UTC"
    return _cached_name  # type: ignore[return-value]


def reset_cache() -> None:
    """Forget the resolved zone. For tests, and for long-lived processes."""
    global _cached_name
    _cached_name = _UNSET


def zone() -> ZoneInfo:
    """Return the display ZoneInfo, falling back to UTC on a bad name."""
    name = zone_name()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        logger.warning("unknown timezone %r; falling back to UTC", name)
        return ZoneInfo("UTC")


def now() -> datetime:
    """Current time as a tz-aware datetime in the display zone."""
    return datetime.now(zone())


def to_zone(value: datetime) -> datetime:
    """Convert a datetime into the display zone.

    Naive input is read as UTC — every timestamp this codebase stores is UTC,
    so that is the only reading that can be right.
    """
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(zone())


def fmt(value: datetime, *, with_zone: bool = True) -> str:
    """Render a timestamp for a human or a model, in the display zone.

    Always labelled by default: an unlabelled timestamp is exactly the
    ambiguity this module exists to remove.
    """
    local = to_zone(value)
    rendered = local.strftime("%Y-%m-%d %H:%M")
    if not with_zone:
        return rendered
    label = local.strftime("%Z") or zone_name()
    return f"{rendered} {label}"


def describe(value: datetime | None = None) -> str:
    """Format an instant with both abbreviation and IANA name.

    Handed to models so they never have to infer a zone or do offset
    arithmetic — both things they do unreliably. Defaults to now.
    """
    current = to_zone(value) if value is not None else now()
    label = current.strftime("%Z") or zone_name()
    return f"{current.strftime('%Y-%m-%d %H:%M')} {label} ({zone_name()})"


def describe_now() -> str:
    """The current time, described. See ``describe``."""
    return describe()


def humanize_age(value: datetime, *, reference: datetime | None = None) -> str:
    """Coarse "3 days ago" for a past timestamp.

    Models compare dates badly and subtract them worse, so the comparison is
    done here and the answer handed over as plain words.
    """
    ref = reference or datetime.now(timezone.utc)
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    seconds = (ref.astimezone(timezone.utc) - aware.astimezone(timezone.utc)).total_seconds()

    if seconds < 0:
        return "in the future"
    if seconds < 90:
        return "just now"

    minutes = seconds / 60
    if minutes < 60:
        count = int(minutes)
        return f"{count} minute{'s' if count != 1 else ''} ago"

    hours = minutes / 60
    if hours < 24:
        count = int(hours)
        return f"{count} hour{'s' if count != 1 else ''} ago"

    days = int(hours / 24)
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''} ago"

    months = days // 30
    if months < 12:
        return f"{months} month{'s' if months != 1 else ''} ago"

    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"
