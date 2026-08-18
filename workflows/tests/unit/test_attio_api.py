"""Tests for the read-only Attio client.

Attio returns attributes as arrays of time-versioned values rather than scalars,
so most of the risk here is reading a superseded value and briefing Alton on
someone's previous job.
"""

from __future__ import annotations

from app import attio_api as A


class TestCurrentValue:
    def test_the_value_still_active_wins_over_the_old_one(self):
        values = [
            {"value": "Head of Ops", "active_from": "2023-01-01", "active_until": "2025-06-01"},
            {"value": "VP Engineering", "active_from": "2025-06-01", "active_until": None},
        ]
        assert A._first_value(values) == "VP Engineering"

    def test_a_lone_historical_value_is_still_returned(self):
        """Better a stale title than none — but only when there is no current one."""
        values = [{"value": "Head of Ops", "active_until": "2025-06-01"}]
        assert A._first_value(values) == "Head of Ops"

    def test_a_plain_string_passes_through(self):
        assert A._first_value("Acme") == "Acme"

    def test_empty_and_missing_yield_nothing(self):
        assert A._first_value([]) is None
        assert A._first_value(None) is None

    def test_a_nested_option_is_unwrapped(self):
        values = [{"option": {"title": "Customer"}, "active_until": None}]
        assert A._first_value(values) == "Customer"

    def test_a_full_name_field_is_recognised(self):
        values = [{"full_name": "Eric Kebschull", "active_until": None}]
        assert A._first_value(values) == "Eric Kebschull"


class TestInteractions:
    """Often the only populated part of a record, and the most useful part."""

    def test_an_interaction_reads_as_type_and_date(self):
        values = [{
            "interaction_type": "email",
            "interacted_at": "2026-08-06T15:58:51.000000000Z",
            "active_until": None,
        }]
        assert A._first_value(values) == "email on 2026-08-06"

    def test_an_interaction_without_a_type_still_dates(self):
        values = [{"interacted_at": "2026-08-10T15:00:00Z", "active_until": None}]
        assert A._first_value(values) == "interaction on 2026-08-10"


class TestReferences:
    """A UUID in a briefing is noise a model may read meaning into."""

    def test_a_bare_uuid_is_never_emitted(self):
        values = [{
            "attribute_type": "record-reference",
            "target_object": "companies",
            "target_record_id": "826c6bb1-906b-4825-8949-1fc32b7641a8",
            "active_until": None,
        }]
        assert A._first_value(values) is None

    def test_an_unresolvable_reference_is_dropped_not_guessed(self, monkeypatch):
        monkeypatch.setattr(A, "_REFERENCE_NAMES", {})
        monkeypatch.setattr(A, "_get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
        values = [{
            "attribute_type": "record-reference",
            "target_object": "companies",
            "target_record_id": "abc",
            "active_until": None,
        }]
        assert A._reference_name(values) is None

    def test_a_reference_resolves_to_the_records_name(self, monkeypatch):
        monkeypatch.setattr(A, "_REFERENCE_NAMES", {})
        monkeypatch.setattr(
            A, "_get",
            lambda *a, **k: {"data": {"values": {"name": [{"value": "Northwind Strategies",
                                                           "active_until": None}]}}},
        )
        values = [{
            "attribute_type": "record-reference",
            "target_object": "companies",
            "target_record_id": "abc",
            "active_until": None,
        }]
        assert A._reference_name(values) == "Northwind Strategies"

    def test_the_second_lookup_is_cached(self, monkeypatch):
        calls = []
        monkeypatch.setattr(A, "_REFERENCE_NAMES", {})
        monkeypatch.setattr(
            A, "_get",
            lambda *a, **k: (calls.append(1), {"data": {"values": {"name": "Acme"}}})[1],
        )
        values = [{
            "attribute_type": "record-reference",
            "target_object": "companies",
            "target_record_id": "abc",
            "active_until": None,
        }]
        A._reference_name(values)
        A._reference_name(values)
        assert len(calls) == 1


class TestSummarize:
    def test_only_the_useful_fields_survive(self):
        record = {
            "web_url": "https://app.attio.com/x/person/1",
            "values": {
                "name": [{"full_name": "Eric Kebschull", "active_until": None}],
                "job_title": [{"value": "Founder", "active_until": None}],
                "internal_scoring_hack": [{"value": "42", "active_until": None}],
            },
        }
        out = A.summarize_person(record)
        assert out["name"] == "Eric Kebschull"
        assert out["job_title"] == "Founder"
        assert "internal_scoring_hack" not in out
        assert out["web_url"].startswith("https://app.attio.com")

    def test_a_record_with_nothing_useful_summarizes_to_nothing(self):
        assert A.summarize_person({"values": {}}) == {}

    def test_long_values_are_bounded(self):
        record = {"values": {"description": [{"value": "x" * 900, "active_until": None}]}}
        assert len(A.summarize_person(record)["description"]) == 300


class TestConfigured:
    def test_configured_follows_the_token(self, monkeypatch):
        monkeypatch.delenv(A.API_KEY, raising=False)
        assert A.configured() is False
        monkeypatch.setenv(A.API_KEY, "sk-test")
        assert A.configured() is True

    def test_find_person_without_a_token_raises_rather_than_silently_skipping(
        self, monkeypatch
    ):
        monkeypatch.delenv(A.API_KEY, raising=False)
        try:
            A.find_person("x@y.com")
        except RuntimeError as exc:
            assert A.API_KEY in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected RuntimeError")


class TestOpenTasks:
    """Tasks are read fresh every run and never written to memory."""

    def test_a_task_reduces_to_content_and_deadline(self):
        out = A._task_summary({
            "content_plaintext": "Follow up on pricing",
            "deadline_at": "2026-08-10T00:00:00Z",
            "is_completed": False,
        })
        assert out["content"] == "Follow up on pricing"
        assert out["deadline"] == "2026-08-10"

    def test_a_task_with_no_deadline_still_counts(self):
        out = A._task_summary({"content_plaintext": "Send the deck"})
        assert out["content"] == "Send the deck"
        assert "deadline" not in out

    def test_assignee_is_recorded_but_never_filters(self):
        """Whoever owns it, it is still owed to this person."""
        out = A._task_summary({
            "content_plaintext": "x",
            "assignees": [
                {"referenced_actor_id": "a" * 36},
                {"referenced_actor_id": "b" * 36},
            ],
        })
        assert out["assignee_count"] == "2"

    def test_an_empty_task_is_dropped(self, monkeypatch):
        monkeypatch.setattr(A, "_get", lambda *a, **k: {"data": [{"content_plaintext": "  "}]})
        assert A.open_tasks("rec-1") == []

    def test_no_record_id_means_no_request(self, monkeypatch):
        called = []
        monkeypatch.setattr(A, "_get", lambda *a, **k: called.append(1) or {})
        assert A.open_tasks("") == []
        assert called == []

    def test_the_query_asks_the_api_to_exclude_completed(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(A, "_get", lambda path, what: seen.setdefault("path", path) or {"data": []})
        A.open_tasks("rec-1")
        assert "is_completed=false" in seen["path"]
        assert "linked_object=people" in seen["path"]
        assert "linked_record_id=rec-1" in seen["path"]


class TestRecordId:
    def test_the_record_id_survives_the_summary(self):
        rec = {"id": {"record_id": "abc-123"}, "values": {}}
        assert A.summarize_person(rec)["record_id"] == "abc-123"
