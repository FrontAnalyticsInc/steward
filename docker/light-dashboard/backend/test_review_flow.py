"""The flow diagrams, and the one property that makes them trustworthy.

A Sankey whose columns do not balance is a picture that quietly asserts
something false, and every way of getting there is easy: a partition that turns
out not to be one, a window boundary an item straddles, a field that is absent
rather than zero. So most of this file is one assertion — inflow equals outflow
at every interior node — applied to the awkward cases rather than the happy one.
"""

from __future__ import annotations

import datetime
import json
import os

import pytest

from . import review_flow


UTC = datetime.timezone.utc


def _iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(started, *, examined=30, reached=10, extra=None):
    metrics = {
        "touched": {"email": reached},
        "produced": {},
        "extra": {"candidates_examined": examined, **(extra or {})},
    }
    return {"run_id": f"r{started}", "started_at": started, "metrics": metrics}


@pytest.fixture
def traces(tmp_path):
    """A trace directory the loader will read, with a writer for it."""
    root = tmp_path / "traces" / review_flow.TRIAGE_APP
    root.mkdir(parents=True)

    def write(runs):
        day = datetime.datetime.now(UTC).strftime("%Y-%m-%d")
        with open(root / f"{day}.jsonl", "a", encoding="utf-8") as fh:
            for run in runs:
                fh.write(json.dumps(run) + "\n")

    return str(tmp_path), write


@pytest.fixture
def queue(tmp_path):
    """The six queue directories, with a writer that places items."""
    dirs = {}
    for state in ("pending", "approved", "rejected", "executing", "executed", "failed"):
        path = tmp_path / "q" / state
        path.mkdir(parents=True)
        dirs[state] = str(path)

    def place(state, item):
        name = f"{item.get('created_at', 'x')}--{item['id']}.json"
        with open(os.path.join(dirs[state], name), "w", encoding="utf-8") as fh:
            json.dump(item, fh)

    return dirs, place


def _item(item_id, *, created, decided=None, decision="approve", executor=None):
    item = {"id": item_id, "created_at": _iso(created)}
    if decided is not None:
        item["decided_at"] = _iso(decided)
        item["decision"] = decision
    if executor:
        item["execution"] = {"state": "done", "executor": executor}
    return item


def _links(diagram):
    return {(l["source"], l["target"]): l["value"] for l in diagram["links"]}


def _total_out(diagram, node):
    return sum(v for (s, _), v in _links(diagram).items() if s == node)


# --- the property -------------------------------------------------------------


