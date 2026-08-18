"""Tests for the stack-health probe.

What is worth pinning down here is the judgement, not the plumbing. Whether
httpx can fetch a URL is httpx's business; what this module adds is the set of
rules that turn a response into a colour — and every one of those rules exists
because the naive reading of that response would have been wrong:

* 200 with an empty app list is a broken ADK server, not a healthy one.
* 401 from a token-guarded service is proof of life, not a failure.
* A slow answer is a different finding from no answer.
* The header light is the worst row, with nothing weighted down.

So the tests drive `_classify`, `overall` and the timing rule directly, and use
a real local socket for the two probe paths rather than mocking the transport.
"""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest

from . import health as H


def _resp(status: int, json_body=None) -> httpx.Response:
    """A response as httpx would hand one back, without a server."""
    request = httpx.Request("GET", "http://127.0.0.1/probe")
    if json_body is None:
        return httpx.Response(status, request=request, text="")
    return httpx.Response(status, request=request, json=json_body)


# --- What a response means -------------------------------------------------


def test_plain_200_is_ok_and_says_nothing_more():
    status, detail = H._classify({}, _resp(200))
    assert status == "ok"
    assert detail is None


def test_expected_json_field_must_actually_match():
    spec = {"expect_json": {"status": "ok"}}
    assert H._classify(spec, _resp(200, {"status": "ok"}))[0] == "ok"

    status, detail = H._classify(spec, _resp(200, {"status": "starting"}))
    assert status == "degraded"
    assert "starting" in detail


def test_adk_server_serving_no_apps_is_degraded_not_ok():
    # The failure this exists for: the process is up, /list-apps answers 200,
    # and every scheduled workflow is a no-op because the app tree did not
    # import. Green here would be a lie the cron tab could not correct.
    spec = {"expect_nonempty_list": True}
    assert H._classify(spec, _resp(200, []))[0] == "degraded"
    assert H._classify(spec, _resp(200, ["app.agents.draft_reply"]))[0] == "ok"


def test_metrics_store_with_no_profiles_is_degraded_not_ok():
    # The same class of failure one level in, and the reason the rule exists:
    # the store answers 200 whether or not its data directory is mounted, and
    # an unmounted one reports no profiles, no runs and no spend while looking
    # perfectly healthy. A green row there would say the fleet costs nothing.
    spec = {"expect_nonempty_key": "profiles", "empty_detail": "check the data mount"}
    status, detail = H._classify(spec, _resp(200, {"profiles": []}))
    assert status == "degraded"
    assert "mount" in detail
    assert H._classify(spec, _resp(200, {"profiles": ["default", "dev"]}))[0] == "ok"


def test_non_json_body_where_json_was_expected_is_degraded():
    spec = {"expect_json": {"status": "ok"}}
    status, detail = H._classify(spec, _resp(200))
    assert status == "degraded"
    assert "JSON" in detail


def test_server_error_is_down_and_client_error_is_degraded():
    # A 5xx is the service failing at its own job; a 4xx means it is well
    # enough to reject a request, which is a different call to make.
    assert H._classify({}, _resp(503))[0] == "down"
    assert H._classify({}, _resp(404))[0] == "degraded"


def test_auth_rejection_counts_as_alive_for_token_guarded_services():
    status, detail = H._classify({"auth_means_alive": True}, _resp(401))
    assert status == "degraded"
    assert "responding" in detail

    # Only for services that declared it. Elsewhere a 401 is just a 4xx.
    assert H._classify({}, _resp(401))[0] == "degraded"


# --- Timing ----------------------------------------------------------------


def test_a_slow_but_healthy_answer_is_degraded(monkeypatch):
    async def go():
        # Below zero, not zero: a mocked transport answers in well under a
        # millisecond, so a 0 threshold is one no probe here ever crosses.
        monkeypatch.setattr(H, "SLOW_MS", -1)
        transport = httpx.MockTransport(lambda req: _resp(200))
        async with httpx.AsyncClient(transport=transport) as client:
            return await H._probe_http(client, {"url": "http://127.0.0.1/x"})

    result = asyncio.run(go())
    assert result["status"] == "degraded"
    assert "slow" in result["detail"]


def test_slowness_does_not_overwrite_an_existing_complaint(monkeypatch):
    async def go():
        monkeypatch.setattr(H, "SLOW_MS", -1)
        transport = httpx.MockTransport(lambda req: _resp(200, []))
        async with httpx.AsyncClient(transport=transport) as client:
            return await H._probe_http(
                client, {"url": "http://127.0.0.1/x", "expect_nonempty_list": True}
            )

    result = asyncio.run(go())
    assert result["status"] == "degraded"
    assert result["detail"] == "serving no apps"


