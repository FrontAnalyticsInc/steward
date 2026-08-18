"""Where mail went, as flows rather than totals.

The Metrics screen counts what happened. It cannot show what became of what —
how many of the messages read got filed, how many turned into a reply, how many
of those replies a person actually released. Those are the questions a Sankey
answers, and they are answerable here only because two different sources each
know half of it.

Two diagrams, deliberately not one:

  **Triage** — from the run traces. What a batch of mail turned into.
  **Review** — from the queue directories. What became of the items a person
  was asked about.

They are drawn separately because a single diagram of both would double count.
Filing a message and drafting a reply to it are orthogonal: the triage code
records `actions = [FILED or LEFT_IN_INBOX]` and *appends* `DRAFT_QUEUED`, so a
message can be filed and answered. Chaining "disposition -> action" into one
flow would invent messages that do not exist — on a recent run the dispositions
summed to 10 and the actions to 12, and the 2 are the drafts, counted twice.

Every diagram here conserves: each node's inflow equals its outflow, and the
tests assert it. That is not decoration. A Sankey whose columns do not balance
is a picture that quietly asserts something false, and the ways to get there are
all easy — a partition that is not one, a window boundary, a missing field
read as zero.

**On the window**, which is the subtle one. The review stage is asynchronous: an
item queued last Monday may be decided on Friday or never. So flows are
*transitions that happened inside the window*, and the items that straddle its
edges get their own nodes — `Backlog at start` on the left, `Still pending` on
the right. Without those two, inflow and outflow simply do not match, and the
temptation is to fudge it. With them the diagram balances exactly, and the
backlog stops being invisible: for this queue it is currently the largest flow
on the chart, which is the single most useful thing it has to say.

The alternative, following only items *created* in the window, was rejected on
purpose. It reads as "mostly still pending" for any recent window — not because
anything is wrong but because those items have not been decided yet — and only
becomes true retroactively.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from . import adk_scorecard

TRIAGE_APP = "app.agents.gmail_inbox_triage"

# The disposition the triage code gates filing on. `file_it` requires
# `disposition == "check_later"` and a confidence above the floor, so a filed
# message is always a check_later one. That guarantee is what lets this module
# draw the disposition -> outcome links exactly instead of guessing at a join
# the metrics do not carry: only aggregate counts are recorded per run, never
# which message went where.
FILING_DISPOSITION = "check_later"

# Read off the item rather than assumed, but named here so the mapping from a
# decision to a box on the screen is in one place.
OUTCOME_LABELS = {
    "draft_created": "Draft created",
    "sent": "Sent",
    "rejected": "Rejected",
    "failed": "Failed",
    "awaiting": "Awaiting execution",
    "pending": "Still pending",
}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse(value: Any) -> Optional[datetime.datetime]:
    return adk_scorecard.parse_ts(value)


# --- triage -------------------------------------------------------------------


def _triage_totals(runs: List[dict]) -> Tuple[Dict[str, float], int]:
    """Sum the run metrics, and say how many runs carried a breakdown.

    The count matters: `disposition_*` and `action_*` were added after this
    pipeline had already been running, so most historical runs have only totals.
    Drawing a breakdown from the few that do while implying it covers them all
    is the mistake this returns a sample size to prevent.
    """
    totals: Dict[str, float] = {}
    with_breakdown = 0
    for run in runs:
        metrics = run.get("metrics") or {}
        extra = metrics.get("extra") or {}
        touched = metrics.get("touched") or {}
        totals["reached"] = totals.get("reached", 0) + float(touched.get("email") or 0)
        totals["examined"] = totals.get("examined", 0) + float(
            extra.get("candidates_examined") or 0
        )
        if any(k.startswith("disposition_") for k in extra):
            with_breakdown += 1
        for key, value in extra.items():
            if key.startswith(("disposition_", "action_")) or key == "undecided_count":
                totals[key] = totals.get(key, 0) + float(value or 0)
    return totals, with_breakdown


def triage_diagram(state_dir: str, days: int) -> dict:
    runs = adk_scorecard.load_runs(state_dir, TRIAGE_APP, days=days)
    totals, with_breakdown = _triage_totals(runs)

    examined = int(totals.get("examined", 0))
    reached = int(totals.get("reached", 0))
    # The fetch budget caps how many of the messages looked at get a verdict.
    # Without this node the first column does not balance, and the cap — a real
    # operational limit — would be invisible.
    not_reached = max(0, examined - reached)

    nodes: List[dict] = [
        {"id": "examined", "label": "Examined"},
        {"id": "reached", "label": "Triaged"},
    ]
    links: List[dict] = []
    notes: List[str] = []

    if not_reached:
        nodes.append({"id": "not_reached", "label": "Not reached (batch limit)"})
    if examined:
        links.append({"source": "examined", "target": "reached", "value": reached})
        if not_reached:
            links.append(
                {"source": "examined", "target": "not_reached", "value": not_reached}
            )

    dispositions = {
        key[len("disposition_"):]: int(value)
        for key, value in totals.items()
        if key.startswith("disposition_") and value
    }
    undecided = int(totals.get("undecided_count", 0))
    filed = int(totals.get("action_filed", 0))
    drafted = int(totals.get("action_draft_queued", 0))

    if not dispositions and not undecided:
        if runs:
            notes.append(
                "No per-message breakdown recorded for this window — these runs "
                "predate it. Totals above are still exact."
            )
        return {
            "key": "triage",
            "title": "Inbox triage",
            "subtitle": "What a batch of mail turned into",
            "nodes": nodes,
            "links": links,
            "notes": notes,
            "runs": len(runs),
            "runs_with_breakdown": with_breakdown,
        }

    # Reached -> disposition. Undecided is neither a disposition nor an action:
    # the message was fetched, read and paid for, and no verdict came back.
    # Folding it into either would make a broken run look like a decided one.
    for name, count in sorted(dispositions.items(), key=lambda kv: -kv[1]):
        nodes.append({"id": f"disp_{name}", "label": name.replace("_", " ").capitalize()})
        links.append({"source": "reached", "target": f"disp_{name}", "value": count})
    if undecided:
        nodes.append({"id": "disp_undecided", "label": "No verdict"})
        links.append({"source": "reached", "target": "disp_undecided", "value": undecided})

    # The mixed-instrumentation case, and the one that real data caught while
    # the unit tests were happy: `reached` counts every run in the window, but
    # only runs new enough to carry `disposition_*` can say what became of their
    # messages. On this fleet that was 3 runs out of 91 — so 210 messages
    # arrived at this node and 30 left it.
    #
    # The shortfall gets a node of its own rather than being hidden by quietly
    # narrowing `reached` to the instrumented runs. Narrowing would balance too,
    # but it would understate how much mail was actually handled, and a reader
    # comparing this against the totals elsewhere would find them disagreeing
    # with no way to see why. This terminates here on purpose: these messages
    # went somewhere, and the honest answer is that nothing recorded where.
    accounted = sum(dispositions.values()) + undecided
    unrecorded = max(0, reached - accounted)
    if unrecorded:
        nodes.append({"id": "disp_unrecorded", "label": "Outcome not recorded"})
        links.append(
            {"source": "reached", "target": "disp_unrecorded", "value": unrecorded}
        )

    # Disposition -> outcome. `filed` is a subset of the filing disposition by
    # construction, so the split is exact rather than apportioned. Everything
    # else was left where it was.
    nodes.append({"id": "out_filed", "label": "Filed"})
    nodes.append({"id": "out_inbox", "label": "Left in inbox"})

    filed = min(filed, dispositions.get(FILING_DISPOSITION, 0))
    for name, count in dispositions.items():
        if name == FILING_DISPOSITION:
            if filed:
                links.append(
                    {"source": f"disp_{name}", "target": "out_filed", "value": filed}
                )
            held = count - filed
            if held:
                links.append(
                    {"source": f"disp_{name}", "target": "out_inbox", "value": held}
                )
        else:
            links.append({"source": f"disp_{name}", "target": "out_inbox", "value": count})
    if undecided:
        links.append(
            {"source": "disp_undecided", "target": "out_inbox", "value": undecided}
        )

    held_total = dispositions.get(FILING_DISPOSITION, 0) - filed
    if held_total:
        notes.append(
            f"{held_total} message(s) the model wanted to file were left in the "
            "inbox — below the confidence floor, or the move failed."
        )
    if drafted:
        notes.append(
            f"{drafted} of the triaged messages also had a reply drafted. Drafting "
            "is orthogonal to where a message ended up, so it is not a branch "
            "here — those items appear in the Review queue diagram."
        )
    if with_breakdown < len(runs):
        notes.append(
            f"Breakdown covers {with_breakdown} of {len(runs)} runs in this window; "
            f"the rest predate per-message recording, which is the "
            f"{unrecorded} shown as not recorded."
        )

    return {
        "key": "triage",
        "title": "Inbox triage",
        "subtitle": "What a batch of mail turned into",
        "nodes": [n for n in nodes if _touches(n["id"], links) or n["id"] == "examined"],
        "links": links,
        "notes": notes,
        "runs": len(runs),
        "runs_with_breakdown": with_breakdown,
    }


def _touches(node_id: str, links: List[dict]) -> bool:
    return any(l["source"] == node_id or l["target"] == node_id for l in links)


# --- review queue -------------------------------------------------------------

# Most advanced state first: an approved item is also present in approved/ after
# it executes, because that directory is an immutable ledger of decisions and is
# never deleted from. Resolving in this order means each item is counted once,
# under what actually became of it.
_STATE_PRECEDENCE = ("rejected", "executed", "failed", "executing", "approved", "pending")


def _load_items(dirs: Dict[str, str]) -> List[Tuple[str, dict]]:
    """Every queue item once, paired with the furthest state it reached."""
    seen: Dict[str, Tuple[str, dict]] = {}
    for state in _STATE_PRECEDENCE:
        path = dirs.get(state)
        if not path or not os.path.isdir(path):
            continue
        for file_path in sorted(glob.glob(os.path.join(path, "*.json"))):
            try:
                with open(file_path, "r", encoding="utf-8") as fh:
                    item = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue  # a half-written item is not worth failing the page over
            item_id = item.get("id") or os.path.basename(file_path)
            if item_id not in seen:
                seen[item_id] = (state, item)
    return list(seen.values())


def _outcome_of(state: str, item: dict) -> str:
    if state == "rejected":
        return "rejected"
    if state == "failed":
        return "failed"
    if state == "executed":
        action = (item.get("execution") or {}).get("executor")
        if action == "send":
            return "sent"
        if action == "create_draft":
            return "draft_created"
        # apply_labels, record, or an item dismissed by hand. It left the queue
        # without producing mail, which is a real outcome and not a failure.
        return "rejected" if item.get("decision") == "reject" else "awaiting"
    if state in ("approved", "executing"):
        return "awaiting"
    return "pending"


def review_diagram(dirs: Dict[str, str], days: int) -> dict:
    since = _now() - datetime.timedelta(days=days)
    flows: Dict[Tuple[str, str], int] = {}

    for state, item in _load_items(dirs):
        created = _parse(item.get("created_at"))
        decided = _parse(item.get("decided_at"))
        # Decided before the window opened: it was neither in the queue during
        # the window nor moved during it, so it is not part of this flow.
        if decided is not None and decided < since:
            continue
        source = "carried_in" if (created is not None and created < since) else "queued_in"
        target = "pending" if decided is None else _outcome_of(state, item)
        flows[(source, target)] = flows.get((source, target), 0) + 1

    source_labels = {
        "carried_in": "Backlog at start",
        "queued_in": "Queued in window",
    }
    nodes: List[dict] = []
    links: List[dict] = []
    for key in ("carried_in", "queued_in"):
        if any(s == key for s, _ in flows):
            nodes.append({"id": key, "label": source_labels[key]})
    for key, label in OUTCOME_LABELS.items():
        if any(t == key for _, t in flows):
            nodes.append({"id": f"out_{key}", "label": label})
    for (source, target), value in sorted(flows.items(), key=lambda kv: -kv[1]):
        links.append({"source": source, "target": f"out_{target}", "value": value})

    notes: List[str] = []
    pending = sum(v for (_, t), v in flows.items() if t == "pending")
    carried = sum(v for (s, _), v in flows.items() if s == "carried_in")
    if pending:
        notes.append(
            f"{pending} item(s) are still waiting. They are shown so the diagram "
            "balances — an item queued in this window and not yet decided has to "
            "go somewhere."
        )
    if carried:
        notes.append(
            f"{carried} item(s) were already in the queue when the window opened."
        )

    return {
        "key": "review",
        "title": "Review queue",
        "subtitle": "What became of the items a person was asked about",
        "nodes": nodes,
        "links": links,
        "notes": notes,
    }


# --- public -------------------------------------------------------------------


def flow(state_dir: str, dirs: Dict[str, str], days: int = 30) -> dict:
    until = _now()
    return {
        "window_days": days,
        "since": (until - datetime.timedelta(days=days)).isoformat(),
        "until": until.isoformat(),
        "diagrams": [
            triage_diagram(state_dir, days),
            review_diagram(dirs, days),
        ],
    }


def imbalances(diagram: dict) -> List[dict]:
    """Nodes whose inflow and outflow disagree, for the tests and for callers.

    A Sankey that does not balance is a picture asserting something false, so
    this is exported rather than kept in the test file: anything building on
    these diagrams can check them the same way.

    Source nodes (no inflow) and sink nodes (no outflow) are terminals by
    definition and are skipped.
    """
    inflow: Dict[str, int] = {}
    outflow: Dict[str, int] = {}
    for link in diagram.get("links") or []:
        outflow[link["source"]] = outflow.get(link["source"], 0) + link["value"]
        inflow[link["target"]] = inflow.get(link["target"], 0) + link["value"]
    bad = []
    for node in set(inflow) & set(outflow):
        if inflow[node] != outflow[node]:
            bad.append({"node": node, "in": inflow[node], "out": outflow[node]})
    return sorted(bad, key=lambda b: b["node"])
