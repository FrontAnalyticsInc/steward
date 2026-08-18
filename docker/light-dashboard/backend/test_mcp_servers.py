"""Tests for the parts of the MCP connections proxy that are not the proxy.

Everything that talks to Hermes is deliberately untested here: those functions
are a URL, a status check and a forwarded body, and a test of them would assert
that httpx was called, not that anything is true. What is worth pinning down is
the logic this dashboard added on top — what a connection's row *says*, and
whether the page's claim about reloading matches the profile's config.

Written as plain pytest functions rather than unittest.TestCase, unlike
test_integrations.py and test_adk_live.py next to it. Those two say in their
docstrings that they use the standard library because the dashboard has no test
runner; it has one now (see ../pytest.ini). They are left as they are — pytest
runs a TestCase unchanged, so converting them would be churn with no result.

The one thing that follows from the difference: `python3 -m unittest discover`
collects those two and not this module, because unittest sees only TestCase
subclasses. `pytest` collects all three. That is the trade for using tmp_path
instead of hand-rolling tempfile teardown, and it is why pytest is now the
documented way to run the suite.
"""

from __future__ import annotations

import json
import os
import time

from . import mcp_servers as M
from .hermes_api import detail_of


# --- What a row says it connects to ---------------------------------------


def test_http_server_targets_its_url():
    assert M._target({"url": "https://mcp.attio.com/mcp"}) == "https://mcp.attio.com/mcp"


def test_stdio_server_targets_its_whole_command_line():
    server = {"command": "node", "args": ["/opt/data/gmail-mcp/dist/index.js"]}
    assert M._target(server) == "node /opt/data/gmail-mcp/dist/index.js"


def test_placeholders_are_shown_verbatim():
    # ${HERMES_HOME} resolves inside the gateway container and not for a
    # host-side CLI. Expanding it here would paper over that difference on the
    # one screen someone reads when a connection is not working.
    server = {"command": "node", "args": ["${HERMES_HOME}/gmail-mcp/index.js"]}
    assert "${HERMES_HOME}" in M._target(server)


def test_a_server_with_neither_url_nor_command_has_no_target():
    # Malformed config is one blank cell, never a traceback that costs the page.
    assert M._target({"name": "broken"}) == ""


# --- Authentication state --------------------------------------------------


def test_oauth_server_with_a_token_reads_as_signed_in(tmp_path):
    token_dir = tmp_path / "mcp-tokens"
    token_dir.mkdir()
    (token_dir / "attio.json").write_text(
        json.dumps({"expires_at": time.time() + 3600, "refresh_token": "r"})
    )
    row = M._decorate({"name": "attio", "url": "https://x/mcp", "auth": "oauth"}, str(token_dir))
    assert row["oauth"]["authenticated"] is True
    assert row["oauth"]["expired"] is False
    assert row["oauth"]["has_refresh_token"] is True


def test_oauth_server_without_a_token_reads_as_not_signed_in(tmp_path):
    row = M._decorate({"name": "attio", "url": "https://x/mcp", "auth": "oauth"}, str(tmp_path))
    assert row["oauth"]["authenticated"] is False


def test_a_server_that_needs_no_oauth_is_not_reported_as_unauthenticated(tmp_path):
    # The distinction the page depends on: None means "not applicable", and a
    # stdio server carrying its own credential paths is fine, not broken.
    row = M._decorate({"name": "gmail", "command": "node", "args": []}, str(tmp_path))
    assert row["oauth"]["authenticated"] is None


def test_decorating_never_drops_what_hermes_reported():
    row = M._decorate({"name": "graphiti", "url": "http://x/mcp/", "enabled": False}, "/nonexistent")
    assert row["name"] == "graphiti"
    assert row["enabled"] is False
    assert row["target"] == "http://x/mcp/"


# --- Does a saved change reach the running conversation? --------------------


def _write_config(dirpath, body):
    with open(os.path.join(dirpath, "config.yaml"), "w", encoding="utf-8") as fh:
        fh.write(body)


def test_auto_reload_is_the_default_when_config_says_nothing(tmp_path):
    _write_config(tmp_path, "model:\n  default: gpt\n")
    assert M.reload_policy(str(tmp_path))["auto_reload"] is True


def test_auto_reload_off_is_reported_as_off(tmp_path):
    _write_config(tmp_path, "mcp:\n  auto_reload_on_config_change: false\n")
    assert M.reload_policy(str(tmp_path))["auto_reload"] is False


def test_a_missing_config_does_not_claim_a_reload_policy_it_cannot_read(tmp_path):
    # Defaults rather than raising: the connection list itself came from Hermes
    # and is still worth showing when the local config is unreadable.
    assert M.reload_policy(str(tmp_path / "nope"))["auto_reload"] is True


def test_malformed_config_falls_back_to_the_default(tmp_path):
    _write_config(tmp_path, "mcp: [this is not a mapping\n")
    assert M.reload_policy(str(tmp_path))["auto_reload"] is True


# --- Forwarding Hermes's own error text ------------------------------------


class _Resp:
    """The two things detail_of touches on an httpx response."""

    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_hermes_own_reason_is_forwarded():
    # "Server 'gmail' already exists" is the difference between a settings page
    # that says what is wrong and one that says 400.
    assert detail_of(_Resp({"detail": "Server 'gmail' already exists"}), "fallback") == (
        "Server 'gmail' already exists"
    )


def test_validation_errors_forward_their_first_message():
    resp = _Resp({"detail": [{"msg": "field required", "loc": ["body", "name"]}]})
    assert detail_of(resp, "fallback") == "field required"


def test_a_non_json_body_forwards_its_text():
    assert detail_of(_Resp(None, text="  Bad Gateway  "), "fallback") == "Bad Gateway"


def test_an_empty_body_falls_back():
    assert detail_of(_Resp(None, text="   "), "fallback") == "fallback"
    assert detail_of(_Resp({"detail": ""}), "fallback") == "fallback"
