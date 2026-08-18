"""Read an ADK agent team out of its source, without importing it.

ADK's own introspection cannot describe every app here. `GET /apps/{app}/app-info`
returns 400 "Root agent is not an LlmAgent" whenever the root is a workflow agent
(a SequentialAgent or LoopAgent pipeline), and the richer `/dev/*` graph endpoints
only exist under `adk web` — the server here is `adk api_server`, no `--with_ui`.

So we parse. AST rather than import, for three reasons that matter operationally:
the dashboard stays dependency-free (no google-adk in a deliberately light image),
it never executes agent code it happens to be pointed at, and it keeps working when
the ADK container is down — which is exactly when you want to look at the team.

The cost is that we resolve expressions ourselves. The resolver below is small on
purpose: it handles the patterns this codebase actually uses and reports
`resolved: false` with the raw source for everything else. It never guesses. A
wrong model name rendered confidently is worse than an honest source snippet.
"""

from __future__ import annotations

import ast
import os
import hashlib
from typing import Any, Dict, List, Optional, Tuple

# Anything whose constructor name ends in `Agent` counts: LlmAgent, LoopAgent,
# SequentialAgent, ParallelAgent, and any custom subclass a future team defines.
_AGENT_SUFFIX = "Agent"

# The ADK classes that orchestrate other agents. Used to tell a container from a
# leaf; see the is_workflow comment below for why "not an LlmAgent" is not the
# same question.
_WORKFLOW_CLASSES = ("SequentialAgent", "ParallelAgent", "LoopAgent")

# ADK strips these from a tool's model-visible schema (FunctionTool._ignore_params),
# so showing them would misrepresent what the model is actually offered.
_HIDDEN_PARAM_TYPES = ("ToolContext",)
_HIDDEN_PARAM_NAMES = ("self", "cls", "input_stream")

# Parsed-file cache keyed by path -> (mtime_ns, size, payload). The dashboard polls
# and caches nothing else; re-walking these trees every 7s per open browser tab is
# exactly the cost we are avoiding.
_CACHE: Dict[str, Tuple[int, int, Any]] = {}
# Last successful parse per app dir, so a syntax error mid-edit shows the previous
# team plus the error rather than an empty page.
_LAST_GOOD: Dict[str, dict] = {}


# ---------------------------------------------------------------------------
# resolved values
# ---------------------------------------------------------------------------

def _val(value: Any, source: str, resolved: bool = True, **extra) -> dict:
    out = {"value": value, "source": source, "resolved": resolved}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


class _Ctx:
    """Module-level scope: constants and zero-arg functions we may inline."""

    def __init__(self, tree: ast.AST, src: str):
        self.src = src
        self.constants: Dict[str, ast.AST] = {}
        self.functions: Dict[str, ast.FunctionDef] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                tgt = node.targets[0]
                if isinstance(tgt, ast.Name):
                    self.constants[tgt.id] = node.value
            elif isinstance(node, ast.FunctionDef):
                self.functions[node.name] = node


def _seg(node: ast.AST, ctx: _Ctx) -> str:
    return ast.get_source_segment(ctx.src, node) or ""


def _resolve(node: Optional[ast.AST], ctx: _Ctx, depth: int = 0) -> dict:
    """Best-effort static evaluation. Returns a resolved-value dict, never raises."""
    if node is None:
        return _val(None, "", True)
    src = _seg(node, ctx)
    if depth > 6:  # cheap cycle guard; these chains are 2-3 deep in practice
        return _val(None, src, False)

    if isinstance(node, ast.Constant):
        return _val(node.value, src, True)

    if isinstance(node, ast.Name):
        if node.id in ctx.constants:
            inner = _resolve(ctx.constants[node.id], ctx, depth + 1)
            inner["source"] = src
            return inner
        return _val(None, src, False)

    if isinstance(node, ast.JoinedStr):  # f-string
        parts: List[str] = []
        env: Optional[str] = None
        for piece in node.values:
            if isinstance(piece, ast.Constant):
                parts.append(str(piece.value))
            elif isinstance(piece, ast.FormattedValue):
                got = _resolve(piece.value, ctx, depth + 1)
                if not got["resolved"] or got["value"] is None:
                    return _val(None, src, False)
                parts.append(str(got["value"]))
                # An interpolated constant may itself be env-overridable; that is
                # the whole `f"ollama_chat/{LOCAL_MODEL}"` chain, and losing it here
                # would hide why the running model can differ from the source.
                env = env or got.get("env_override")
            else:
                return _val(None, src, False)
        return _val("".join(parts), src, True, env_override=env)

    if isinstance(node, ast.Call):
        return _resolve_call(node, ctx, src, depth)

    return _val(None, src, False)


