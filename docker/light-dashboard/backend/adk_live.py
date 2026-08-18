"""Read a team from a running ADK server's own introspection endpoint.

The companion to adk_introspect. That module parses `agent.py` off disk, which
is the only option for a pipeline root: `GET /apps/<app>/app-info` answers 400
"Root agent is not an LlmAgent" for a SequentialAgent or LoopAgent, and such an
app is also omitted from the routing root's sub-agent listing — so a pipeline is
invisible to this module entirely.

Where the root *is* an LlmAgent the live endpoint is strictly better data. It
reports instructions already resolved by the
interpreter — `instruction=INSTRUCTION` imported from a sibling `prompt.py`,
`model=build_model()` branching on an env var in `app/config.py` — none of which
a non-executing parser can follow without becoming a small Python interpreter.

What it costs: app-info reports name, description, instruction, tools and
sub_agents, and nothing else. There is no model, no agent class, no output_key.
Those come back as None and must render as "not reported", never as a guess.
The other trade is staleness in the opposite direction from the AST reader: this
shows what the server *loaded at startup*, so an edit on disk is invisible here
until a restart, where the parser would show it immediately.

Nothing in this module raises. An unreachable server is a normal state.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from . import adk_introspect

TIMEOUT = 2.0

# Where the workflows project's source is mounted, read-only. Needed because
# app-info cannot describe an app whose root is a workflow agent: it answers 400
# "Root agent is not an LlmAgent" for a SequentialAgent/LoopAgent root, and the
# routing root's own app-info silently OMITS such children rather than listing
# them — so a pipeline is invisible from both live routes. Parsing the source is
# the only way to see it, which is what adk_introspect already exists for.
WORKFLOWS_SRC_DIR = os.getenv("WORKFLOWS_SRC_DIR", "/opt/workflows/app")


def _get(url: str, timeout: float = TIMEOUT) -> Optional[Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _agent_entry(raw: dict, name: str) -> dict:
    """Map one app-info agent onto the same shape adk_introspect emits.

    Keys the endpoint cannot answer are present and None rather than absent, so
    the frontend renders one "not reported by ADK" treatment instead of having
    to distinguish missing-key from null.
    """
    instruction = raw.get("instruction")
    tools = [
        {"name": t, "docstring": None, "params": [], "returns": None, "resolved": False}
        if isinstance(t, str) else {
            "name": t.get("name"), "docstring": t.get("description"),
            "params": [], "returns": None, "resolved": False,
        }
        for t in (raw.get("tools") or [])
    ]
    subs = list(raw.get("sub_agents") or [])
    return {
        "var_name": name,
        "name": name,
        # app-info does not report the class. `is_workflow` is inferred from
        # having children, which is what the rail actually keys off.
        "agent_class": None,
        "is_workflow": bool(subs),
        "line": None,
        "description": raw.get("description"),
        "instruction": instruction,
        "instruction_resolved": True,
        "instruction_source": None,
        "instruction_chars": len(instruction) if isinstance(instruction, str) else 0,
        "model": None,
        "model_resolved": False,
        "model_source": None,
        "model_tier": None,
        "model_env_override": None,
        "model_extra": None,
        "declares_tools": bool(tools),
        "tools": tools,
        "tool_count": len(tools),
        "sub_agent_vars": subs,
        "sub_agents": subs,
        "config": {},
        "note": None,
    }


def _flatten(agents: Dict[str, dict], root: Optional[str]) -> List[dict]:
    """Walk root's sub_agents into the flat depth/order list the rail renders."""
    out: List[dict] = []
    seen = set()

    def walk(name: str, parent: Optional[str], depth: int, order: int):
        if name in seen or name not in agents:
            return
        seen.add(name)
        entry = dict(agents[name])
        entry.update({"parent": parent, "depth": depth, "order": order})
        out.append(entry)
        for i, child in enumerate(entry.get("sub_agent_vars") or []):
            walk(child, name, depth + 1, i)

    if root and root in agents:
        walk(root, None, 0, 0)
    for name in agents:
        if name not in seen:
            entry = dict(agents[name])
            entry.update({"parent": None, "depth": 0, "order": len(out), "unreachable": True})
            out.append(entry)
    return out


