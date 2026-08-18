"""The settings snapshot must never carry a secret out of this process.

`app/integration_config.py` exists to be read by a dashboard in another
container, so the interesting property is not what it reports but what it
refuses to: a token value leaving here would land on an unauthenticated
LAN-reachable page. Everything else in the snapshot is an identity, a path or a
boolean, all of which are the point.
"""

from __future__ import annotations

import json

import pytest

from app import integration_config as cfg


@pytest.fixture()
def configured_env(monkeypatch, tmp_path):
    """A fully populated environment, with real files behind the paths."""
    sa = tmp_path / "service-account.json"
    sa.write_text("{}")
    for name, value in {
        "ATTIO_API_KEY": "attio-secret-value-do-not-leak",
        "GMAIL_SERVICE_ACCOUNT_FILE": str(sa),
        "GMAIL_DELEGATED_USER": "reader@example.com",
        "MAILER_SERVICE_ACCOUNT_FILE": str(sa),
        "MAILER_SENDER": "assistant@example.com",
        "MAILER_ALLOWED_RECIPIENTS": "someone@example.com",
        "CALENDAR_SERVICE_ACCOUNT_FILE": str(sa),
        "CALENDAR_DELEGATED_USER": "reader@example.com",
    }.items():
        monkeypatch.setenv(name, value)
    return tmp_path


def test_a_secret_value_never_appears_anywhere_in_the_snapshot(configured_env):
    blob = json.dumps(cfg.snapshot())
    assert "attio-secret-value-do-not-leak" not in blob


def test_a_secret_is_still_reported_as_present(configured_env):
    attio = next(a for a in cfg.snapshot()["access"] if a["key"] == "attio")
    var = next(v for v in attio["vars"] if v["name"] == "ATTIO_API_KEY")
    assert var["set"] is True
    # Presence without a value is the whole contract for this kind.
    assert "value" not in var


def test_identities_and_paths_are_shown_because_that_is_the_point(configured_env):
    gmail = next(a for a in cfg.snapshot()["access"] if a["key"] == "gmail")
    user = next(v for v in gmail["vars"] if v["name"] == "GMAIL_DELEGATED_USER")
    assert user["value"] == "reader@example.com"


def test_a_path_that_points_at_nothing_says_so(monkeypatch):
    # The failure this catches: every credential path in the compose file
    # defaults to empty, so a typo reads as configured until the call needs it.
    monkeypatch.setenv("GMAIL_SERVICE_ACCOUNT_FILE", "/no/such/file.json")
    monkeypatch.setenv("GMAIL_DELEGATED_USER", "reader@example.com")
    gmail = next(a for a in cfg.snapshot()["access"] if a["key"] == "gmail")
    sa = next(v for v in gmail["vars"] if v["name"] == "GMAIL_SERVICE_ACCOUNT_FILE")
    assert sa["set"] is True
    assert sa["file_present"] is False


def test_an_unset_credential_is_not_reported_as_configured(monkeypatch):
    for name in ("ATTIO_API_KEY",):
        monkeypatch.delenv(name, raising=False)
    attio = next(a for a in cfg.snapshot()["access"] if a["key"] == "attio")
    assert attio["configured"] is False


def test_an_empty_recipient_allowlist_is_an_empty_list_not_a_blank_string(monkeypatch):
    # The UI turns this into "unattended mail goes nowhere", which is the
    # correct default; a [""] would render as one nameless allowed recipient.
    monkeypatch.setenv("MAILER_ALLOWED_RECIPIENTS", "")
    mail = next(o for o in cfg.snapshot()["outputs"] if o["key"] == "mail")
    assert mail["recipients"] == []


def test_every_section_is_present_even_with_nothing_configured(monkeypatch):
    for name in (
        "ATTIO_API_KEY", "GMAIL_SERVICE_ACCOUNT_FILE", "GMAIL_DELEGATED_USER",
        "GMAIL_TOKEN_FILE", "MAILER_SERVICE_ACCOUNT_FILE", "MAILER_SENDER",
        "CALENDAR_SERVICE_ACCOUNT_FILE", "CALENDAR_DELEGATED_USER",
    ):
        monkeypatch.delenv(name, raising=False)
    snap = cfg.snapshot()
    # A blank page and an unreachable service must stay distinguishable, so the
    # shape is constant and only the contents go empty.
    assert {"access", "outputs", "identities", "source"} <= set(snap)
    assert snap["access"] and snap["outputs"] and snap["identities"]
    assert all(a["configured"] is False for a in snap["access"])