# --- Reaching, and failing to reach, a real port ---------------------------


def test_tcp_probe_is_ok_against_a_listening_socket():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        result = asyncio.run(
            H._probe_tcp({"host": "127.0.0.1", "port": port})
        )
    finally:
        server.close()
    assert result["status"] == "ok"


def test_tcp_probe_is_down_with_a_reason_when_nothing_listens():
    # Bind and immediately release, so the port is real and certainly free.
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.close()

    result = asyncio.run(H._probe_tcp({"host": "127.0.0.1", "port": port}))
    assert result["status"] == "down"
    assert result["detail"]  # never a bare, unexplained red row


def test_connection_failures_always_carry_a_reason():
    # httpx.ConnectError() stringifies to "", which would render a red row that
    # says a service is down and nothing about why.
    assert H._reason(httpx.ConnectError("")) == "connection refused — nothing listening"
    assert H._reason(asyncio.TimeoutError()).startswith("no answer within")
    assert H._reason(RuntimeError("")) == "RuntimeError"


# --- The header light ------------------------------------------------------


@pytest.mark.parametrize(
    "states,expected",
    [
        (["ok", "ok"], "ok"),
        (["ok", "degraded"], "degraded"),
        (["ok", "degraded", "down"], "down"),
        (["down", "ok"], "down"),
        ([], "unknown"),
    ],
)
def test_overall_is_the_worst_row(states, expected):
    assert H.overall([{"status": s} for s in states]) == expected


# --- Roster ----------------------------------------------------------------


def test_targets_follow_the_environment_compose_reads(monkeypatch):
    # These used to be built from BROWSER_PORT/DOCS_PORT, back when the console
    # probed published ports on the host. On a bridge network it reaches the
    # services by name instead, so the variable that moves a probe is the _URL,
    # and a published port is now irrelevant to whether the probe finds it.
    monkeypatch.setenv("BROWSER_URL", "http://browser:13010")
    by_id = {s["id"]: s for s in H._services()}
    assert "13010" in by_id["browser"]["url"]


def test_an_empty_override_falls_back_rather_than_probing_nothing(monkeypatch):
    # An unset BROWSER_URL is a supported state — it means "rendering
    # unavailable" — but an EMPTY one reaching _base() as a bare string would
    # build "/json/version" and probe the console itself. Falling back is what
    # keeps a blank line in .env from turning into a green row for a service
    # that is not running.
    monkeypatch.setenv("DOCS_URL", "")
    by_id = {s["id"]: s for s in H._services()}
    assert by_id["docs"]["url"] == "http://docs:80/"


def test_every_service_declares_what_a_reader_needs_to_act():
    for spec in H._services():
        assert spec["label"] and spec["group"] and spec["note"], spec["id"]


def test_snapshot_serves_a_cached_sweep_within_its_ttl(monkeypatch):
    calls = {"n": 0}

    async def counted(client, spec):
        calls["n"] += 1
        return {"id": spec["id"], "label": spec["label"], "group": spec["group"],
                "note": None, "target": None, "status": "ok", "detail": None,
                "latency_ms": 1}

    monkeypatch.setattr(H, "_probe", counted)
    monkeypatch.setattr(H, "_cache", {"at": 0.0, "payload": None})

    first = asyncio.run(H.snapshot())
    after_one_sweep = calls["n"]
    assert after_one_sweep == len(H._services())
    assert first["overall"] == "ok"

    asyncio.run(H.snapshot())
    assert calls["n"] == after_one_sweep, "second call inside the TTL re-probed"

    asyncio.run(H.snapshot(max_age=0))
    assert calls["n"] > after_one_sweep, "fresh=1 did not force a live sweep"


# --- Version marker ---------------------------------------------------------
#
# The console reads this to answer "is an update available" and "is a migration
# pending". It cannot see /opt/migrations, which lives in the hermes-init image,
# so hermes-init records the ids it carries into the marker and the arithmetic
# happens here — which is the part worth testing.


def _marker(tmp_path, **fields):
    payload = {
        "seeded_version": "v0.1.0",
        "current_version": "v0.1.0",
        "last_migration": "0000",
        "available_migrations": [],
        "updated_at": "2026-08-13T12:00:00Z",
    }
    payload.update(fields)
    path = tmp_path / ".steward-version"
    path.write_text(json.dumps(payload))
    return str(path)