def _app_info(base_url: str, app: str) -> Optional[dict]:
    info = _get(f"{base_url.rstrip('/')}/apps/{app}/app-info")
    if not isinstance(info, dict) or not isinstance(info.get("agents"), dict):
        return None
    agents = {n: _agent_entry(a, n) for n, a in info["agents"].items()}
    return {
        "app": app,
        "root": info.get("rootAgentName"),
        "description": info.get("description"),
        "agents": _flatten(agents, info.get("rootAgentName")),
        "_names": set(agents),
    }


def _source_dir(app: str) -> Optional[str]:
    """Map an ADK app name onto its source directory under the mounted project.

    `app.agents.gmail_inbox_triage` -> <src>/agents/gmail_inbox_triage
    `app`                           -> <src>
    """
    if not os.path.isdir(WORKFLOWS_SRC_DIR):
        return None
    if app == "app":
        return WORKFLOWS_SRC_DIR
    if not app.startswith("app."):
        return None
    return os.path.join(WORKFLOWS_SRC_DIR, *app.split(".")[1:])


def app_sha(app: str) -> Optional[str]:
    """sha1 of the app's agent.py, for drift comparison against a run's record.

    Must stay byte-identical in method to hermes/scripts/invoke_workflow.py's
    agent_py_sha(), which stamps the same digest into each trace: the two are
    compared directly, so a difference in what is hashed would read as drift
    that never happened. Returns None when the source is not mounted, which
    aggregate() renders as unknown rather than as "no drift".
    """
    src = _source_dir(app)
    if not src:
        return None
    try:
        with open(os.path.join(src, "agent.py"), "rb") as fh:
            return hashlib.sha1(fh.read()).hexdigest()
    except OSError:
        return None


def _from_source(app: str) -> Optional[dict]:
    """Parse an app app-info could not describe. Returns the _app_info shape.

    Reports what the live endpoint structurally cannot: the agent class of every
    node, and the ordered sub-agent list of a workflow root. For a pipeline the
    steps ARE the thing worth seeing — it has no tools, so a tools-only view
    renders it as an empty box.
    """
    src = _source_dir(app)
    if not src:
        return None
    parsed = adk_introspect.parse_app(src)
    if parsed.get("status") != "ok" or not parsed.get("agents"):
        return None
    return {
        "app": app,
        "root": parsed.get("root"),
        "description": parsed.get("docstring"),
        "agents": parsed["agents"],
        "sha": parsed.get("sha"),
        "path": parsed.get("path"),
        "_names": {a.get("name") for a in parsed["agents"] if a.get("name")},
        # Flagged so the card can say where this came from. Source-parsed data
        # is stale in the opposite direction from live: it shows what is on disk,
        # which may not be what the server loaded at startup.
        "_source": "source",
    }


def _enrich_classes(team: dict) -> dict:
    """Fill agent_class on a live team from the source, where the source agrees.

    app-info reports no class at all, so every live node renders as "not
    reported" — which is most of the Agents tab. The class is a static fact, so
    reading it off disk is safe in a way that reading a model or an instruction
    would not be: those resolve at import time and the file may have changed
    since the server loaded it. Matching is by agent name, and a name the source
    does not have is left alone rather than guessed at.
    """
    src = _source_dir(team.get("app", ""))
    if not src:
        return team
    parsed = adk_introspect.parse_app(src)
    if parsed.get("status") != "ok":
        return team
    by_name = {a.get("name"): a for a in parsed.get("agents") or [] if a.get("name")}
    for agent in team.get("agents") or []:
        name = agent.get("name")
        match = by_name.get(name)
        if match is None and name:
            # Agents registered by import rather than defined in this file — the
            # routing root's children are each their own module — so parse the
            # module that owns it.
            own = _source_dir(f"app.agents.{name}")
            if own and os.path.isdir(own):
                sub = adk_introspect.parse_app(own)
                if sub.get("status") == "ok":
                    match = next(
                        (a for a in sub.get("agents") or [] if a.get("name") == name), None
                    )
        if not match:
            continue
        if agent.get("agent_class") is None and match.get("agent_class"):
            agent["agent_class"] = match["agent_class"]
            agent["agent_class_source"] = "source"
        # Only ever upgrade this flag. Live infers it from children the server
        # actually loaded; the parser infers it from a sub_agents list it can
        # read, and `sub_agents=WORKFLOW_AGENTS` is a name it cannot follow. A
        # false from the parser is "could not tell", not "is a leaf".
        agent["is_workflow"] = bool(agent.get("is_workflow")) or bool(match.get("is_workflow"))
    return team


