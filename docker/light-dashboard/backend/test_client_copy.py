"""The naming leak that arrives over the wire, pinned so it cannot come back.

Six naming tasks passed over ``GET /api/channels`` while it was saying "Use
Hermes from Slack via Socket Mode" on the page every client uses to connect a
channel. The reason is worth keeping in front of whoever changes this next: the
sentence is not in our tree. It is in the upstream gateway's channel catalog,
and it arrives over HTTP at request time, so a sweep of our Python literals and
a grep of our compiled JSX were both correct and both blind.

That makes the recorded payloads next door load-bearing rather than
convenience. ``testdata/upstream_channel_catalog.json`` is what the gateway
actually answered on 2026-08-22 for the six exposed platforms, and
``testdata/channels_payload_pre_fix.json`` is what this console actually
returned from it. Both were captured from the running stack and then sanitised
— every ``redacted_value`` nulled, ``is_set``/``enabled``/``configured`` forced
false, live chats and ``home_channel`` dropped — because this repository is
public and a redacted token prefix is still a token prefix. Nothing that
matters here was touched: the prose, the ids, the keys and the shape are
upstream's own.

The tests therefore run the real mapper over the real catalog. A synthetic
fixture would only prove the mapper handles strings someone here thought of,
which is the exact mistake being corrected.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict

import pytest

from . import channels as C
from . import client_copy as CC

HERE = os.path.dirname(os.path.abspath(__file__))
TESTDATA = os.path.join(HERE, "testdata")


def _load(name: str) -> Any:
    with open(os.path.join(TESTDATA, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


UPSTREAM = _load("upstream_channel_catalog.json")
PRE_FIX = _load("channels_payload_pre_fix.json")


# --- The detector ----------------------------------------------------------


def test_detector_fails_on_the_pre_fix_payload():
    """The whole point. A check that passes on the bug is decorative.

    Seven, not the six the brief counted: the seventh is teams' ``docs_url``,
    which the Channels panel renders as its own link *text*, so the client
    reads the hostname ``hermes-agent.nousresearch.com`` off the page.
    """
    leaks = CC.find_leaks(PRE_FIX)
    paths = [p for p, _, _ in leaks]
    assert len(leaks) == 7, leaks
    assert paths == [
        ".channels[0].description",
        ".channels[0].env_vars[2].description",
        ".channels[1].description",
        ".channels[1].docs_url",
        ".channels[2].description",
        ".channels[3].description",
        ".channels[5].description",
    ]


def test_detector_reads_values_and_keys_apart():
    payload = {"hermes_provider": "anthropic", "note": "ask Hermes"}
    leaks = {path for path, _, _ in CC.find_leaks(payload)}
    assert ".hermes_provider#key" in leaks   # a field name, reported not renamed
    assert ".note" in leaks


def test_detector_sees_a_name_inside_a_longer_token():
    """A hostname is read by the client even though it is not a word."""
    assert CC.leak_name("https://hermes-agent.nousresearch.com/docs") == "hermes"


def test_detector_does_not_fire_on_ordinary_words():
    for clean in ("Add the bot to a channel", "Slack member IDs", "gadket", "adkins"):
        assert CC.leak_name(clean) is None, clean


# --- The map ---------------------------------------------------------------


@pytest.mark.parametrize(
    "before, after",
    [
        ("Run Hermes from Telegram DMs.", "Run Steward from Telegram DMs."),
        ("Hermes's own dashboard", "Steward's own dashboard"),
        ("Hermes’s own dashboard", "Steward's own dashboard"),
        ("Built by Hermes Agent", "Built by Steward"),
        ("", ""),
    ],
)
def test_scrub_maps_the_name_and_leaves_the_sentence(before, after):
    assert CC.scrub(before) == after


def test_scrub_passes_none_through():
    """Callers apply this to optional fields; it must not invent a value."""
    assert CC.scrub(None) is None


def test_scrub_keeps_the_facts():
    """The map changes a name. It must not touch scopes, prefixes or gotchas.

    Upstream's env-var copy is the only accurate description anyone has of
    these fields, which is why the console maps rather than rewrites them.
    """
    original = (
        "Slack bot token (xoxb-). Required scopes: chat:write, app_mentions:read"
    )
    assert CC.scrub(original) == original


def test_ingredient_urls_are_recognised_by_host_only():
    assert CC.is_ingredient_url("https://hermes-agent.nousresearch.com/docs/x")
    assert not CC.is_ingredient_url("https://api.slack.com/apps")
    assert not CC.is_ingredient_url("https://example.com/hermes-guide")
    assert not CC.is_ingredient_url(None)


# --- The map, applied to the real catalog ----------------------------------


def _list_channels(catalog: Dict[str, Any], monkeypatch, chats=None) -> Dict[str, Any]:
    """Run the real list_channels over a recorded upstream response."""

    class _Resp:
        status_code = 200

        def json(self):
            return catalog

    async def _request(method, path, **kwargs):
        assert (method, path) == ("GET", "/api/messaging/platforms")
        return _Resp()

    monkeypatch.setattr(C._client, "request", _request)
    monkeypatch.setattr(C, "_live_chats", lambda data_dir: chats or {})
    return asyncio.run(C.list_channels("/nonexistent"))


def test_the_real_catalog_comes_out_clean(monkeypatch):
    out = _list_channels(UPSTREAM, monkeypatch)
    assert CC.find_leaks(out) == []


def test_platform_ids_and_field_names_are_untouched(monkeypatch):
    """Task 32's hard constraint, checked against the payload rather than the diff.

    Ids and field names are keys the frontend renders by and the gateway writes
    env by. This compares the mapped output to what the console returned before
    the fix: same ids in the same order, same field names, same env-var keys.
    """
    out = _list_channels(UPSTREAM, monkeypatch)

    assert [c["id"] for c in out["channels"]] == [c["id"] for c in PRE_FIX["channels"]]
    assert set(out) == set(PRE_FIX)

    for got, was in zip(out["channels"], PRE_FIX["channels"]):
        assert set(got) == set(was), got["id"]
        assert [v["key"] for v in got["env_vars"]] == [v["key"] for v in was["env_vars"]]
        for gv, wv in zip(got["env_vars"], was["env_vars"]):
            assert set(gv) == set(wv), (got["id"], gv["key"])


def test_only_human_readable_copy_moved(monkeypatch):
    """Nothing changed except prose and one link. Named, so a surprise shows up."""
    out = _list_channels(UPSTREAM, monkeypatch)
    moved = set()
    for got, was in zip(out["channels"], PRE_FIX["channels"]):
        for key in was:
            if key == "env_vars":
                continue
            if got[key] != was[key]:
                moved.add(f"{got['id']}.{key}")
        for gv, wv in zip(got["env_vars"], was["env_vars"]):
            for key in wv:
                if gv[key] != wv[key]:
                    moved.add(f"{got['id']}/{wv['key']}.{key}")
    assert moved == {
        "slack.description",
        "slack/SLACK_ALLOWED_USERS.description",
        "teams.description",
        "teams.docs_url",
        "telegram.description",
        "discord.description",
        "whatsapp.description",
    }


def test_a_new_upstream_sentence_is_mapped_too(monkeypatch):
    """The fix is the hole, not the six strings that came through it."""
    catalog = json.loads(json.dumps(UPSTREAM))
    for platform in catalog["platforms"]:
        if platform["id"] == "signal":
            platform["description"] = "A new Hermes adapter upstream added later."
            platform["env_vars"][0]["help"] = "Point this at Hermes's bridge."
            platform["env_vars"][0]["prompt"] = "Hermes bridge URL"
    out = _list_channels(catalog, monkeypatch)
    assert CC.find_leaks(out) == []
    signal = next(c for c in out["channels"] if c["id"] == "signal")
    assert signal["description"] == "A new Steward adapter upstream added later."
    assert signal["env_vars"][0]["help"] == "Point this at Steward's bridge."
    assert signal["env_vars"][0]["prompt"] == "Steward bridge URL"


def test_an_ingredient_docs_link_never_reaches_the_page(monkeypatch):
    """teams gets Microsoft's own page; anything else loses the link entirely.

    Dropping it is the safe default: the panel renders docs_url as its visible
    link text, so a hostname with no substitute is worse than no paragraph.
    """
    catalog = json.loads(json.dumps(UPSTREAM))
    for platform in catalog["platforms"]:
        if platform["id"] == "discord":
            platform["docs_url"] = "https://hermes-agent.nousresearch.com/docs/discord"
    out = _list_channels(catalog, monkeypatch)
    by_id = {c["id"]: c for c in out["channels"]}
    assert by_id["teams"]["docs_url"].startswith("https://learn.microsoft.com/")
    assert by_id["discord"]["docs_url"] is None
    assert by_id["slack"]["docs_url"] == "https://api.slack.com/apps"


def test_an_unknown_platform_row_still_appears(monkeypatch):
    """The pre-existing fallback row is not disturbed by the mapping."""
    catalog = {"platforms": [p for p in UPSTREAM["platforms"] if p["id"] != "signal"]}
    out = _list_channels(catalog, monkeypatch)
    signal = next(c for c in out["channels"] if c["id"] == "signal")
    assert signal["unknown"] is True
    assert signal["name"] == "Signal"


def test_a_malformed_env_var_does_not_break_the_page(monkeypatch):
    catalog = json.loads(json.dumps(UPSTREAM))
    catalog["platforms"][0]["env_vars"].append("not a dict")
    out = _list_channels(catalog, monkeypatch)
    assert all(isinstance(v, dict) for v in out["channels"][0]["env_vars"])


# --- The check tool --------------------------------------------------------


def _check(argv):
    import importlib.util

    path = os.path.join(os.path.dirname(HERE), "tools", "check_client_copy.py")
    spec = importlib.util.spec_from_file_location("check_client_copy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(argv)


def test_the_check_fails_on_the_pre_fix_payload(capsys):
    rc = _check([
        "--only", "/api/channels",
        "--payload", f"/api/channels={os.path.join(TESTDATA, 'channels_payload_pre_fix.json')}",
    ])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Use Hermes from Slack via Socket Mode" in out


def test_the_check_passes_on_the_mapped_payload(tmp_path, monkeypatch, capsys):
    out = _list_channels(UPSTREAM, monkeypatch)
    path = tmp_path / "after.json"
    path.write_text(json.dumps(out), encoding="utf-8")
    rc = _check(["--only", "/api/channels", "--payload", f"/api/channels={path}"])
    assert rc == 0, capsys.readouterr().out


def test_an_unreachable_console_is_not_a_pass(capsys):
    """A check that could not look must not report clean."""
    rc = _check(["--base", "http://127.0.0.1:9", "--only", "/api/channels"])
    assert rc == 2
    assert "did not look" in capsys.readouterr().err


def test_accepted_paths_match_list_rows():
    """The bug that made three ACCEPTED entries silently dead.

    fnmatch reads ``[*]`` as a character class holding a literal asterisk, so
    ``.services[*].target`` — the obvious spelling — matches nothing, and the
    check reported real acceptances as failures. Indices are blanked instead.
    """
    import importlib.util

    path = os.path.join(os.path.dirname(HERE), "tools", "check_client_copy.py")
    spec = importlib.util.spec_from_file_location("check_client_copy_paths", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._blank_indices(".services[0].target") == ".services[].target"
    assert module._blank_indices(".a[0].b[12].c") == ".a[].b[].c"
    assert module._accepts(
        "/api/health/services", ".services[3].target", "http://hermes-gateway:8642/health"
    ) is not None
    # And a genuinely new leak on the same endpoint still fails.
    assert module._accepts(
        "/api/health/services", ".services[3].note", "Hermes says hello"
    ) is None


def test_every_accepted_entry_is_reachable():
    """An ACCEPTED entry that matches nothing is a belief nobody checked.

    Each entry is compiled and given the value it was written for, so a typo in
    a path or a regex shows up here rather than as a silent gap years later.
    """
    import importlib.util
    import re as _re

    path = os.path.join(os.path.dirname(HERE), "tools", "check_client_copy.py")
    spec = importlib.util.spec_from_file_location("check_client_copy_entries", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for entry in module.ACCEPTED:
        _re.compile(entry.value)          # a broken regex is a dead entry
        assert entry.reason.strip()
        assert "[*]" not in entry.path, entry   # the fnmatch trap, banned outright


# --- The other wire path on the same page ----------------------------------


def test_upstream_error_text_is_mapped_too():
    """`detail` forwarded from the gateway is rendered in the save toast.

    Task 15 rewrote the *fallbacks* in this module — the branch taken when
    upstream sends no detail of its own. When it does send one, that string is
    what the client reads, and it is upstream's prose. Same wire path, same
    treatment.
    """
    from .hermes_api import detail_of

    class _Resp:
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            if self._payload is None:
                raise ValueError("not json")
            return self._payload

    assert detail_of(
        _Resp({"detail": "Hermes rejected the token."}), "fallback"
    ) == "Steward rejected the token."
    assert detail_of(
        _Resp({"detail": [{"msg": "Hermes could not parse it"}]}), "fallback"
    ) == "Steward could not parse it"


def test_channel_save_error_is_mapped(monkeypatch):
    class _Resp:
        status_code = 400
        text = ""

        def json(self):
            return {"detail": "Hermes will not enable telegram without a token."}

    async def _request(method, path, **kwargs):
        return _Resp()

    monkeypatch.setattr(C._client, "request", _request)
    with pytest.raises(C.ChannelsUnavailable) as exc:
        asyncio.run(C.update_channel("telegram", True, {}, []))
    assert "Hermes" not in str(exc.value)
    assert str(exc.value) == "Steward will not enable telegram without a token."