def test_a_fresh_install_has_nothing_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "VERSION_MARKER", _marker(tmp_path))
    state = H.version_state()
    assert state["version"] == "v0.1.0"
    assert state["seeded_version"] == "v0.1.0"
    assert state["pending_migrations"] == []
    assert state["last_update_at"] == "2026-08-13T12:00:00Z"


def test_migrations_beyond_the_applied_one_are_pending(tmp_path, monkeypatch):
    # The state hermes-update leaves behind when it fails at the health check:
    # images swapped, marker not advanced. The console has to be able to say so.
    monkeypatch.setattr(H, "VERSION_MARKER", _marker(
        tmp_path, last_migration="0002",
        available_migrations=["0001", "0002", "0003", "0004"]))
    assert H.version_state()["pending_migrations"] == ["0003", "0004"]


def test_ids_compare_as_fixed_width_strings(tmp_path, monkeypatch):
    # Zero-padding is what makes string comparison correct. If an id were ever
    # written unpadded, "9" > "0010" and the ordering silently inverts.
    monkeypatch.setattr(H, "VERSION_MARKER", _marker(
        tmp_path, last_migration="0009",
        available_migrations=["0009", "0010", "0011"]))
    assert H.version_state()["pending_migrations"] == ["0010", "0011"]


def test_the_upgrade_command_defaults_to_the_documented_location(tmp_path, monkeypatch):
    monkeypatch.delenv("STEWARD_HOME", raising=False)
    monkeypatch.setattr(H, "VERSION_MARKER", _marker(tmp_path))
    assert H.version_state()["steward_home"] == "/srv/steward"


def test_a_relocated_install_reports_its_own_path(tmp_path, monkeypatch):
    # Not hypothetical: both install.sh and hermes-update take --home, and a
    # panel that prints /srv/steward at someone who installed elsewhere sends
    # them to a command that does not exist.
    monkeypatch.setenv("STEWARD_HOME", "/opt/steward")
    monkeypatch.setattr(H, "VERSION_MARKER", _marker(tmp_path))
    assert H.version_state()["steward_home"] == "/opt/steward"


def test_the_upgrade_path_survives_a_missing_marker(tmp_path, monkeypatch):
    # The case that matters most. Every other field is None here, so the only
    # useful thing the About panel can say is "here is how to upgrade" — and
    # upgrading is what writes the marker whose absence we are reporting.
    monkeypatch.setenv("STEWARD_HOME", "/opt/steward")
    monkeypatch.setattr(H, "VERSION_MARKER", str(tmp_path / "absent"))
    state = H.version_state()
    assert state["version"] is None
    assert state["steward_home"] == "/opt/steward"


def test_last_migration_distinguishes_none_yet_from_unknown(tmp_path, monkeypatch):
    # "0000" is a definite answer — the marker was read and nothing has run —
    # and None means there was no marker to read. The panel renders them
    # differently, so folding them together here would hide a real difference.
    monkeypatch.setattr(H, "VERSION_MARKER", _marker(tmp_path))
    assert H.version_state()["last_migration"] == "0000"

    monkeypatch.setattr(H, "VERSION_MARKER", str(tmp_path / "absent"))
    assert H.version_state()["last_migration"] is None


def test_a_missing_marker_is_unknown_rather_than_an_error(tmp_path, monkeypatch):
    # An install predating the marker is exactly when a reader most needs the
    # rest of the readiness payload to still work.
    monkeypatch.setattr(H, "VERSION_MARKER", str(tmp_path / "absent"))
    state = H.version_state()
    assert state["version"] is None
    assert state["pending_migrations"] == []


def test_a_corrupt_marker_does_not_take_readiness_down(tmp_path, monkeypatch):
    path = tmp_path / ".steward-version"
    path.write_text("{ this is not json")
    monkeypatch.setattr(H, "VERSION_MARKER", str(path))
    assert H.version_state()["version"] is None


def test_readiness_carries_the_version_block(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "VERSION_MARKER", _marker(tmp_path))
    monkeypatch.setattr(H, "_cache", {"at": 0.0, "payload": None})

    async def ok(client, spec):
        return {"id": spec["id"], "label": spec["label"], "group": spec["group"],
                "note": None, "target": None, "status": "ok", "detail": None,
                "latency_ms": 1}

    monkeypatch.setattr(H, "_probe", ok)
    assert asyncio.run(H.snapshot())["version"]["version"] == "v0.1.0"