class TestEveryDiagramBalances:
    def test_the_triage_diagram_balances(self, traces):
        state_dir, write = traces
        now = _iso(datetime.datetime.now(UTC))
        write([
            _run(now, examined=30, reached=10, extra={
                "disposition_check_later": 7, "disposition_keep": 3,
                "action_filed": 7, "action_left_in_inbox": 3,
                "action_draft_queued": 2, "held_by_confidence_count": 0,
                "undecided_count": 0,
            }),
        ])
        diagram = review_flow.triage_diagram(state_dir, days=30)
        assert review_flow.imbalances(diagram) == []

    def test_it_balances_when_the_model_wanted_to_file_but_could_not(self, traces):
        # The held-by-confidence case. These are check_later messages that were
        # left in the inbox, and the naive drawing (check_later -> filed) loses
        # them: the disposition column would be 7 and the outcome column 5.
        state_dir, write = traces
        write([
            _run(_iso(datetime.datetime.now(UTC)), examined=12, reached=10, extra={
                "disposition_check_later": 7, "disposition_keep": 3,
                "action_filed": 5, "action_left_in_inbox": 5,
                "held_by_confidence_count": 2, "undecided_count": 0,
            }),
        ])
        diagram = review_flow.triage_diagram(state_dir, days=30)
        assert review_flow.imbalances(diagram) == []
        assert _links(diagram)[("disp_check_later", "out_filed")] == 5
        assert _links(diagram)[("disp_check_later", "out_inbox")] == 2

    def test_it_balances_when_a_message_got_no_verdict(self, traces):
        # Undecided is neither a disposition nor an action. If it is dropped,
        # the reached node emits fewer than it received.
        state_dir, write = traces
        write([
            _run(_iso(datetime.datetime.now(UTC)), examined=10, reached=10, extra={
                "disposition_keep": 8, "action_left_in_inbox": 10,
                "undecided_count": 2,
            }),
        ])
        diagram = review_flow.triage_diagram(state_dir, days=30)
        assert review_flow.imbalances(diagram) == []
        assert _links(diagram)[("reached", "disp_undecided")] == 2

    def test_it_balances_when_only_some_runs_carry_a_breakdown(self, traces):
        # Regression. The unit tests passed on this shape while the diagram was
        # wrong: every run contributes to `reached`, but only instrumented runs
        # can say what became of their messages, so the node received far more
        # than it emitted. Real data found it — 210 in, 30 out.
        state_dir, write = traces
        now = _iso(datetime.datetime.now(UTC))
        write([
            _run(now, examined=10, reached=10, extra={
                "disposition_keep": 10, "action_left_in_inbox": 10,
            }),
            _run(now, examined=10, reached=10),
            _run(now, examined=10, reached=10),
        ])
        diagram = review_flow.triage_diagram(state_dir, days=30)
        assert review_flow.imbalances(diagram) == []
        assert _links(diagram)[("reached", "disp_unrecorded")] == 20
        assert _total_out(diagram, "reached") == 30

    def test_the_review_diagram_balances(self, queue):
        dirs, place = queue
        now = datetime.datetime.now(UTC)
        place("pending", _item("a", created=now - datetime.timedelta(days=1)))
        place("rejected", _item("b", created=now - datetime.timedelta(days=2),
                                decided=now, decision="reject"))
        place("executed", _item("c", created=now - datetime.timedelta(days=1),
                                decided=now, executor="send"))
        diagram = review_flow.review_diagram(dirs, days=30)
        assert review_flow.imbalances(diagram) == []


# --- the window, which is where an async queue goes wrong ---------------------


class TestTheWindowBoundary:
    def test_an_item_from_before_the_window_is_carried_in(self, queue):
        dirs, place = queue
        now = datetime.datetime.now(UTC)
        # Queued 40 days ago, decided today: it moved during the window, but it
        # did not arrive during it. Counting it as "queued in window" would
        # overstate intake.
        place("executed", _item("old", created=now - datetime.timedelta(days=40),
                                decided=now, executor="send"))
        diagram = review_flow.review_diagram(dirs, days=7)
        assert _links(diagram) == {("carried_in", "out_sent"): 1}

    def test_an_item_decided_before_the_window_is_absent(self, queue):
        dirs, place = queue
        now = datetime.datetime.now(UTC)
        place("executed", _item("older", created=now - datetime.timedelta(days=40),
                                decided=now - datetime.timedelta(days=30),
                                executor="send"))
        diagram = review_flow.review_diagram(dirs, days=7)
        assert diagram["links"] == []

    def test_undecided_items_are_shown_rather_than_dropped(self, queue):
        # The whole reason the diagram balances. An item queued in the window
        # and not yet decided has to go somewhere, and "somewhere" is a node
        # the reader can see — otherwise intake silently exceeds outcomes.
        dirs, place = queue
        now = datetime.datetime.now(UTC)
        for i in range(3):
            place("pending", _item(f"p{i}", created=now - datetime.timedelta(hours=i)))
        diagram = review_flow.review_diagram(dirs, days=7)
        assert _links(diagram) == {("queued_in", "out_pending"): 3}
        assert review_flow.imbalances(diagram) == []


