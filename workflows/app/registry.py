"""Find the workflow agents this deployment should run.

Replaces a hand-maintained list in app/agent.py. That list was the single
file every new agent had to edit, which made it a guaranteed conflict point:
two agents added on two branches collide there every time, and with more than
one operator it stops being merge-able at all.

Two halves, on purpose:

    find_agent_modules()  pure. Walks directories, returns module names.
    load_agents()         imports them and pulls out the agent objects.

The split is what makes discovery testable. The scanning half — where the
ordering, filtering and overlay rules live, and so where the bugs will be —
can be exercised against temporary directories without importing a single
agent, and therefore without a model config, a credential, or any of the API
clients an agent drags in. (`app/__init__` still imports ADK, so the tests
run where the project's other tests do; the point is that nothing under
test needs an agent to be loadable.)

Ordering is alphabetical by module name rather than insertion order. The old
list was hand-ordered, and the root agent's prompt is built from it, so an
unstable order would quietly change routing behaviour between runs.
"""

from __future__ import annotations

import importlib
import logging
import os
import pathlib
import sys
from typing import Any

_log = logging.getLogger(__name__)

# Colon-separated, like PATH. A tenant deployment points this at its own agent
# directory so it can add workflows without editing anything shipped here.
AGENTS_PATH_ENV = "HERMES_AGENTS_PATH"

# Comma-separated module names (bare, e.g. "kestrel_site_kg"). Lets a deployment
# switch off a shipped agent without forking the file that defines it.
DISABLED_ENV = "HERMES_DISABLED_AGENTS"

_BUILTIN_AGENTS_DIR = pathlib.Path(__file__).parent / "agents"


class AgentLoadError(RuntimeError):
    """An agent directory exists but could not be turned into an agent.

    Raised rather than skipped, for anything we ship. A silently dropped agent
    is the exact failure the old registry produced — the workflow imports fine,
    its runner script works, and it is simply absent from the playground with
    nothing to explain why. Failing here costs a startup crash and saves that
    hunt.

    Agents from an overlay directory are the exception, and load_agents()
    documents why.
    """


def _disabled() -> set[str]:
    raw = os.getenv(DISABLED_ENV, "")
    return {name.strip() for name in raw.split(",") if name.strip()}


def _search_roots() -> list[pathlib.Path]:
    """Built-in agents first, then each overlay directory in order."""
    roots = [_BUILTIN_AGENTS_DIR]
    for entry in os.getenv(AGENTS_PATH_ENV, "").split(os.pathsep):
        entry = entry.strip()
        if entry:
            roots.append(pathlib.Path(entry))
    return roots


def _is_agent_dir(path: pathlib.Path) -> bool:
    """A package with an __init__.py, not a cache or a private directory."""
    return (
        path.is_dir()
        and not path.name.startswith((".", "_"))
        and (path / "__init__.py").is_file()
    )


def find_agent_modules() -> list[tuple[str, pathlib.Path]]:
    """(import_name, directory) for every agent, sorted, duplicates resolved.

    Pure: touches the filesystem and the environment, imports nothing.

    A later root shadowing an earlier one is deliberate — it is how a tenant
    overrides a shipped agent with its own version of the same name, and it
    mirrors how PATH already behaves in reverse. Built-ins are searched first,
    so an overlay wins.
    """
    disabled = _disabled()
    found: dict[str, pathlib.Path] = {}

    for root in _search_roots():
        if not root.is_dir():
            # An overlay that is configured but absent is not an error: the
            # mount simply is not there in this deployment.
            continue
        builtin = root == _BUILTIN_AGENTS_DIR
        for child in sorted(root.iterdir()):
            if not _is_agent_dir(child) or child.name in disabled:
                continue
            module = f"app.agents.{child.name}" if builtin else child.name
            found[child.name] = (module, child)  # type: ignore[assignment]

    return [found[name] for name in sorted(found)]  # type: ignore[misc]


def _agent_of(module: Any, name: str) -> Any:
    """The agent object a module exports.

    Accepts `root_agent` (what ADK's own loader resolves) or a single
    `<something>_agent`. Anything else is ambiguous and says so, rather than
    picking one and being subtly wrong about which workflow is registered.
    """
    if hasattr(module, "root_agent"):
        return module.root_agent

    candidates = [attr for attr in dir(module) if attr.endswith("_agent")]
    if len(candidates) == 1:
        return getattr(module, candidates[0])
    if not candidates:
        raise AgentLoadError(
            f"{name}: exports no `root_agent` and nothing ending in `_agent`. "
            "An agent module must export one of those to be discoverable."
        )
    raise AgentLoadError(
        f"{name}: exports several agents ({', '.join(sorted(candidates))}) and no "
        "`root_agent` to disambiguate. Add `root_agent = <the one to register>`."
    )


# Overlay agents that could not be loaded, as {module_name: reason}. Populated
# by load_agents() and served by the workflows app so a skipped agent has
# somewhere to be seen — see the module docstring for why they are skipped
# rather than fatal.
_LOAD_ERRORS: dict[str, str] = {}


def load_errors() -> dict[str, str]:
    """Overlay agents skipped on the last load, and why. Empty is the good case."""
    return dict(_LOAD_ERRORS)


def load_agents() -> list[Any]:
    """Every discovered agent, imported. Order matches find_agent_modules().

    A built-in that fails to load raises. An overlay agent that fails to load
    is recorded in `load_errors()` and skipped.

    That asymmetry is the whole point, and it is not a softening of the rule
    above. A broken built-in means the release is wrong, and every deployment
    has the same one, so stopping is both correct and the fastest way to find
    out. A broken overlay means THIS box's own file is wrong — written by an
    operator, on a running system, against a workflow surface that is supposed
    to be editable. Refusing to start there takes every other workflow, the
    playground and the review queue down over one file, and does it at the
    moment its author is least able to read a container log.

    Skipping is only defensible because the error goes somewhere. It is logged
    at ERROR and served at /agent-load-errors; a silent skip would be the exact
    failure this module was written to end.
    """
    agents = []
    _LOAD_ERRORS.clear()
    for module_name, directory in find_agent_modules():
        builtin = module_name.startswith("app.")
        # An overlay lives outside the package tree, so its parent has to be
        # importable before the module name will resolve.
        parent = str(directory.parent)
        if not builtin and parent not in sys.path:
            sys.path.insert(0, parent)
        try:
            module = importlib.import_module(module_name)
            agent = _agent_of(module, module_name)
        except Exception as exc:  # noqa: BLE001 - re-raised or recorded below
            if builtin:
                raise AgentLoadError(
                    f"{module_name}: failed to load ({exc})"
                ) from exc
            _LOAD_ERRORS[module_name] = str(exc)
            _log.error(
                "custom agent %r in %s was skipped: %s", module_name, directory, exc
            )
            continue
        agents.append(agent)
    return agents