def _resolve_call(node: ast.Call, ctx: _Ctx, src: str, depth: int) -> dict:
    func = node.func
    kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}

    # os.environ.get("NAME", "default") -> the default, and remember the override.
    # The env var NAME is reported; its VALUE is never read. That matters here:
    # API_SERVER_KEY flows through this exact pattern, and the dashboard is
    # unauthenticated and LAN-reachable.
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "get"
        and _seg(func.value, ctx) in ("os.environ", "environ")
    ):
        args = node.args
        env_name = None
        if args and isinstance(args[0], ast.Constant):
            env_name = args[0].value
        default = _resolve(args[1], ctx, depth + 1) if len(args) > 1 else _val(None, src, True)
        return _val(default["value"], src, default["resolved"], env_override=env_name)

    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")

    # A model wrapper, e.g. LiteLlm(model=..., api_base=...). The display value is
    # its `model`; the rest is useful context (which gateway a tier points at).
    if name.endswith("Llm") or name.endswith("LLM"):
        model = _resolve(kwargs.get("model"), ctx, depth + 1)
        extra = {}
        for key in ("api_base", "api_key"):
            if key in kwargs:
                got = _resolve(kwargs[key], ctx, depth + 1)
                # Never surface a credential value, only where it came from.
                if key == "api_key":
                    extra[key] = got.get("env_override") or "(set in code)"
                else:
                    extra[key] = got["value"] if got["resolved"] else got["source"]
        return _val(model["value"], src, model["resolved"],
                    wrapper=name, env_override=model.get("env_override"),
                    model_extra=extra or None)

    # A zero-arg module-level factory, e.g. local_model(). Inline its return.
    if isinstance(func, ast.Name) and func.id in ctx.functions and not node.args:
        fn = ctx.functions[func.id]
        for stmt in ast.walk(fn):
            if isinstance(stmt, ast.Return) and stmt.value is not None:
                inner = _resolve(stmt.value, ctx, depth + 1)
                inner["source"] = src
                inner["factory"] = func.id
                return inner

    return _val(None, src, False)


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def _import_map(tree: ast.AST, app_dir: str) -> Dict[str, str]:
    """Map imported name -> the sibling .py file it came from.

    Only relative, same-package imports are followed (`from .workspace_tools import ...`).
    Anything else is out of our reach without importing, and is reported as such.
    """
    out: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            path = os.path.join(app_dir, node.module.replace(".", os.sep) + ".py")
            if os.path.isfile(path):
                for alias in node.names:
                    out[alias.asname or alias.name] = path
    return out


def _describe_tool(fn: ast.FunctionDef, src: str) -> dict:
    params = []
    args = fn.args
    defaults: List[Optional[ast.AST]] = [None] * (len(args.args) - len(args.defaults)) + list(args.defaults)
    for arg, default in zip(args.args, defaults):
        ann = ast.get_source_segment(src, arg.annotation) if arg.annotation else None
        if arg.arg in _HIDDEN_PARAM_NAMES:
            continue
        if ann and any(h in ann for h in _HIDDEN_PARAM_TYPES):
            continue  # ADK hides it from the model; so do we
        params.append({
            "name": arg.arg,
            "type": ann,
            "default": ast.get_source_segment(src, default) if default is not None else None,
            "required": default is None,
        })
    return {
        "name": fn.name,
        "docstring": ast.get_docstring(fn),
        "params": params,
        "returns": ast.get_source_segment(src, fn.returns) if fn.returns else None,
        "line": fn.lineno,
        "resolved": True,
    }


