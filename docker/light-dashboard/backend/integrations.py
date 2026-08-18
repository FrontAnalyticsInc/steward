"""What each part of this deployment can still reach, and when it last did.

Grouped by **source system**, not by protocol. Whether a connection is made over
MCP or a direct API client is an implementation detail; the question an operator
actually asks is "is Gmail working?" and only then "which part of it?". Grouping
by source answers those in that order, and as a side effect makes the security
model legible — one glance shows that exactly one consumer can send mail.

Within a source, one row per **grant**: a (consumer, capability) pair. Grants
fail independently — the triage job's Gmail credential can go stale while chat's
keeps working — so they are the unit of status, and the source row above them
carries only the worst status among them.

Status is derived from the outcome of real calls, never from a synthetic probe.
A ping proves a credential authenticates; it does not prove the scope is still
granted, which is the failure that actually happens. Where the logs record that
a call happened but not how it ended, this module says `unverified` rather than
inventing a green tick — see `gaps()` for what is missing and where.

Read defensively throughout: a missing database, an unparseable log line or an
absent config degrades one row, never the screen.

Secrets: served on an unauthenticated LAN-reachable port, so nothing here
returns a credential value. Env vars appear as NAMES only, and OAuth state is
reduced to a boolean plus an expiry timestamp.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import os
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Ranked worst-last. A source row shows the highest-ranked status among its
# grants, so "one grant is broken" is never hidden behind "three are fine".
#
# `never` ranks lowest on purpose: a grant that exists and has not been used is
# informational — dead weight or a forgotten credential — not an alarm, and a
# source with one working grant and one unused one is working.
#
# `unverified` outranks `working` because it is a weaker claim, not a stronger
# one. A screen that renders "we logged a call but not its outcome" as a green
# tick is the exact dishonesty this module exists to avoid.
STATUS_RANK = {
    "never": 0,
    "working": 1,
    "unverified": 2,
    "stale": 3,
    "failed": 4,
}

# How a status was arrived at. Rendered distinctly in the UI so evidence is
# never confused with inference.
#   usage    — outcome of the last real call
#   activity — a real call happened; its outcome was not recorded (see gaps)
#   config   — no calls at all; derived from credential/config state
BASIS_USAGE = "usage"
BASIS_ACTIVITY = "activity"
BASIS_CONFIG = "config"

# Where `hermes mcp login` parks OAuth state. Presence of <server>.json is the
# only honest local signal that a server is usable at all — a config entry with
# `auth: oauth` and no token connects and then fails on the first tool call.
TOKEN_DIR_NAME = "mcp-tokens"

# Session `source` values, as the gateway writes them, mapped to what a person
# would call that consumer. sessions.profile_name is NULL for every row in
# practice, so the session source is the finest consumer attribution available;
# see gaps().
CONSUMER_LABELS = {
    "api_server": "chat",
    "webui": "web",
    "tui": "tui",
    "cli": "cli",
    "telegram": "telegram",
    "cron": "scheduled jobs",
    "kanban": "kanban",
}

# Usage older than this is not read at all. Bounds the query on a database that
# grows without limit, and a call from two months ago answers no question this
# screen asks.
USAGE_WINDOW_DAYS = 30
USAGE_ROW_CAP = 20000
RECENT_CALLS = 3


def _rel_seconds(ts: Optional[float]) -> Optional[float]:
    return None if ts is None else max(0.0, time.time() - ts)


def _parse_interval(spec: Any) -> Optional[float]:
    """`"6h"` / `"3d"` / `900` → seconds. Anything unparseable → None.

    None means "no expectation was stated", which is different from "expected
    to be idle" — a grant with no interval can never be stale, because nothing
    said how often it should run.
    """
    if isinstance(spec, (int, float)):
        return float(spec) if spec > 0 else None
    if not isinstance(spec, str) or not spec.strip():
        return None
    text = spec.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        if text[-1] in units:
            return float(text[:-1]) * units[text[-1]]
        return float(text)
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Config — optional, and only for what cannot be derived
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {"version": 1, "sources": {}, "grants": [], "consumers": {}}


def load_config(path: str) -> Dict[str, Any]:
    """Grant definitions from `integrations.json`, if one exists.

    Deliberately optional. Everything this screen shows is derived from configs
    and logs; the file only supplies the three things no log can state — a
    human label for a source, the credential type, and how often a grant is
    *expected* to run (without which `stale` is undecidable). A grant that
    appears in the logs but not here still displays, under its raw identifiers.
    """
    if not path or not os.path.exists(path):
        return dict(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        # A malformed config must not take the screen down with it.
        cfg = dict(DEFAULT_CONFIG)
        cfg["error"] = f"{os.path.basename(path)}: {exc}"
        return cfg
    if not isinstance(data, dict):
        cfg = dict(DEFAULT_CONFIG)
        cfg["error"] = f"{os.path.basename(path)}: expected a JSON object"
        return cfg
    return {
        "version": data.get("version", 1),
        "sources": data.get("sources") if isinstance(data.get("sources"), dict) else {},
        "grants": data.get("grants") if isinstance(data.get("grants"), list) else [],
        "consumers": (
            data.get("consumers") if isinstance(data.get("consumers"), dict) else {}
        ),
        "path": path,
    }


def _source_cfg(cfg: Dict[str, Any], key: str) -> Dict[str, Any]:
    entry = (cfg.get("sources") or {}).get(key)
    return entry if isinstance(entry, dict) else {}


def _capability_for(cfg: Dict[str, Any], source_key: str, tool: str) -> Optional[str]:
    """Which declared capability a tool call belongs to, if any is declared.

    Matched by glob against the bare tool name, so `{"name": "send", "match":
    ["send_*"]}` collects `send_email` without the config having to restate the
    `mcp__gmail__` prefix. No match → None, and the grant is keyed on the
    consumer alone.
    """
    for rule in _source_cfg(cfg, source_key).get("capabilities") or []:
        if not isinstance(rule, dict):
            continue
        patterns = rule.get("match") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        for pattern in patterns:
            if isinstance(pattern, str) and fnmatch.fnmatch(tool, pattern):
                return rule.get("name") or None
    return None


def _consumer_label(cfg: Dict[str, Any], raw: str) -> str:
    declared = (cfg.get("consumers") or {}).get(raw)
    if isinstance(declared, dict) and declared.get("label"):
        return str(declared["label"])
    if isinstance(declared, str):
        return declared
    return CONSUMER_LABELS.get(raw, raw)


# ---------------------------------------------------------------------------
# Usage: Hermes MCP calls, from the session database
# ---------------------------------------------------------------------------


def _split_mcp_tool(tool_name: str) -> Optional[Tuple[str, str]]:
    """`mcp__gmail__send_email` → `("gmail", "send_email")`.

    Anything not in that shape is not an MCP call and is skipped — the same
    table holds the built-in tools (`read_file`, `cronjob`), which are not
    integrations.
    """
    if not tool_name or not tool_name.startswith("mcp__"):
        return None
    rest = tool_name[len("mcp__"):]
    server, sep, tool = rest.partition("__")
    if not sep or not server or not tool:
        return None
    return server, tool


def mcp_calls(state_db: str) -> List[dict]:
    """Every MCP tool call in the recent window, with who made it and when.

    The gateway writes one `messages` row per tool result, carrying `tool_name`
    and the session it belongs to. Joined to `sessions.source` this gives the
    consumer. It does **not** carry an outcome — see gaps() — so every call here
    is evidence of activity, not of success.
    """
    if not state_db or not os.path.exists(state_db):
        return []
    cutoff = time.time() - USAGE_WINDOW_DAYS * 86400
    rows: List[dict] = []
    try:
        # Read-only: this database belongs to the gateway and is being written
        # to while we read it.
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT m.tool_name AS tool_name,
                   m.timestamp AS ts,
                   COALESCE(s.profile_name, '') AS profile,
                   COALESCE(s.source, 'unknown') AS consumer,
                   m.session_id AS session_id
              FROM messages m
              LEFT JOIN sessions s ON s.id = m.session_id
             WHERE m.tool_name LIKE 'mcp!_!_%' ESCAPE '!'
               AND m.timestamp >= ?
             ORDER BY m.timestamp DESC
             LIMIT ?
            """,
            (cutoff, USAGE_ROW_CAP),
        )
        for row in cur:
            parsed = _split_mcp_tool(row["tool_name"])
            if not parsed:
                continue
            server, tool = parsed
            rows.append({
                "source_key": server,
                "consumer": row["consumer"],
                "profile": row["profile"] or None,
                "tool": tool,
                "at": row["ts"],
                # The gateway records no per-call outcome. `None` here is the
                # honest value and drives the `unverified` status.
                "ok": None,
                "error": None,
                "session_id": row["session_id"],
                # Where the evidence came from. Chat reaches the world over MCP
                # and nothing else, so the chat sidebar filters on this rather
                # than on the consumer name — which today yields the same rows
                # only because no workflow pipeline happens to run as a chat
                # session.
                "origin": "mcp",
            })
        cur.close()
        conn.close()
    except sqlite3.Error as exc:
        print(f"integrations: could not read MCP usage from {state_db}: {exc}")
        return []
    return rows


