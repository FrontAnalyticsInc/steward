"""Which ADK app does a cron job launch?

Nothing in `cron/jobs.json` says. A job names a `script` and nothing else —
cron's `script` field takes a bare path with no arguments, so the app name and
the run-id policy live inside the wrapper rather than in the job. The link is
therefore recoverable only by reading the wrapper:

    workflows/scripts/run_gmail_inbox_triage.py
                                       APP_NAME = "app.agents.gmail_inbox_triage"

Both spellings are matched because the wrappers are not written to a house
style: whichever of APP / APP_NAME a script happens to use, the app is the
first such literal in the file.

We return that literal verbatim rather than normalising it against the list of
known apps. Two reasons: the caller (`/api/cron/jobs`) then needs no round-trip
to the live ADK server just to describe a job, and the frontend already holds
the team list, so matching happens where the data already is.

Reading, not importing: these wrappers talk to a live ADK server on import-time
module scope, and a dashboard endpoint must not fire a workflow to find out its
name.
"""

import os
import re
from typing import Optional, Tuple

# Both dirs are bind-mounted read-only. The first is the Hermes home's own
# scripts/; the second is the agents-cli project's, which lives outside
# ~/.hermes and so is not reachable through the /opt/data mount.
SCRIPT_DIRS = [
    os.path.join("/opt/data", "scripts"),
    os.getenv("WORKFLOWS_SCRIPTS_DIR", "/opt/workflows/scripts"),
]

# Module-level `APP = "..."` / `APP_NAME = "..."` only. Anchored to column zero
# so an assignment inside a function body — a local, not the app identity —
# does not match.
APP_RE = re.compile(r'^APP(?:_NAME)?\s*=\s*["\']([^"\']+)["\']', re.M)

# Does the wrapper go through invoke_workflow, or does it speak HTTP itself?
# Only invoke_workflow writes the run record under ${ADK_STATE_DIR}/traces, so a
# wrapper that bypasses it produces runs the scorecard never sees — and an app
# whose every run is invisible reads as "never run", which is a different and
# much worse claim than "not recorded". This is what lets the UI tell them apart.
#
# A textual check, for the same reason the app name is: importing the wrapper
# would invoke the workflow. It can only be evidence, never proof — hence
# `records_runs`, not `traced`.
INVOKER_RE = re.compile(r"^\s*(?:from|import)\s+invoke_workflow\b|invoke_workflow\s*\(", re.M)

# path -> (mtime, app, records_runs). Wrappers change rarely and this is read on
# every /api/cron/jobs call, which the dashboard polls.
_cache: dict = {}


def _find(script: str) -> Optional[str]:
    """Absolute path for a job's `script`, or None if no candidate exists."""
    if not script:
        return None
    if os.path.isabs(script):
        return script if os.path.isfile(script) else None
    for base in SCRIPT_DIRS:
        candidate = os.path.join(base, script)
        if os.path.isfile(candidate):
            return candidate
    return None


def resolve_app(script: str) -> Tuple[Optional[str], Optional[str], Optional[bool]]:
    """(app_literal, script_path, records_runs) for a job's script.

    Any element is None when it cannot be determined: a script that does not
    exist on any search path, or one that names no app because it is not an ADK
    wrapper at all. Both are ordinary — most jobs are not ADK jobs — so failure
    is silent and the job simply reads as unattached.

    `records_runs` is None rather than False when the script could not be read.
    Unknown and "confirmed bypasses the invoker" are different claims, and only
    the second one justifies warning about a job.
    """
    path = _find(script)
    if not path:
        return None, None, None

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, None, None

    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1], path, cached[2]

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError:
        return None, path, None

    match = APP_RE.search(source)
    app = match.group(1) if match else None
    records_runs = bool(INVOKER_RE.search(source))
    _cache[path] = (mtime, app, records_runs)
    return app, path, records_runs
