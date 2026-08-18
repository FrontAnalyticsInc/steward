"""Tests for the dashboard half of the integrations settings page.

The workflows service is not exercised here — it owns its own tests. What is
asserted is the reading this dashboard does on top: the assistant's token file,
and the difference between "the workflows service says nothing is configured"
and "the workflows service did not answer", which are the same empty list on
screen and only one of which means a container needs looking at.
"""

from __future__ import annotations

import json
import os
import time

from . import settings_integrations as S


def _write_token(tmp_path, **fields):
    d = tmp_path / S.GMAIL_MCP_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / S.GMAIL_MCP_CREDENTIALS).write_text(json.dumps(fields))
    return str(tmp_path)


def test_no_token_file_means_not_signed_in(tmp_path):
    row = S._assistant_identity(str(tmp_path))
    assert row["signed_in"] is False
    assert row["needs_reauth"] is False  # not signed in is not the same alarm


def test_an_expired_access_token_with_a_refresh_token_needs_nothing(tmp_path):
    # A Google access token lasts an hour, so this file is expired most of the
    # time and the MCP server refreshes it on the next call. Reporting that as
    # a problem would raise an alarm on almost every page load.
    db = _write_token(
        tmp_path,
        scope="https://www.googleapis.com/auth/gmail.modify",
        expiry_date=(time.time() - 60) * 1000,
        refresh_token="r",
    )
    row = S._assistant_identity(db)
    assert row["signed_in"] is True
    assert row["expired"] is True
    assert row["needs_reauth"] is False


def test_an_expired_token_with_nothing_to_refresh_from_needs_a_person(tmp_path):
    db = _write_token(
        tmp_path,
        scope="https://www.googleapis.com/auth/gmail.modify",
        expiry_date=(time.time() - 60) * 1000,
    )
    assert S._assistant_identity(db)["needs_reauth"] is True


def test_millisecond_expiry_is_converted_to_seconds(tmp_path):
    # Mixing the two puts the expiry in the year 57000, which reads as a token
    # that never expires — the opposite of the truth.
    expiry_ms = (time.time() + 3600) * 1000
    db = _write_token(tmp_path, expiry_date=expiry_ms, refresh_token="r")
    row = S._assistant_identity(db)
    assert abs(row["expires_at"] - expiry_ms / 1000.0) < 1
    assert row["expired"] is False


def test_scopes_are_shortened_to_what_a_person_reads(tmp_path):
    db = _write_token(
        tmp_path,
        scope=("https://www.googleapis.com/auth/gmail.modify "
               "https://www.googleapis.com/auth/gmail.settings.basic"),
        refresh_token="r",
    )
    assert S._assistant_identity(db)["scopes"] == ["gmail.modify", "gmail.settings.basic"]


def test_the_token_itself_is_never_returned(tmp_path):
    db = _write_token(
        tmp_path,
        access_token="ya29-secret-access-token",
        refresh_token="1//secret-refresh-token",
        scope="https://www.googleapis.com/auth/gmail.modify",
    )
    blob = json.dumps(S._assistant_identity(db))
    assert "ya29-secret-access-token" not in blob
    assert "1//secret-refresh-token" not in blob


def test_a_corrupt_token_file_is_not_a_signed_in_assistant(tmp_path):
    d = tmp_path / S.GMAIL_MCP_DIR
    d.mkdir(parents=True)
    (d / S.GMAIL_MCP_CREDENTIALS).write_text("{ this is not json")
    assert S._assistant_identity(str(tmp_path))["signed_in"] is False


def test_an_unreachable_workflows_service_says_so_rather_than_reporting_nothing(tmp_path):
    # Port 1 is reserved and nothing listens on it, so this exercises the real
    # failure path rather than a mocked one.
    out = S.build(db_dir=str(tmp_path), src_dir=str(tmp_path), workflows_url="http://127.0.0.1:1")
    assert out["workflows"]["reachable"] is False
    assert out["workflows"]["error"]
    assert out["workflow_access"] == []
    # The assistant's identity is read locally, so it survives the workflows
    # service being down — the section is still worth showing.
    assert out["identities"] and out["identities"][0]["key"] == "assistant_mail"


def test_no_workflows_url_is_handled_like_an_unreachable_one(tmp_path):
    out = S.build(db_dir=str(tmp_path), src_dir=str(tmp_path), workflows_url="")
    assert out["workflows"]["reachable"] is False
