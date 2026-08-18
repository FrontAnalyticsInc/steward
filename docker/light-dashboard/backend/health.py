"""Is every service in this Hermes stack still serving?

The stack is a dozen processes that depend on each other in ways no single
screen showed: the review executor cannot deliver without Gmail, the ADK server
cannot serve a workflow whose app tree failed to import, and chat cannot do
anything without the Hermes gateway. Each
tab in this dashboard noticed its own dependency failing and none of them
noticed anyone else's, so "the page is broken" was the only symptom a reader
ever got. This module is the one place that asks all of them at once.

What it probes, and what it deliberately does not:

* It asks each service over the network, not Docker. This container has no
  docker socket by design (see the light-dashboard service in
  docker-compose.yml — sharing it would trade a restart button for control of
  every container), so "the container is running" is not observable from here.
  That is the better check anyway: a wedged uvicorn is `Up` in `docker ps` and
  answers nothing, and it is answering that the rest of the stack needs.

* Every address is 127.0.0.1. Every service in this compose file either runs
  with `network_mode: host` or publishes its port to the host, and this
  container is host-networked too, so loopback reaches all of them. Service
  names would resolve for none of them.

* Ports come from the same environment variables docker-compose.yml reads,
  with the same defaults, so a port changed in .env does not leave this file
  probing an address nothing is on.

Three states, and the difference between the last two is the point:

  ok        answered, promptly, with what it was supposed to say
  degraded  answered, but slowly or with something other than health
  down      did not answer

`degraded` exists so that "Ollama took nine seconds" is not filed next to
"Ollama is gone". A stack under load looks nothing like a stack with a dead
process, and a reader deciding whether to page someone needs them separated.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional

import httpx

# Per-probe ceiling. Short on purpose: this is a liveness question, and a
# service that needs longer than this to say "I am here" is already a finding.
# The whole roster runs concurrently, so the endpoint's worst case is one
# timeout, not the sum of them.
PROBE_TIMEOUT = float(os.getenv("HEALTH_PROBE_TIMEOUT", "4.0"))

# Answered, but slowly. Loopback to a healthy local service is single-digit
# milliseconds (measured across this whole roster); a second is three orders of
# magnitude past that and means something is queueing.
SLOW_MS = float(os.getenv("HEALTH_SLOW_MS", "1500"))

# How long a probe result is reused. The frontend polls this on the dashboard's
# shared 7s loop and there may be several browser tabs open; without a cache,
# each one would put its own load on twelve services whose health cannot
# meaningfully change between them.
CACHE_TTL = float(os.getenv("HEALTH_CACHE_TTL", "5.0"))


def _port(var: str, default: int) -> int:
    """A port from the environment, falling back the way compose does."""
    raw = (os.getenv(var) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _base(var: str, default: str) -> str:
    return (os.getenv(var) or default).rstrip("/")


BROWSER_TOKEN = os.getenv("BROWSER_TOKEN", "")

# Where hermes-init writes the version marker. The console cannot see
# /opt/migrations — that lives in the hermes-init image, not this one — so
# hermes-init records the ids it carries into the marker on every run and the
# arithmetic happens here.
VERSION_MARKER = os.getenv("STEWARD_VERSION_MARKER", "/opt/data/.steward-version")


def version_state() -> Dict[str, Any]:
    """What release this deployment is on, and whether an upgrade is half-done.

    Never raises, like everything else on this endpoint. A missing or malformed
    marker is reported as unknown rather than taking down the readiness check —
    an install predating the marker is exactly the case where a reader most
    needs the rest of this payload to still work.

    `pending_migrations` non-empty on a running stack means an upgrade applied
    images without completing its migrations, which is the state hermes-update
    leaves behind when it fails at the health check. Re-running it is the fix,
    and the console should say so rather than leaving someone to guess.

    `steward_home` is where that runner lives, so the About panel can print the
    command rather than a guess. It is set before the marker is read and never
    inside the try: a box with no marker is precisely the one whose only useful
    answer is "here is how to upgrade", and it would be absurd to withhold the
    path because the file naming the version is missing.
    """
    state: Dict[str, Any] = {
        "version": None,
        "seeded_version": None,
        "last_migration": None,
        "pending_migrations": [],
        "last_update_at": None,
        # Not read from the marker — the marker is written by hermes-init, which
        # lives in a container and has no idea what host path the stack was
        # installed at. install.sh puts it in .env; compose passes it here.
        "steward_home": os.getenv("STEWARD_HOME", "/srv/steward"),
    }
    try:
        with open(VERSION_MARKER, "r", encoding="utf-8") as fh:
            marker = json.load(fh)
    except (OSError, ValueError):
        return state

    applied = str(marker.get("last_migration") or "0000")
    available = marker.get("available_migrations") or []
    state["version"] = marker.get("current_version")
    state["seeded_version"] = marker.get("seeded_version")
    # Reported as-is, including the "0000" that means none have run. The panel
    # renders that as "none applied" — distinguishing it from the null above,
    # which means there is no marker to have read it from.
    state["last_migration"] = applied
    state["last_update_at"] = marker.get("updated_at")
    # Fixed-width zero-padded ids, so a string comparison is the ordering.
    state["pending_migrations"] = sorted(
        str(i) for i in available if str(i) > applied
    )
    return state


def _services() -> List[Dict[str, Any]]:
    """The roster, resolved against the current environment.

    Built per call rather than at import so a test can move a port and see the
    probe follow it, and so the ports shown in the UI are never a stale copy of
    what the process started with.

    `group` is what the reader is being told is at risk, not what the container
    happens to be — which is why the LiteLLM proxy sits under Models rather than
    under its own name. A reader who sees it red needs to know that no agent can
    think, not which image stopped.
    """
    gateway = _base("HERMES_API_BASE", "http://localhost:8642")
    hermes_dashboard = _base("HERMES_DASHBOARD_URL", "http://127.0.0.1:9119")
    workflows = _base("ADK_WORKFLOWS_URL", "http://127.0.0.1:8020")

    return [
        {
            "id": "gateway",
            "label": "Hermes Gateway",
            "group": "Agent",
            "kind": "http",
            "url": f"{gateway}/health",
            "expect_json": {"status": "ok"},
            "note": "The agent itself. Chat, cron and every tool call go through it.",
        },
        {
            "id": "hermes-dashboard",
            "label": "Hermes Dashboard",
            "group": "Agent",
            "kind": "http",
            "url": f"{hermes_dashboard}/",
            "note": "Upstream console. Also serves the messaging-platform API this "
                    "dashboard proxies channel settings through.",
        },
        # The console's own row is gone. It could only ever be green — it was
        # answering the request that drew it — so it spent a row telling the
        # reader something the page's own existence already proved. What this
        # process does that CAN fail is served by the Metrics Store and Memory
        # rows below, which probe its real endpoints.
        {
            "id": "workflows",
            "label": "ADK Workflows",
            "group": "Agent",
            "kind": "http",
            "url": f"{workflows}/list-apps",
            # A served app list is the only proof the ADK server is usable: the
            # process can be up with its app tree failing to import, and then
            # /list-apps answers 200 with [] and every scheduled workflow is a
            # no-op. Empty is degraded, not ok.
            "expect_nonempty_list": True,
            "note": "Serves every ADK agent. Empty means the app tree failed to import.",
        },
        {
            "id": "metrics-store",
            "label": "Metrics Store",
            "group": "Observability",
            # Probed over HTTP like everything else, even though it runs inside
            # this very process. The alternative — importing metrics_store and
            # calling it directly — would report on a connection this module
            # opened rather than the one the Metrics tab actually uses, and the
            # failures worth catching (a data directory that is not mounted, a
            # DuckDB file that cannot be opened, the sqlite extension missing
            # from the image) are all invisible until something asks the real
            # endpoint. A 503 from it becomes `down` by the ordinary rule.
            "kind": "http",
            "url": f"http://127.0.0.1:{_port('LIGHT_DASHBOARD_PORT', 9120)}/api/metrics/health",
            # Zero profiles means it attached no Hermes state database, which
            # answers 200 while reporting no cost for anything.
            "expect_nonempty_key": "profiles",
            "empty_detail": "answering, but found no Hermes profiles — check the data mount",
            "note": "Cost and usage across ADK, Hermes chat and automations. "
                    "Down means the Metrics tab is blank, not that spend stopped.",
        },
        {
            "id": "memory",
            "label": "Memory",
            "group": "Observability",
            # Not a service — a directory of markdown with an FTS5 index beside
            # it, which is exactly why it earns a row. There is no process to
            # crash, so nothing else on this page would ever go red for it; what
            # fails instead is quieter and worse. The directory is not mounted,
            # or the index was never built, and every search returns nothing
            # while the agent reports honestly that it found no memories.
            #
            # Probed through this dashboard's own endpoint rather than by
            # reading the directory here, for the reason the Metrics Store row
            # gives: the failure worth catching is a mount that this process
            # cannot see, and only the real endpoint can demonstrate that.
            "kind": "http",
            "url": f"http://127.0.0.1:{_port('LIGHT_DASHBOARD_PORT', 9120)}/api/wiki/health",
            "expect_nonempty_key": "documents",
            "empty_detail": "answering, but holds no documents — check the wiki mount",
            "expect_true_key": "index_present",
            "false_detail": "documents are there but the search index is missing — "
                            "recall returns nothing",
            "note": "The agent's long-term memory: markdown documents and the "
                    "search index over them. Empty means recall silently finds nothing.",
        },
        # There is deliberately no telemetry row. The tracing backend and the
        # collector in front of it were removed, and their absence is a fact
        # about the stack rather than a state to monitor — a row for a thing
        # that does not exist can only ever be red, and a permanently red row
        # teaches the reader to ignore the colour. Observability here is the
        # Metrics Store and Memory rows above, which read the JSONL run records.
        # If a backend is ever added, its row comes back with it.
        {
            "id": "browser",
            "label": "Renderer",
            "group": "Tools",
            "kind": "http",
            # /json/version is browserless's path, kept when we replaced it with
            # docker/browser so this probe did not have to change. It also
            # reports which extensions loaded.
            # By service name, not 127.0.0.1. Inside this container loopback is
            # this container, so the old address only ever worked while the
            # stack shared one network namespace; on the bridge it reported a
            # healthy renderer as "connection refused". Same rule the gateway
            # and workflows rows already followed.
            "url": f"{_base('BROWSER_URL', 'http://browser:3010')}/json/version",
            # Its whole job is fetching arbitrary URLs, so it is behind a token.
            # Supplied here when the environment has one; see `_classify` for
            # what a 401 means when it does not.
            "params": {"token": BROWSER_TOKEN} if BROWSER_TOKEN else None,
            "auth_means_alive": True,
            "note": "Headless Chromium for the crawler. Down means "
                    "JavaScript-rendered sites read as empty, not as failures.",
        },
        {
            "id": "docs",
            "label": "Docs",
            "group": "Tools",
            "kind": "http",
            # Service name for the same reason as the renderer above. Port 80,
            # not DOCS_PORT: that variable is the host-side publish port, and
            # inside the network the container serves on 80.
            "url": f"{_base('DOCS_URL', 'http://docs:80')}/",
            "note": "The MkDocs build for this stack.",
        },
    ]


def _classify(spec: Dict[str, Any], resp: httpx.Response) -> tuple[str, Optional[str]]:
    """Status and one line of detail, from a response that arrived.

    Returns (status, detail). Detail is what the row says under its name, so it
    carries the reason for anything that is not plain `ok` and stays quiet
    otherwise — a row reading "200 OK" next to a green dot says nothing the dot
    did not.
    """
    # A service that answers 401 answered: the process is alive and its HTTP
    # stack is serving. That is the whole liveness question, and calling it
    # `down` would send someone looking for a crashed container that is fine.
    # It is not `ok` either, because this probe could not see past the door.
    if resp.status_code in (401, 403) and spec.get("auth_means_alive"):
        return "degraded", (
            "responding, but rejected this probe's credentials"
            if BROWSER_TOKEN
            else "responding; no token configured for this probe"
        )

    if resp.status_code >= 500:
        return "down", f"HTTP {resp.status_code}"
    if resp.status_code >= 400:
        return "degraded", f"HTTP {resp.status_code}"

    expect = spec.get("expect_json")
    if expect:
        try:
            body = resp.json()
        except Exception:
            return "degraded", "answered, but not with JSON"
        for key, want in expect.items():
            got = body.get(key)
            if got != want:
                return "degraded", f"reports {key}={got!r}, expected {want!r}"

    if spec.get("expect_nonempty_list"):
        try:
            body = resp.json()
        except Exception:
            return "degraded", "answered, but not with JSON"
        if not isinstance(body, list):
            return "degraded", "answered, but not with a list"
        if not body:
            return "degraded", "serving no apps"

    # Same idea one level in: a service can answer perfectly while having found
    # nothing to serve, and the two must not both read as `ok`. The metrics
    # store is the case that motivated it — it answers 200 with an empty
    # profile list when its data directory is not mounted, which looks healthy
    # and reports no spend at all.
    key = spec.get("expect_nonempty_key")
    if key:
        try:
            body = resp.json()
        except Exception:
            return "degraded", "answered, but not with JSON"
        if not (body or {}).get(key):
            return "degraded", spec.get("empty_detail") or f"answered, but {key} is empty"

    # And a flag the service raises about itself. Distinct from the check above
    # because "has content" and "can be searched" fail separately: Memory can
    # hold a hundred documents with no FTS index beside them, which answers 200,
    # counts them all, and returns nothing for every query.
    flag = spec.get("expect_true_key")
    if flag:
        try:
            body = resp.json()
        except Exception:
            return "degraded", "answered, but not with JSON"
        if not (body or {}).get(flag):
            return "degraded", spec.get("false_detail") or f"answered, but {flag} is false"

    return "ok", None


async def _probe_http(client: httpx.AsyncClient, spec: Dict[str, Any]) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        resp = await client.get(spec["url"], params=spec.get("params") or None)
    except Exception as exc:
        return {
            "status": "down",
            "detail": _reason(exc),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    latency_ms = int((time.monotonic() - started) * 1000)
    status, detail = _classify(spec, resp)
    # Slowness never upgrades a status, only downgrades one: a service already
    # reporting trouble should keep saying what the trouble is.
    if status == "ok" and latency_ms > SLOW_MS:
        status, detail = "degraded", f"slow to answer ({latency_ms} ms)"
    return {"status": status, "detail": detail, "latency_ms": latency_ms}


async def _probe_tcp(spec: Dict[str, Any]) -> Dict[str, Any]:
    started = time.monotonic()
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(spec["host"], spec["port"]), timeout=PROBE_TIMEOUT
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        status = "ok" if latency_ms <= SLOW_MS else "degraded"
        detail = None if status == "ok" else f"slow to accept ({latency_ms} ms)"
        return {"status": status, "detail": detail, "latency_ms": latency_ms}
    except Exception as exc:
        return {
            "status": "down",
            "detail": _reason(exc),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        if writer is not None:
            writer.close()


def _reason(exc: Exception) -> str:
    """Why a probe failed, in the words a reader can act on.

    httpx and asyncio both stringify some failures to the empty string
    (`ConnectError()`), which would render a row that says a service is down
    and nothing else.
    """
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return f"no answer within {PROBE_TIMEOUT:g}s"
    if isinstance(exc, (ConnectionRefusedError, httpx.ConnectError)):
        return "connection refused — nothing listening"
    text = str(exc).strip()
    return text or exc.__class__.__name__


async def _probe(client: httpx.AsyncClient, spec: Dict[str, Any]) -> Dict[str, Any]:
    """One service, as the frontend receives it."""
    kind = spec["kind"]
    if kind == "self":
        # Reached by answering this request. Probing our own port would test
        # the loopback stack, not this process, and would deadlock a
        # single-worker uvicorn serving the very request that made the call.
        result = {"status": "ok", "detail": None, "latency_ms": 0}
    elif kind == "tcp":
        result = await _probe_tcp(spec)
    else:
        result = await _probe_http(client, spec)

    target = spec.get("url") or (
        f"{spec['host']}:{spec['port']}" if kind == "tcp" else None
    )
    return {
        "id": spec["id"],
        "label": spec["label"],
        "group": spec["group"],
        "note": spec.get("note"),
        "target": target,
        **result,
    }


# Worst-first, so `max` over this ordering answers "what is the stack's state".
_RANK = {"ok": 0, "degraded": 1, "down": 2}


def overall(services: List[Dict[str, Any]]) -> str:
    """The single state the header indicator shows.

    The worst of them, with no weighting: this stack has no service whose loss
    is survivable in a way worth encoding here, and a rule that quietly
    downgraded one of them would be the reason nobody trusted the light.
    """
    if not services:
        return "unknown"
    return max((s["status"] for s in services), key=lambda s: _RANK.get(s, 0))


_cache: Dict[str, Any] = {"at": 0.0, "payload": None}


async def snapshot(max_age: Optional[float] = None) -> Dict[str, Any]:
    """Every service's state, from cache when it is fresh enough.

    Pass max_age=0 to force a live sweep — what the modal's Refresh button
    does, since a reader who just restarted something is asking precisely the
    question the cache would answer stalely.
    """
    ttl = CACHE_TTL if max_age is None else max_age
    now = time.monotonic()
    if _cache["payload"] is not None and (now - _cache["at"]) < ttl:
        cached = dict(_cache["payload"])
        cached["age_s"] = round(now - _cache["at"], 1)
        return cached

    specs = _services()
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=True) as client:
        results = await asyncio.gather(*(_probe(client, s) for s in specs))

    services = list(results)
    payload = {
        "overall": overall(services),
        "services": services,
        "counts": {
            state: sum(1 for s in services if s["status"] == state)
            for state in ("ok", "degraded", "down")
        },
        "checked_at": time.time(),
        "age_s": 0.0,
        "version": version_state(),
    }
    _cache["at"] = now
    _cache["payload"] = payload
    return payload