def _load_tool_defs(path: str) -> Dict[str, dict]:
    """Parse a tools module once, cached on mtime."""
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = "tools:" + path
    hit = _CACHE.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src, filename=path)
    except (OSError, SyntaxError, ValueError):
        return {}
    defs = {
        node.name: _describe_tool(node, src)
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    _CACHE[key] = (st.st_mtime_ns, st.st_size, defs)
    return defs


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------

def _preceding_comment(lines: List[str], lineno: int) -> Optional[str]:
    """The contiguous `#` block immediately above a definition.

    `reviewer` declares `tools=[]` on purpose, and the fourteen comment lines above
    it are the only record of why. Dropping them would turn a hard-won constraint
    into what looks like an oversight.
    """
    out: List[str] = []
    i = lineno - 2  # 0-indexed line above the statement
    while i >= 0:
        stripped = lines[i].strip()
        if stripped.startswith("#"):
            out.append(stripped.lstrip("#").strip())
        elif not stripped and out:
            break
        elif not stripped:
            i -= 1
            continue
        else:
            break
        i -= 1
    if not out:
        return None
    text = "\n".join(reversed(out)).strip()
    return text or None


_KNOWN_KWARGS = {"name", "description", "instruction", "model", "tools", "sub_agents"}


def _parse_agent(var: str, call: ast.Call, ctx: _Ctx, tools: Dict[str, dict],
                 lines: List[str]) -> dict:
    kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    cls = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "?")

    name = _resolve(kwargs.get("name"), ctx)
    instruction = _resolve(kwargs.get("instruction"), ctx)
    model = _resolve(kwargs.get("model"), ctx)

    tool_names: List[str] = []
    declared_tools = "tools" in kwargs
    if isinstance(kwargs.get("tools"), ast.List):
        for elt in kwargs["tools"].elts:
            tool_names.append(elt.id if isinstance(elt, ast.Name) else _seg(elt, ctx))

    sub: List[str] = []
    if isinstance(kwargs.get("sub_agents"), ast.List):
        for elt in kwargs["sub_agents"].elts:
            if isinstance(elt, ast.Name):
                sub.append(elt.id)

    # Everything we do not model explicitly still gets shown, as source.
    config = {}
    for key, node in kwargs.items():
        if key in _KNOWN_KWARGS:
            continue
        got = _resolve(node, ctx)
        config[key] = got["value"] if got["resolved"] else got["source"]

    resolved_tools = []
    for tname in tool_names:
        info = tools.get(tname)
        resolved_tools.append(info or {
            "name": tname, "docstring": None, "params": [], "returns": None,
            "resolved": False,
        })

    return {
        "var_name": var,
        "name": name["value"] or var,
        "agent_class": cls,
        # A workflow node orchestrates other agents. Being non-LlmAgent is not
        # enough: a custom BaseAgent step (a deterministic fetch, a queue write)
        # is a leaf that runs code, and rendering it as a container shows an
        # empty box where the actual work is.
        "is_workflow": cls in _WORKFLOW_CLASSES or bool(sub),
        "line": call.lineno,
        "description": _resolve(kwargs.get("description"), ctx)["value"],
        "instruction": instruction["value"],
        "instruction_resolved": instruction["resolved"],
        "instruction_source": None if instruction["resolved"] else instruction["source"],
        "instruction_chars": len(instruction["value"]) if isinstance(instruction["value"], str) else 0,
        "model": model["value"],
        "model_resolved": model["resolved"],
        "model_source": model["source"],
        "model_tier": (model.get("factory") or "").replace("_model", "") or None,
        "model_env_override": model.get("env_override"),
        "model_extra": model.get("model_extra"),
        "declares_tools": declared_tools,
        "tools": resolved_tools,
        "tool_count": len(resolved_tools),
        "sub_agent_vars": sub,
        "config": config,
        "note": _preceding_comment(lines, call.lineno if not hasattr(call, "_stmt_line") else call._stmt_line),
    }


def _root_var(tree: ast.AST, agents: Dict[str, dict]) -> Optional[str]:
    """Find which variable is the app's root agent.

    Usually `root_agent = SomeAgent(...)`, which is already in `agents`. But the
    convention in this codebase is to build the agent under a descriptive name
    and then alias it — `root_agent = gmail_inbox_triage_agent` — so ADK's loader
    can find it. An alias is a plain Name assignment, not a call, so the agent
    scan above skips it and the tree would otherwise have no root at all.
    """
    if "root_agent" in agents:
        return "root_agent"
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "root_agent"
                and isinstance(node.value, ast.Name)
                and node.value.id in agents):
            return node.value.id
    return None


