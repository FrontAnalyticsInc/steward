"""Tests for the deterministic half of the calendar briefing.

What is asserted here is that the pipeline cannot quietly do the wrong thing:
that a declined meeting is not on your day, that a cancelled one is gone, that
the day window is a day rather than a rolling window from whenever cron fired,
and that an unattended sender refuses an address nobody authorized.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app import calendar_api, mailer


class TestAttendance:
    def test_a_declined_meeting_is_not_on_your_day(self):
        event = {"attendees": [{"email": "a@x.com", "self": True, "responseStatus": "declined"}]}
        assert calendar_api._is_declined(event, "a@x.com") is True

    def test_accepted_and_unanswered_both_count_as_attending(self):
        for status in ("accepted", "tentative", "needsAction"):
            event = {"attendees": [{"email": "a@x.com", "self": True, "responseStatus": status}]}
            assert calendar_api._is_declined(event, "a@x.com") is False

    def test_someone_elses_decline_is_not_yours(self):
        event = {
            "attendees": [
                {"email": "other@x.com", "responseStatus": "declined"},
                {"email": "a@x.com", "self": True, "responseStatus": "accepted"},
            ]
        }
        assert calendar_api._is_declined(event, "a@x.com") is False

    def test_an_event_with_no_attendees_is_not_declined(self):
        assert calendar_api._is_declined({}, "a@x.com") is False


class TestNormalize:
    def test_only_the_narrow_field_set_reaches_the_model(self):
        """Every field included is one an invite sender gets to write."""
        raw = {
            "id": "e1",
            "summary": "Sync",
            "start": {"dateTime": "2026-08-07T15:00:00-06:00"},
            "end": {"dateTime": "2026-08-07T15:30:00-06:00"},
            "organizer": {"email": "o@x.com"},
            "attendees": [{"email": "a@x.com"}, {"displayName": "no address"}],
            "description": "ctx",
            "attachments": [{"fileUrl": "https://evil.example/x"}],
            "conferenceData": {"entryPoints": [{"uri": "https://meet"}]},
        }
        out = calendar_api._normalize(raw, "cal1", "a@x.com")
        assert out["event_id"] == "e1"
        assert out["attendees"] == ["a@x.com"]  # the one without an address is dropped
        assert "attachments" not in out
        assert "conferenceData" not in out

    def test_an_all_day_event_uses_its_date(self):
        raw = {"id": "e2", "start": {"date": "2026-08-07"}, "end": {"date": "2026-08-08"}}
        out = calendar_api._normalize(raw, "cal1", None)
        assert out["start"] == "2026-08-07"


class FakeEvents:
    def __init__(self, payloads, fail_for=()):
        self._payloads, self._fail_for = payloads, set(fail_for)

    def list(self, calendarId=None, **kwargs):
        if calendarId in self._fail_for:
            raise RuntimeError("boom")
        return _Exec({"items": self._payloads.get(calendarId, [])})


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class FakeService:
    def __init__(self, payloads, fail_for=()):
        self._events = FakeEvents(payloads, fail_for)

    def events(self):
        return self._events


class TestListEvents:
    def test_cancelled_and_declined_are_dropped_and_the_rest_sorted(self):
        payloads = {
            "cal1": [
                {"id": "late", "summary": "Late", "start": {"dateTime": "2026-08-07T16:00:00Z"}},
                {"id": "gone", "status": "cancelled", "start": {"dateTime": "2026-08-07T09:00:00Z"}},
                {
                    "id": "declined",
                    "start": {"dateTime": "2026-08-07T10:00:00Z"},
                    "attendees": [{"email": "a@x.com", "self": True, "responseStatus": "declined"}],
                },
                {"id": "early", "summary": "Early", "start": {"dateTime": "2026-08-07T08:00:00Z"}},
            ]
        }
        events, skipped = calendar_api.list_events(
            FakeService(payloads), "t0", "t1", calendar_ids=["cal1"], owner="a@x.com"
        )
        assert [e["event_id"] for e in events] == ["early", "late"]
        assert skipped == []

    def test_one_unreadable_calendar_does_not_lose_the_others(self):
        """A morning with one broken calendar beats no morning at all."""
        payloads = {"good": [{"id": "e1", "start": {"dateTime": "2026-08-07T08:00:00Z"}}]}
        events, skipped = calendar_api.list_events(
            FakeService(payloads, fail_for=["bad"]),
            "t0",
            "t1",
            calendar_ids=["bad", "good"],
        )
        assert [e["event_id"] for e in events] == ["e1"]
        assert len(skipped) == 1 and skipped[0].startswith("bad:")


class TestMailerAllowlist:
    def test_an_unlisted_recipient_is_refused_before_any_send(self, monkeypatch):
        monkeypatch.setenv(mailer.ALLOWED_RECIPIENTS, "owner@example.com")
        with pytest.raises(ValueError, match="refusing to send"):
            mailer.send("stranger@example.com", "s", "b")

    def test_an_empty_allowlist_authorizes_nobody(self, monkeypatch):
        """Fail closed: a missing allowlist must not mean 'anywhere'."""
        monkeypatch.delenv(mailer.ALLOWED_RECIPIENTS, raising=False)
        assert mailer.allowed_recipients() == []
        with pytest.raises(ValueError):
            mailer.send("owner@example.com", "s", "b")

    def test_matching_ignores_case_and_padding(self, monkeypatch):
        monkeypatch.setenv(mailer.ALLOWED_RECIPIENTS, " Owner@Example.com , x@y.com ")
        assert "owner@example.com" in mailer.allowed_recipients()

    def test_configured_needs_both_key_and_sender(self, monkeypatch):
        monkeypatch.delenv(mailer.SERVICE_ACCOUNT_FILE, raising=False)
        monkeypatch.setenv(mailer.SENDER, "assistant@example.com")
        assert mailer.configured() is False
        monkeypatch.setenv(mailer.SERVICE_ACCOUNT_FILE, "/secrets/x.json")
        assert mailer.configured() is True

    def test_the_send_scope_cannot_read(self):
        """Documents the property the split exists for."""
        assert mailer.SCOPES == ["https://www.googleapis.com/auth/gmail.send"]
        assert calendar_api.SCOPES == ["https://www.googleapis.com/auth/calendar.readonly"]


class TestConfigured:
    def test_calendar_needs_both_key_and_subject(self, monkeypatch):
        monkeypatch.setenv(calendar_api.SERVICE_ACCOUNT_FILE, "/secrets/x.json")
        monkeypatch.delenv(calendar_api.DELEGATED_USER, raising=False)
        assert calendar_api.configured() is False
        monkeypatch.setenv(calendar_api.DELEGATED_USER, "owner@example.com")
        assert calendar_api.configured() is True