class TestAnItemIsCountedOnce:
    def test_an_executed_item_still_in_approved_is_not_double_counted(self, queue):
        # approved/ is an immutable ledger and is never deleted from, so a sent
        # item is present in both directories. Counting the directories rather
        # than the items would report two sends for one email.
        dirs, place = queue
        now = datetime.datetime.now(UTC)
        item = _item("dup", created=now - datetime.timedelta(hours=2),
                     decided=now, executor="send")
        place("approved", item)
        place("executed", item)
        diagram = review_flow.review_diagram(dirs, days=7)
        assert _links(diagram) == {("queued_in", "out_sent"): 1}

    def test_a_send_and_a_draft_land_in_different_outcomes(self, queue):
        dirs, place = queue
        now = datetime.datetime.now(UTC)
        place("executed", _item("s", created=now, decided=now, executor="send"))
        place("executed", _item("d", created=now, decided=now, executor="create_draft"))
        links = _links(review_flow.review_diagram(dirs, days=7))
        assert links[("queued_in", "out_sent")] == 1
        assert links[("queued_in", "out_draft_created")] == 1


# --- honesty about missing data ----------------------------------------------


class TestItNeverInventsABreakdown:
    def test_runs_without_a_breakdown_get_no_disposition_column(self, traces):
        # These fields postdate the pipeline. Drawing a breakdown from the runs
        # that have them, sized as though it covered all of them, is the failure
        # mode this guards.
        state_dir, write = traces
        now = _iso(datetime.datetime.now(UTC))
        write([_run(now, examined=30, reached=10)])
        diagram = review_flow.triage_diagram(state_dir, days=30)
        assert not any(l["target"].startswith("disp_") for l in diagram["links"])
        assert diagram["runs_with_breakdown"] == 0
        assert any("predate" in n for n in diagram["notes"])

    def test_a_partial_window_reports_its_sample_size(self, traces):
        state_dir, write = traces
        now = _iso(datetime.datetime.now(UTC))
        write([
            _run(now, examined=10, reached=10, extra={
                "disposition_keep": 10, "action_left_in_inbox": 10,
            }),
            _run(now, examined=10, reached=10),
        ])
        diagram = review_flow.triage_diagram(state_dir, days=30)
        assert diagram["runs"] == 2
        assert diagram["runs_with_breakdown"] == 1
        assert any("1 of 2 runs" in n for n in diagram["notes"])

    def test_drafting_is_a_note_and_never_a_branch(self, traces):
        # Drafting overlaps with where a message ended up: on a real run the
        # dispositions summed to 10 and the actions to 12. Branching on it would
        # draw those 2 twice.
        state_dir, write = traces
        write([
            _run(_iso(datetime.datetime.now(UTC)), examined=10, reached=10, extra={
                "disposition_check_later": 7, "disposition_keep": 3,
                "action_filed": 7, "action_left_in_inbox": 3,
                "action_draft_queued": 2,
            }),
        ])
        diagram = review_flow.triage_diagram(state_dir, days=30)
        assert not any("draft" in l["target"] for l in diagram["links"])
        assert _total_out(diagram, "reached") == 10
        assert any("orthogonal" in n for n in diagram["notes"])

    def test_an_empty_window_is_empty_rather_than_wrong(self, traces, queue):
        state_dir, _ = traces
        dirs, _place = queue
        result = review_flow.flow(state_dir, dirs, days=7)
        for diagram in result["diagrams"]:
            assert diagram["links"] == []
            assert review_flow.imbalances(diagram) == []


class TestTheBatchLimitIsVisible:
    def test_messages_never_reached_get_their_own_node(self, traces):
        state_dir, write = traces
        write([_run(_iso(datetime.datetime.now(UTC)), examined=30, reached=10)])
        diagram = review_flow.triage_diagram(state_dir, days=30)
        assert _links(diagram)[("examined", "not_reached")] == 20
        assert _links(diagram)[("examined", "reached")] == 10

    def test_it_never_reports_a_negative_flow(self, traces):
        # A run whose touched count exceeds its examined count would otherwise
        # produce a negative bar, which draws as a nonsense shape.
        state_dir, write = traces
        write([_run(_iso(datetime.datetime.now(UTC)), examined=5, reached=10)])
        diagram = review_flow.triage_diagram(state_dir, days=30)
        assert all(l["value"] >= 0 for l in diagram["links"])