def _build_tree(agents: Dict[str, dict], root_var: Optional[str] = "root_agent") -> List[dict]:
    """Flatten root_agent's tree into ordered entries with parent/depth/order.

    Flat-with-parent rather than nested: the frontend renders an ordered rail, and
    `order` is load-bearing — for a LoopAgent it is the execution order of the step.
    """
    out: List[dict] = []
    seen = set()

    def walk(var: str, parent: Optional[str], depth: int, order: int):
        if var in seen or var not in agents:
            return
        seen.add(var)
        entry = dict(agents[var])
        entry.update({"parent": parent, "depth": depth, "order": order,
                      "sub_agents": [agents[s]["name"] for s in entry["sub_agent_vars"] if s in agents]})
        out.append(entry)
        for i, child in enumerate(entry["sub_agent_vars"]):
            walk(child, entry["name"], depth + 1, i)

    if root_var and root_var in agents:
        walk(root_var, None, 0, 0)
    # Anything not reachable from root_agent is still real code worth showing.
    for var in agents:
        if var not in seen:
            entry = dict(agents[var])
            entry.update({"parent": None, "depth": 0, "order": len(out),
                          "sub_agents": [], "unreachable": True})
            out.append(entry)
    return out


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------

def parse_app(app_dir: str) -> dict:
    """Describe one ADK app from `<app_dir>/agent.py`. Never raises."""
    app = os.path.basename(app_dir.rstrip(os.sep))
    agent_py = os.path.join(app_dir, "agent.py")
    base = {"app": app, "path": agent_py}

    try:
        st = os.stat(agent_py)
    except OSError as exc:
        return {**base, "status": "error", "agents": [],
                "error": {"type": "OSError", "message": str(exc)}}

    base.update({
        "mtime": st.st_mtime,
        "size": st.st_size,
    })

    cached = _CACHE.get(agent_py)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]

    try:
        with open(agent_py, "r", encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src, filename=agent_py)
    except SyntaxError as exc:
        # Mid-edit is a normal state for a file the overlord rewrites. Show the
        # last good team alongside the error rather than blanking the page.
        payload = {**base, "status": "error", "sha": None,
                   "error": {"type": "SyntaxError", "message": exc.msg,
                             "line": exc.lineno, "text": (exc.text or "").rstrip()},
                   "agents": _LAST_GOOD.get(app_dir, {}).get("agents", []),
                   "stale": bool(_LAST_GOOD.get(app_dir))}
        return payload
    except (OSError, ValueError) as exc:
        return {**base, "status": "error", "agents": [],
                "error": {"type": type(exc).__name__, "message": str(exc)}}

    ctx = _Ctx(tree, src)
    lines = src.splitlines()
    tools: Dict[str, dict] = {}
    for path in set(_import_map(tree, app_dir).values()):
        tools.update(_load_tool_defs(path))

    found: Dict[str, dict] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target, value = node.targets[0], node.value
        if not (isinstance(target, ast.Name) and isinstance(value, ast.Call)):
            continue
        cls = value.func.id if isinstance(value.func, ast.Name) else getattr(value.func, "attr", "")
        if not cls.endswith(_AGENT_SUFFIX):
            continue
        value._stmt_line = node.lineno  # comments sit above the statement, not the call
        found[target.id] = _parse_agent(target.id, value, ctx, tools, lines)

    root_var = _root_var(tree, found)
    payload = {
        **base,
        "status": "ok",
        "sha": hashlib.sha1(src.encode("utf-8")).hexdigest(),
        "docstring": ast.get_docstring(tree),
        "root": found.get(root_var, {}).get("name") if root_var else None,
        "agents": _build_tree(found, root_var),
        "error": None,
        "stale": False,
    }
    _CACHE[agent_py] = (st.st_mtime_ns, st.st_size, payload)
    _LAST_GOOD[app_dir] = payload
    return payload


# list_apps() and app_sha() lived here and are gone with the /opt/adk/apps
# layout they walked: one directory of sibling apps, each with an agent.py at
# its root. The workflows project nests agents under app/agents/<name>/, which
# adk_live enumerates from the server. parse_app() above stays — it is how a
# pipeline root that app-info cannot describe still gets read.