def fetch_teams(base_url: str, project: str) -> List[dict]:
    """Every app on one ADK server, deduped, in the /api/adk/teams shape.

    A project like workflows registers each agent module as its own app *and*
    under a routing root, so /list-apps returns `app`, `app.agents.enrich_contact`
    and `app.agents.draft_reply` — three entries covering two agents.

    Only *aliases* — two apps describing the identical agent set — are folded,
    into `also_invocable_as`. A routing root is NOT folded over, because doing so
    hid every workflow it routed to: an app whose root is an LlmAgent got
    absorbed into `app` and vanished from the fleet, while a SequentialAgent root
    survived purely because app-info cannot describe it and its stages therefore
    never appeared in the root's listing. Visibility then depended on an ADK
    introspection limit rather than on anything true about the workflow.

    So a router is reported as one — `router: True` plus `routes_to` — and the
    apps it routes to are teams in their own right. A router is any app whose
    agent set strictly contains another app's, which is structural: it needs no
    list of known root names and stays correct when the project is renamed.
    """
    def _unavailable(reason: str) -> List[dict]:
        # A card saying "unreachable" beats the team silently vanishing from the
        # page, which reads as "this team no longer exists".
        return [{
            "app": project, "project": project, "source": "live", "server": base_url,
            "status": "error", "root": None, "agents": [], "also_invocable_as": [],
            "sha": None, "path": None, "stale": False,
            "error": {"type": "Unavailable", "message": reason},
        }]

    apps = _get(f"{base_url.rstrip('/')}/list-apps")
    if not isinstance(apps, list):
        return _unavailable(f"no response from {base_url}")

    # app-info first, source second. An app that app-info describes is better
    # data — resolved instructions, resolved models — so the fallback only runs
    # where the endpoint refused, which in practice means every workflow root.
    parsed = []
    for app in (a for a in apps if isinstance(a, str)):
        live = _app_info(base_url, app)
        described = _enrich_classes(live) if live else _from_source(app)
        if described:
            parsed.append(described)
    if not parsed:
        return _unavailable(f"{len(apps)} app(s) listed, none described by app-info")

    kept: List[dict] = []
    for cand in sorted(parsed, key=lambda p: (-len(p["_names"]), p["app"])):
        # Equal sets only. A subset is a workflow the candidate routes to, not
        # another name for it, and folding those is what made agents disappear.
        twin = next((k for k in kept if cand["_names"] == k["_names"]), None)
        if twin:
            twin.setdefault("also_invocable_as", []).append(cand["app"])
        else:
            kept.append(cand)

    for team in kept:
        routes_to = sorted(
            other["app"] for other in kept
            if other is not team and other["_names"] < team["_names"]
        )
        if routes_to:
            team["router"] = True
            team["routes_to"] = routes_to

    out = []
    for team in kept:
        team.pop("_names", None)
        team.setdefault("also_invocable_as", [])
        team["also_invocable_as"].sort()
        origin = team.pop("_source", "live")
        out.append({
            "sha": None,
            "path": None,
            # Defaults before **team so a flagged router keeps its own values.
            "router": False,
            "routes_to": [],
            **team,
            "project": project,
            # "source" here means parsed-from-disk, not fetched from the running
            # server. The card should say which, because they go stale in
            # opposite directions.
            "source": origin,
            "server": base_url,
            "status": "ok",
            "error": None,
            "stale": False,
        })
    return out