# ---------------------------------------------------------------------------
# Usage: ADK workflow calls, from the integration call log
# ---------------------------------------------------------------------------

CALL_LOG_DIRNAME = "integration-calls"


def adk_calls(log_dir: str) -> List[dict]:
    """Outcome-bearing call records written by `workflows/app/integration_log.py`.

    One JSONL file per day, append-only. Unlike the MCP side these records carry
    `ok` and an error string, because the code making the call is ours and was
    changed to say so. Only the newest files are read — enough to cover the
    usage window without walking a directory that grows forever.
    """
    if not log_dir or not os.path.isdir(log_dir):
        return []
    try:
        names = sorted(
            f for f in os.listdir(log_dir) if f.endswith(".jsonl")
        )[-(USAGE_WINDOW_DAYS + 1):]
    except OSError:
        return []
    cutoff = time.time() - USAGE_WINDOW_DAYS * 86400
    rows: List[dict] = []
    for name in names:
        try:
            with open(os.path.join(log_dir, name), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        # One torn line (a crash mid-append) must not discard
                        # the rest of the day.
                        continue
                    if not isinstance(rec, dict):
                        continue
                    at = rec.get("at")
                    if not isinstance(at, (int, float)) or at < cutoff:
                        continue
                    source_key = rec.get("source")
                    if not source_key:
                        continue
                    rows.append({
                        "source_key": str(source_key),
                        "consumer": str(rec.get("consumer") or "workflows"),
                        "profile": None,
                        "tool": str(rec.get("operation") or "call"),
                        "capability": rec.get("capability") or None,
                        "at": float(at),
                        "ok": bool(rec.get("ok")) if rec.get("ok") is not None else None,
                        "error": rec.get("error") or None,
                        "session_id": rec.get("run_id"),
                        "origin": "workflow",
                    })
        except OSError:
            continue
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Hermes: MCP servers as declared
# ---------------------------------------------------------------------------


def _oauth_status(token_dir: str, server: str, auth: Optional[str]) -> Dict[str, Any]:
    """Authentication state for one MCP server, from its token file alone.

    Only `auth: oauth` servers have a token here. For the others `authenticated`
    is None — meaning "not applicable", not "not logged in". The distinction is
    the whole point: a stdio server carrying its own credential paths and a
    local server needing no credential at all are both fine, and rendering them
    as unauthenticated would put two permanent false alarms on the tab.

    Never reads the token itself into the response. `expires_at` is a wall-clock
    stamp Hermes writes alongside the token so a restart can tell a live token
    from one that expired while the process was down; we surface it so the UI
    can say "expires in 6h" rather than the useless "configured".
    """
    if auth != "oauth":
        return {"authenticated": None, "expires_at": None, "expired": None,
                "has_refresh_token": None}
    path = os.path.join(token_dir, f"{server}.json")
    if not os.path.exists(path):
        return {"authenticated": False, "expires_at": None, "expired": None,
                "has_refresh_token": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        # A corrupt token file is not an authenticated server.
        return {"authenticated": False, "expires_at": None, "expired": None,
                "has_refresh_token": None}
    expires_at = data.get("expires_at")
    expired = None
    if isinstance(expires_at, (int, float)):
        expired = expires_at <= time.time()
    return {
        "authenticated": True,
        "expires_at": expires_at,
        "expired": expired,
        "has_refresh_token": bool(data.get("refresh_token")),
    }


def declared_mcp_servers(agents: List[dict], db_dir: str) -> Dict[str, dict]:
    """One entry per MCP server, merged across every agent that declares it.

    `agents` is the output of /api/agents: each entry already carries the
    parsed, secret-scrubbed `mcp_servers` list, so this adds no second config
    parser. Which agents hold the connection is kept, because a server only one
    profile can see is a different fact from one every profile can see.
    """
    token_dir = os.path.join(db_dir, TOKEN_DIR_NAME)
    out: Dict[str, dict] = {}
    for agent in agents:
        for server in agent.get("mcp_servers") or []:
            name = server.get("name")
            if not name:
                continue
            row = out.get(name)
            if row is None:
                creds = list(server.get("env_keys") or []) + list(
                    server.get("header_keys") or []
                )
                row = {
                    "transport": server.get("transport"),
                    "target": server.get("target"),
                    "enabled": server.get("enabled", True),
                    "credential_keys": creds,
                    "auth": server.get("auth"),
                    "declared_by": [],
                    **_oauth_status(token_dir, name, server.get("auth")),
                }
                out[name] = row
            row["declared_by"].append(agent.get("name"))
            # A server disabled for one agent but live for another is enabled
            # *somewhere*, which is what the roster claims.
            if server.get("enabled", True):
                row["enabled"] = True
    for row in out.values():
        row["declared_by"] = sorted(set(n for n in row["declared_by"] if n))
    return out


# ---------------------------------------------------------------------------
# ADK: direct API clients, read from their own source
# ---------------------------------------------------------------------------

# Outward-facing modules in `workflows/app/`. This registry carries only what
# cannot be derived — the human-facing label and which source system the module
# actually talks to — because naming "Gmail" is knowledge the source does not
# state. Everything else (what it does, what configures it, who uses it) is read
# from the module.
#
# It is a FALLBACK, not the roster. `_adk_module_specs()` also scans the mount,
# so a new integration appears without editing this file — a hardcoded list is a
# second place to update, and the one people forget, which shows up as an
# integration silently missing from the screen rather than as an error.
ADK_MODULES = [
    {"module": "gmail_api", "label": "Gmail", "source_key": "gmail"},
    {"module": "approvals", "label": "Human approval queue", "source_key": "approvals"},
]

# A module states its own identity with these, and then needs no entry above.
SOURCE_KEY_CONST = "INTEGRATION_SOURCE"
SOURCE_LABEL_CONST = "INTEGRATION_LABEL"

# Importing this is what makes a module outward-facing: it is how a module
# records the outcome of a call it made to something outside this process. That
# is a better signal than a name pattern, because it is the same fact the screen
# reports — a module with nothing to log has no grant to show.
INTEGRATION_LOG_MODULE = "integration_log"

# Never auto-discovered, whatever they import. `integration_log` is the logger
# itself, not a thing being reached.
ADK_MODULE_SKIP = {INTEGRATION_LOG_MODULE, "agent", "config", "fast_api_app"}

# Suffixes that name the *shape* of a client rather than the system it reaches,
# so `calendar_api` keys on `calendar` and matches a `calendar` config entry.
_MODULE_SUFFIXES = ("_api", "_sink", "_client")


def _derive_source_key(module: str) -> str:
    """Config key for a module that does not name one itself."""
    for suffix in _MODULE_SUFFIXES:
        if module.endswith(suffix):
            return module[: -len(suffix)]
    return module


def _derive_label(source_key: str) -> str:
    """Last-resort human label. Deliberately plain: a wrong-looking label on the
    screen is a prompt to set INTEGRATION_LABEL, whereas a missing row is not.
    """
    return source_key.replace("_", " ").title()


def _adk_module_specs(src_dir: str) -> List[dict]:
    """Curated entries, plus every other outward-facing module found in `src_dir`.

    A module states its own identity with `INTEGRATION_SOURCE` /
    `INTEGRATION_LABEL`; those win over the curated fallback, because the module
    is the more local and more current statement of what it talks to. A module
    that declares neither and is not listed above still appears, keyed on its own
    name — under a plain label, but present, which is the point.
    """
    specs: Dict[str, dict] = {}
    if os.path.isdir(src_dir):
        for fname in sorted(os.listdir(src_dir)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            module = fname[: -len(".py")]
            if module in ADK_MODULE_SKIP:
                continue
            try:
                with open(os.path.join(src_dir, fname), "r", encoding="utf-8") as f:
                    tree = ast.parse(f.read())
            except (OSError, SyntaxError):
                # Unreadable or mid-edit: fall through to the curated entry if
                # there is one, rather than dropping a known integration.
                continue
            consts = _string_constants(tree)
            declared_key = consts.get(SOURCE_KEY_CONST)
            if not declared_key and not _imports_module(tree, INTEGRATION_LOG_MODULE):
                continue
            source_key = declared_key or _derive_source_key(module)
            specs[module] = {
                "module": module,
                "source_key": source_key,
                "label": consts.get(SOURCE_LABEL_CONST) or _derive_label(source_key),
            }

    # Curated entries fill in for modules that declare nothing. `memory` and
    # `wiki_write` both declare INTEGRATION_SOURCE, so the scan finds them and
    # neither needs an entry above — which is the arrangement this list exists
    # to make unnecessary.
    for spec in ADK_MODULES:
        found = specs.get(spec["module"])
        if found is None:
            specs[spec["module"]] = dict(spec)
            continue
        tree_declared = found["source_key"] != _derive_source_key(spec["module"])
        if not tree_declared:
            found["source_key"] = spec["source_key"]
        if found["label"] == _derive_label(found["source_key"]):
            found["label"] = spec["label"]
    return list(specs.values())


def _string_constants(tree: ast.AST) -> Dict[str, str]:
    """Module-level `NAME = "literal"` bindings.

    Needed because the env var is routinely named once as a constant and read
    through it — `TOKEN_FILE = "GMAIL_TOKEN_FILE"`, then
    `os.environ.get(TOKEN_FILE)`. Resolving one level of that indirection is the
    difference between listing Gmail's two tuning knobs and listing the three
    variables that actually decide whether Gmail works at all.
    """
    consts: Dict[str, str] = {}
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        if isinstance(target, ast.Name) and isinstance(value, ast.Constant):
            if isinstance(value.value, str):
                consts[target.id] = value.value
    return consts


def _env_names(tree: ast.AST) -> List[str]:
    """Every environment variable a module reads, by name.

    Covers the spellings these modules actually use: `os.getenv("X")`,
    `os.environ.get("X")`, `os.environ["X"]`, and any of those going through a
    module-level string constant. A name computed at runtime is still missed,
    which is the honest outcome — guessing would put a variable in the UI that
    no operator could act on.
    """
    consts = _string_constants(tree)

    def literal(node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        return None

    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            attr = fn.attr if isinstance(fn, ast.Attribute) else None
            if attr in ("getenv", "get") and node.args:
                # os.environ.get(...) or os.getenv(...); a plain dict `.get`
                # would be a false positive, so require the receiver to be the
                # os module or its environ mapping.
                recv = ast.dump(fn.value) if isinstance(fn, ast.Attribute) else ""
                if "environ" in recv or "'os'" in recv or '"os"' in recv:
                    found = literal(node.args[0])
                    if found:
                        names.append(found)
        elif isinstance(node, ast.Subscript):
            val = node.value
            if isinstance(val, ast.Attribute) and val.attr == "environ":
                found = literal(node.slice)
                if found:
                    names.append(found)
    return sorted(set(names))


def _imports_module(tree: ast.AST, module: str) -> bool:
    """Does this file import `module`, in any of the forms in use here?

    Parsed rather than string-matched. `from app import approvals, gmail_api`
    binds two integrations in one statement, and a substring test for
    "import gmail_api" sees only the first — which silently understates who
    touches Gmail.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module or alias.name.endswith("." + module):
                    return True
        elif isinstance(node, ast.ImportFrom):
            # `from app import gmail_api` / `from . import approvals`
            for alias in node.names:
                if alias.name == module:
                    return True
            # `from ..gmail_api import fetch` / `from app.gmail_api import fetch`
            mod = node.module or ""
            if mod == module or mod.endswith("." + module):
                return True
    return False


def _module_users(agents_dir: str, module: str) -> List[str]:
    """Agent packages under `app/agents/` that import `module`.

    Answers "who would break if this integration lost its credential", which is
    the reason to show an integration at all.
    """
    users: List[str] = []
    if not os.path.isdir(agents_dir):
        return users
    for entry in sorted(os.listdir(agents_dir)):
        pkg = os.path.join(agents_dir, entry)
        if not os.path.isdir(pkg):
            continue
        for root, dirs, files in os.walk(pkg):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            hit = False
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                try:
                    with open(os.path.join(root, fname), "r", encoding="utf-8") as f:
                        tree = ast.parse(f.read())
                except (OSError, SyntaxError):
                    continue
                if _imports_module(tree, module):
                    hit = True
                    break
            if hit:
                users.append(entry)
                break
    return users


def declared_adk_modules(src_dir: str) -> Dict[str, dict]:
    """Outward-facing workflow modules, keyed by the source system they reach.

    `src_dir` is the read-only mount of `workflows/app`. A missing mount yields
    an empty mapping and a gap line rather than a silent "no integrations" —
    those are different answers and only one of them is a deployment problem.
    """
    out: Dict[str, dict] = {}
    if not os.path.isdir(src_dir):
        return out
    agents_dir = os.path.join(src_dir, "agents")
    for spec in _adk_module_specs(src_dir):
        module = spec["module"]
        path = os.path.join(src_dir, f"{module}.py")
        entry = {
            "module": module,
            "label": spec["label"],
            "present": os.path.exists(path),
            "summary": None,
            "env_keys": [],
            "used_by": [],
        }
        if entry["present"]:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                doc = ast.get_docstring(tree)
                # First line of the module docstring: these modules open with a
                # one-line statement of what they are, which is a better
                # description than anything restated here could be.
                entry["summary"] = doc.strip().splitlines()[0] if doc else None
                entry["env_keys"] = _env_names(tree)
            except (OSError, SyntaxError):
                pass
            entry["used_by"] = _module_users(agents_dir, module)
        out[spec["source_key"]] = entry
    return out


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _grant_status(
    calls: List[dict],
    expected_interval: Optional[float],
) -> Tuple[str, str, Optional[dict]]:
    """Status, basis, and the failing call (if any) for one grant's history.

    `calls` is newest-first. The rules, in the order they are applied:

      the last call failed        → failed   (the error is the useful thing)
      the last successful call is
        older than expected       → stale    (the job stopped; nothing else says so)
      the last call has no outcome→ unverified
      the last call succeeded     → working
      no calls at all             → never
    """
    if not calls:
        return "never", BASIS_CONFIG, None

    last = calls[0]
    outcomes = [c for c in calls if c.get("ok") is not None]
    last_success = next((c for c in calls if c.get("ok") is True), None)

    if last.get("ok") is False:
        return "failed", BASIS_USAGE, last

    if expected_interval:
        # Measured from the last *successful* call where outcomes exist, and
        # from the last call otherwise: with no outcome recorded, "it ran" is
        # the strongest available evidence that the job is alive.
        reference = last_success if outcomes else last
        age = _rel_seconds(reference["at"]) if reference else None
        if age is not None and age > expected_interval:
            basis = BASIS_USAGE if outcomes else BASIS_ACTIVITY
            return "stale", basis, None

    if last.get("ok") is True:
        return "working", BASIS_USAGE, None
    return "unverified", BASIS_ACTIVITY, None


def _recent(calls: List[dict]) -> List[dict]:
    return [
        {
            "at": c["at"],
            "operation": c.get("tool"),
            "ok": c.get("ok"),
            "error": c.get("error"),
        }
        for c in calls[:RECENT_CALLS]
    ]


def _build_grants(
    cfg: Dict[str, Any],
    source_key: str,
    calls: List[dict],
    credential_type: Optional[str],
    expires_at: Optional[float],
    source_origins: List[str],
) -> List[dict]:
    """Observed grants for one source: one row per (consumer, capability).

    `source_origins` is how the source is *declared* reachable (mcp, workflow,
    or both). Observed grants take their origin from the calls themselves, which
    is stronger evidence; a declared-but-never-used grant has no calls to read,
    so it falls back to the declaration.
    """
    buckets: Dict[Tuple[str, Optional[str]], List[dict]] = {}
    for call in calls:
        capability = call.get("capability") or _capability_for(
            cfg, source_key, call.get("tool") or ""
        )
        buckets.setdefault((call["consumer"], capability), []).append(call)

    declared = {
        (g.get("consumer"), g.get("capability")): g
        for g in cfg.get("grants") or []
        if isinstance(g, dict) and g.get("source") == source_key
    }

    grants: List[dict] = []
    for (consumer, capability), rows in buckets.items():
        rows.sort(key=lambda r: r["at"], reverse=True)
        declared_grant = declared.pop((consumer, capability), None) or {}
        interval = _parse_interval(declared_grant.get("expected_interval"))
        status, basis, failure = _grant_status(rows, interval)
        successes = [r for r in rows if r.get("ok") is True]
        grants.append({
            "consumer": consumer,
            "consumer_label": _consumer_label(cfg, consumer),
            "capability": capability,
            # Distinct operations actually exercised. With no capability
            # declared this is what the grant *is*, so it belongs in the
            # expanded row rather than being summarised away.
            "operations": sorted({r["tool"] for r in rows if r.get("tool")}),
            "status": status,
            "status_basis": basis,
            "last_used_at": rows[0]["at"],
            "last_success_at": successes[0]["at"] if successes else None,
            "last_error": (failure or {}).get("error"),
            "call_count": len(rows),
            "expected_interval": declared_grant.get("expected_interval"),
            "credential_type": declared_grant.get("credential_type") or credential_type,
            "expires_at": expires_at,
            "declared": bool(declared_grant),
            # From the calls, not the declaration: a source declared both ways
            # (Gmail is an MCP server *and* a workflow module) has grants of
            # each kind, and only the calls say which is which.
            "origins": sorted({r["origin"] for r in rows if r.get("origin")})
                       or list(source_origins),
            "recent": _recent(rows),
        })

    # Declared but never exercised. Kept visible: the spec's "never used" case
    # is exactly the forgotten credential worth knowing about.
    for (consumer, capability), grant in declared.items():
        grants.append({
            "consumer": consumer,
            "consumer_label": _consumer_label(cfg, consumer or ""),
            "capability": capability,
            "operations": [],
            "status": "never",
            "status_basis": BASIS_CONFIG,
            "last_used_at": None,
            "last_success_at": None,
            "last_error": None,
            "call_count": 0,
            "expected_interval": grant.get("expected_interval"),
            "credential_type": grant.get("credential_type") or credential_type,
            "expires_at": expires_at,
            "declared": True,
            "origins": list(source_origins),
            "recent": [],
        })

    grants.sort(
        key=lambda g: (-STATUS_RANK.get(g["status"], 0), g["consumer_label"],
                       g["capability"] or "")
    )
    return grants


def _rollup(grants: List[dict], fallback: str = "never") -> str:
    if not grants:
        return fallback
    return max(grants, key=lambda g: STATUS_RANK.get(g["status"], 0))["status"]


def build(
    agents: List[dict],
    db_dir: str,
    src_dir: str,
    state_db: str,
    call_log_dir: str,
    config_path: str,
) -> Dict[str, Any]:
    """The whole screen: sources, each with its grants, plus what we cannot say.

    Every input is optional in the sense that its absence degrades the answer
    without breaking it — no config, no session database, no workflows mount and
    no call log each remove rows or lower confidence, and each is reported in
    `gaps` rather than papered over.
    """
    cfg = load_config(config_path)
    declared_mcp = declared_mcp_servers(agents, db_dir)
    declared_adk = declared_adk_modules(src_dir)
    calls = mcp_calls(state_db) + adk_calls(call_log_dir)

    by_source: Dict[str, List[dict]] = {}
    for call in calls:
        by_source.setdefault(call["source_key"], []).append(call)

    keys = sorted(set(declared_mcp) | set(declared_adk) | set(by_source))
    sources: List[dict] = []
    for key in keys:
        mcp = declared_mcp.get(key)
        adk = declared_adk.get(key)
        scfg = _source_cfg(cfg, key)

        credential_type = scfg.get("credential_type")
        if credential_type is None and mcp:
            credential_type = mcp.get("auth") or (
                "env" if mcp.get("credential_keys") else None
            )
        expires_at = mcp.get("expires_at") if mcp else None

        origins = [k for k, present in (("mcp", mcp), ("workflow", adk)) if present]
        grants = _build_grants(
            cfg, key, by_source.get(key, []), credential_type, expires_at, origins
        )

        # A declared MCP server nobody has called yet still deserves a row: the
        # grant exists, and "never used" is the answer.
        if not grants and mcp:
            grants = [{
                "consumer": ", ".join(mcp["declared_by"]) or "unknown",
                "consumer_label": ", ".join(mcp["declared_by"]) or "unknown",
                "capability": None,
                "operations": [],
                "status": "never",
                "status_basis": BASIS_CONFIG,
                "last_used_at": None,
                "last_success_at": None,
                "last_error": None,
                "call_count": 0,
                "expected_interval": None,
                "credential_type": credential_type,
                "expires_at": expires_at,
                "declared": True,
                "origins": ["mcp"],
                "recent": [],
            }]
        if not grants and adk:
            grants = [{
                "consumer": ", ".join(adk["used_by"]) or adk["module"],
                "consumer_label": ", ".join(adk["used_by"]) or adk["module"],
                "capability": adk["module"],
                "operations": [],
                "status": "never",
                "status_basis": BASIS_CONFIG,
                "last_used_at": None,
                "last_success_at": None,
                "last_error": None,
                "call_count": 0,
                "expected_interval": None,
                "credential_type": credential_type,
                "expires_at": expires_at,
                "declared": True,
                "origins": ["workflow"],
                "recent": [],
            }]

        status = _rollup(grants)
        # Credential state can only make a source's status worse, never better:
        # an expired token with a green grant behind it means the green grant is
        # about to stop being green, and that is worth surfacing now.
        credential_note = None
        if mcp:
            if mcp.get("authenticated") is False:
                credential_note = "not signed in — run `hermes mcp login`"
                status = "failed"
            elif mcp.get("expired") is True:
                credential_note = "OAuth token expired"
                status = "failed"
            elif not mcp.get("enabled", True):
                credential_note = "disabled in config"
        if adk and not adk["present"]:
            credential_note = f"{adk['module']}.py is missing from the workflows source"
            status = "failed"

        sources.append({
            "key": key,
            "label": scfg.get("label") or (adk or {}).get("label") or key,
            "status": status,
            "kinds": origins,
            "declared_by": (mcp or {}).get("declared_by", []),
            "transport": (mcp or {}).get("transport"),
            "target": (mcp or {}).get("target"),
            "credential_type": credential_type,
            "credential_keys": sorted(set(
                list((mcp or {}).get("credential_keys") or [])
                + list((adk or {}).get("env_keys") or [])
            )),
            "expires_at": expires_at,
            "has_refresh_token": (mcp or {}).get("has_refresh_token"),
            "credential_note": credential_note,
            "summary": (adk or {}).get("summary"),
            "module": (adk or {}).get("module"),
            # Present in the logs but in no config we can see: shown under its
            # raw identifier rather than dropped.
            "unknown": mcp is None and adk is None and not scfg,
            "grants": grants,
        })

    sources.sort(key=lambda s: (-STATUS_RANK.get(s["status"], 0), s["label"].lower()))
    return {
        "sources": sources,
        "gaps": gaps(cfg, src_dir, state_db, call_log_dir, sources),
        "config": {
            "path": config_path,
            "present": os.path.exists(config_path) if config_path else False,
            "error": cfg.get("error"),
        },
    }


def gaps(
    cfg: Dict[str, Any],
    src_dir: str,
    state_db: str,
    call_log_dir: str,
    sources: List[dict],
) -> List[dict]:
    """What this screen cannot currently tell you, and why.

    Shown in the UI rather than kept in a document, because the alternative to
    naming a missing signal is quietly rendering a weaker signal as if it were
    the real one. Each entry names the fix in the *logging*, never a probe.
    """
    out: List[dict] = []

    unverified = sum(
        1 for s in sources for g in s["grants"] if g["status"] == "unverified"
    )
    if unverified:
        out.append({
            "id": "mcp-outcomes",
            "severity": "warn",
            "title": f"{unverified} grant(s) show activity with no recorded outcome",
            "detail": (
                "The gateway writes one messages row per MCP tool result with "
                "tool_name and a timestamp, but no success flag and no error. "
                "Fix belongs in hermes-agent: set a status/error field in "
                "agent/tool_dispatch_helpers.make_tool_result_message() from the "
                "MCP CallToolResult.isError it already receives, and persist it "
                "in hermes_state.append_message(). Until then these grants are "
                "reported as 'used, outcome unknown' rather than as working."
            ),
        })

    if not os.path.exists(state_db):
        out.append({
            "id": "state-db",
            "severity": "error",
            "title": "Session database not readable",
            "detail": f"No usage history: {state_db} does not exist.",
        })
    else:
        out.append({
            "id": "consumer-attribution",
            "severity": "info",
            "title": "Consumers are session sources, not agent profiles",
            "detail": (
                "sessions.profile_name is NULL for every session the gateway "
                "writes, so a grant can be attributed to 'chat' or 'scheduled "
                "jobs' but not to the profile that ran. Fix belongs in the "
                "gateway's session creation, which knows the profile."
            ),
        })

    if not os.path.isdir(src_dir):
        out.append({
            "id": "workflows-src",
            "severity": "warn",
            "title": "Workflows source not mounted",
            "detail": f"Cannot describe workflow integrations: {src_dir} is absent.",
        })
    if not os.path.isdir(call_log_dir):
        out.append({
            "id": "adk-call-log",
            "severity": "info",
            "title": "No workflow integration calls recorded yet",
            "detail": (
                f"{call_log_dir} does not exist. app/integration_log.py writes "
                "it on the first outbound workflow call; an empty directory "
                "simply means no pipeline has run since it was added."
            ),
        })

    if not (cfg.get("grants") or cfg.get("sources")):
        out.append({
            "id": "no-config",
            "severity": "info",
            "title": "No integrations.json — 'stale' cannot be decided",
            "detail": (
                "Without a declared expected_interval nothing can be called "
                "overdue, so a job that stopped six days ago looks the same as "
                "one that ran this morning. Copy integrations.example.json to "
                f"{cfg.get('path') or 'integrations.json'} and set intervals."
            ),
        })
    if cfg.get("error"):
        out.append({
            "id": "config-error",
            "severity": "warn",
            "title": "integrations.json could not be read",
            "detail": str(cfg["error"]),
        })
    return out


def grants_for_consumer(
    payload: Dict[str, Any], consumer: str, origin: Optional[str] = None
) -> List[dict]:
    """Every grant held by one consumer, flattened across sources.

    Used by the chat screen (what this conversation can reach) and by the review
    queue (what the agent that produced this item had access to). Both ask the
    same question from the other end of the grouping, so it is one function.

    `origin` narrows to one kind of connection. The chat sidebar passes "mcp",
    because that is the only way chat reaches anything and listing a workflow's
    direct API client beside it would imply the conversation could use it. The
    review queue passes nothing: what an agent could reach is the whole answer,
    however it reached it.
    """
    out = []
    for source in payload.get("sources") or []:
        for grant in source.get("grants") or []:
            if grant.get("consumer") != consumer:
                continue
            if origin and origin not in (grant.get("origins") or []):
                continue
            out.append({
                "source": source["label"],
                "source_key": source["key"],
                "capability": grant.get("capability"),
                "operations": grant.get("operations") or [],
                "status": grant.get("status"),
                "status_basis": grant.get("status_basis"),
                "last_used_at": grant.get("last_used_at"),
                "last_error": grant.get("last_error"),
                "origins": grant.get("origins") or [],
            })
    out.sort(key=lambda g: (-STATUS_RANK.get(g["status"], 0), g["source"].lower()))
    return out
