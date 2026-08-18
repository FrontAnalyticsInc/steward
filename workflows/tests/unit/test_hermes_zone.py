"""The workflows-side display timezone.

This container cannot read ~/.hermes/config.yaml (provider credentials live
there, and this is the container that reads untrusted email), so it asks the
gateway. Every step of the resolution has to fail open: a workflow must never
crash, or hang on a network timeout, because it could not determine a display
preference.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import hermes_zone


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    hermes_zone.reset_cache()
    yield
    hermes_zone.reset_cache()


def _no_gateway():
    """Patch out the gateway probe — the common case in tests and offline."""
    return patch.object(hermes_zone, "_probe_gateway", return_value="")


class TestResolution:
    def test_env_wins(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", "America/Denver")
        with _no_gateway():
            assert hermes_zone.zone_name() == "America/Denver"

    def test_falls_back_to_gateway(self):
        with patch.object(hermes_zone, "_probe_gateway", return_value="Asia/Tokyo"):
            assert hermes_zone.zone_name() == "Asia/Tokyo"

    def test_falls_back_to_tz_then_utc(self, monkeypatch):
        with _no_gateway():
            monkeypatch.setenv("TZ", "Europe/Paris")
            assert hermes_zone.zone_name() == "Europe/Paris"

        hermes_zone.reset_cache()
        monkeypatch.delenv("TZ")
        with _no_gateway():
            assert hermes_zone.zone_name() == "UTC"

    def test_resolution_is_cached(self):
        with patch.object(hermes_zone, "_probe_gateway", return_value="Asia/Tokyo") as probe:
            hermes_zone.zone_name()
            hermes_zone.zone_name()
        assert probe.call_count == 1, "the gateway must be probed once per process"

    def test_invalid_zone_falls_back_to_utc(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", "Not/AZone")
        with _no_gateway():
            assert hermes_zone.zone().key == "UTC"


class TestGatewayProbe:
    def _resp(self, payload):
        class _Ctx:
            def __enter__(self_inner):
                class _R:
                    def read(self_r):
                        return json.dumps(payload).encode()

                return _R()

            def __exit__(self_inner, *a):
                return False

        return _Ctx()

    def test_reads_the_published_name(self):
        with patch.object(
            hermes_zone.urllib.request,
            "urlopen",
            return_value=self._resp({"timezone": {"name": "America/Denver"}}),
        ):
            assert hermes_zone._probe_gateway() == "America/Denver"

    def test_unreachable_gateway_is_not_fatal(self):
        with patch.object(
            hermes_zone.urllib.request, "urlopen", side_effect=OSError("refused")
        ):
            assert hermes_zone._probe_gateway() == ""

    def test_malformed_payload_is_not_fatal(self):
        with patch.object(
            hermes_zone.urllib.request,
            "urlopen",
            return_value=self._resp({"status": "ok"}),
        ):
            assert hermes_zone._probe_gateway() == ""


class TestFormatting:
    def test_fmt_converts_and_labels(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", "America/Denver")
        with _no_gateway():
            # 14:00Z on 2026-11-01 is 07:00 MST.
            rendered = hermes_zone.fmt(datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc))
        assert rendered == "2026-11-01 07:00 MST"

    def test_naive_input_is_read_as_utc(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", "UTC")
        with _no_gateway():
            assert hermes_zone.fmt(datetime(2026, 11, 1, 14, 0)) == "2026-11-01 14:00 UTC"

    def test_describe_now_carries_abbreviation_and_iana_name(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", "America/Denver")
        with _no_gateway():
            described = hermes_zone.describe_now()
        assert "(America/Denver)" in described
        assert "MDT" in described or "MST" in described


class TestHumanizeAge:
    REF = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    @pytest.mark.parametrize(
        "delta,expected",
        [
            (timedelta(seconds=10), "just now"),
            (timedelta(minutes=5), "5 minutes ago"),
            (timedelta(minutes=1, seconds=40), "1 minute ago"),
            (timedelta(hours=3), "3 hours ago"),
            (timedelta(hours=1), "1 hour ago"),
            (timedelta(days=3), "3 days ago"),
            (timedelta(days=1), "1 day ago"),
            (timedelta(days=45), "1 month ago"),
            (timedelta(days=400), "1 year ago"),
        ],
    )
    def test_plain_words(self, delta, expected):
        assert hermes_zone.humanize_age(self.REF - delta, reference=self.REF) == expected

    def test_future_timestamps_are_named_not_negative(self):
        """A sender with a skewed clock must not produce "-3 days ago"."""
        future = self.REF + timedelta(days=2)
        assert hermes_zone.humanize_age(future, reference=self.REF) == "in the future"

    def test_naive_input_is_read_as_utc(self):
        naive = datetime(2026, 11, 1, 9, 0)
        assert hermes_zone.humanize_age(naive, reference=self.REF) == "3 hours ago"
