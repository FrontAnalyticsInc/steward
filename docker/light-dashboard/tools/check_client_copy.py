#!/usr/bin/env python3
"""Grep the console's *answers* for names the client must never meet.

The missing half of the naming checks
-------------------------------------
Two checks already existed and both were sound:

* the backend sweep parsed our Python and looked at its string literals;
* the frontend sweep compiled the JSX, stripped comments, and grepped it.

Neither can see a word that is not in our tree. ``GET /api/channels`` said
*"Use Hermes from Slack via Socket Mode"* on Settings > Channels for as long as
those two checks were the whole story, because the sentence lives in the
upstream gateway's channel catalog and arrives over HTTP at request time. A
source check cannot catch that by construction — no amount of care makes it
able to.

So this one reads the wire. It fetches the console's own read-only endpoints
from a running box and walks the JSON it gets back, which is by definition
everything the frontend can render, whoever wrote it.

Running it
----------
    ./check_client_copy.py                        # against http://127.0.0.1:9120
    ./check_client_copy.py --base http://host:9120
    ./check_client_copy.py --payload /api/channels=saved.json   # no box needed

``--payload`` takes a saved response so the check can be pointed at a payload
captured earlier — which is how it is demonstrated to fail against the
pre-fix ``/api/channels``, and how CI can run it with no stack up.

Exit status
-----------
``0``  no unaccepted leak. Accepted ones are still printed, every run.
``1``  at least one leak with no entry in ACCEPTED.
``2``  the console could not be reached, or an endpoint failed in a way that
       means the check did not actually look. A check that cannot see is not a
       pass.

Why there is an ACCEPTED list rather than a clean sweep
------------------------------------------------------
Some of what this finds is not copy at all: environment variable names an
operator has to type character for character, service ids the stack resolves,
toolset ids the gateway keys on. Renaming those in prose tells someone to set
something that is read by nothing — task 15 settled that, and this check must
not reopen it.

The rest of ACCEPTED is real debt on surfaces this task did not own, recorded
with the reason and printed on every run rather than filtered into silence.
The entries are narrow — an endpoint and a path pattern — so a *new* leak on a
surface that already has an accepted one still fails. That is the property that
matters: the whole reason this check exists is that a broad, confident belief
("those hits are all comments and identifiers") is what let the last one
through.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

sys.path.insert(0, __file__.rsplit("/", 2)[0])
from backend.client_copy import find_leaks  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:9120"

# Endpoints whose text is *product copy* — strings this console ships, or
# forwards from a catalog someone else ships. Every one of these is rendered as
# chrome on some panel, so every string in them is something a client reads as
# the product talking.
#
# Deliberately excluded, and this is a judgement worth stating rather than
# leaving as a gap in a list: /api/sessions, /api/sessions/{id}/messages,
# /api/kanban, /api/approvals/queue, /api/wiki/*, /api/context/* and
# /api/cron/jobs carry *content* — task bodies an agent wrote about this
# repository, chat transcripts, wiki pages, prompts the client edited. Those
# legitimately say "Hermes" when the work was about Hermes, and sweeping them
# would produce a permanent wall of false positives, which is the failure mode
# that makes a check get ignored. Their client-facing framing is chrome and is
# covered here; their bodies are not this check's business.
ENDPOINTS: Tuple[str, ...] = (
    "/api/channels",
    "/api/channels/delivery",
    "/api/health/services",
    "/api/mcp/servers",
    "/api/model-config",
    "/api/agents",
    "/api/skills",
    "/api/integrations",
    "/api/settings/integrations",
    "/api/setup/state",
    "/api/automations/library",
    "/api/timezone",
    "/api/adk/health",
    "/api/adk/teams",
    "/api/adk/fleet",
    "/api/metrics/health",
    "/api/metrics/automations",
)

# Endpoints a build may legitimately not serve — an older console, or a feature
# switched off. A 404 on one of these is skipped rather than failing the run;
# a 404 on anything else means the check did not look where it claimed to.
OPTIONAL = frozenset(
    {
        "/api/channels/delivery",
        "/api/model-config",
        "/api/setup/state",
        "/api/automations/library",
        "/api/adk/health",
        "/api/adk/teams",
        "/api/adk/fleet",
        "/api/metrics/health",
        "/api/metrics/automations",
    }
)


class Accepted(NamedTuple):
    endpoint: str      # fnmatch pattern against the endpoint path
    path: str          # fnmatch pattern against the JSON path, indices blanked
    value: str         # regex the string itself must match
    reason: str


#: List indices are blanked before an ACCEPTED path is matched, so an entry
#: reads ".services[].target" rather than needing one per row. It also has to
#: be done rather than written as a glob: fnmatch reads "[*]" as a character
#: class holding a literal asterisk, so the obvious spelling silently matches
#: nothing — which is how the first draft of this file "accepted" three entries
#: that were in fact failing.
_INDEX = re.compile(r"\[\d+\]")


def _blank_indices(path: str) -> str:
    return _INDEX.sub("[]", path)


# Narrow on purpose: endpoint + JSON path + the shape of the value. An entry
# that said "everything under /api/skills" would hide the next leak there.
ACCEPTED: Tuple[Accepted, ...] = (
    # --- not copy: things a human or a machine types verbatim --------------
    Accepted(
        "*", "*#key", r".*",
        "A field name, not copy. The frontend renders by these keys and the "
        "gateway writes env by them; task 32 forbids renaming them outright. "
        "Reported so a shape change is still visible.",
    ),
    Accepted(
        "*", "*", r"^HERMES_[A-Z0-9_]+$",
        "An environment variable name. Renaming it in prose tells an operator "
        "to set something that is read by nothing (task 15).",
    ),
    Accepted(
        "*", "*", r"^hermes-(gateway|dashboard|cli)$",
        "A service or toolset id the stack resolves and the gateway keys on.",
    ),
    Accepted(
        "*", "*", r"\$\{HERMES_HOME\}",
        "An MCP launch command line. It is the path that is actually executed.",
    ),
    Accepted(
        "/api/settings/integrations", ".identities[].change_with", r"^hermes ",
        "A CLI command an operator runs. Shown so it can be copied, not read.",
    ),
    Accepted(
        "*", "*", r"^[~/][\w./-]*$",
        "A filesystem path. It is where the file actually is, and an operator "
        "reading it needs it to be true.",
    ),
    Accepted(
        "/api/settings/integrations", ".workflow_access[].guardrail",
        r"~/\.hermes/",
        "Names the on-disk credential directory it is contrasting against. The "
        "path is the substance of the sentence, not decoration.",
    ),

    # --- real debt, on surfaces this task did not own ---------------------
    Accepted(
        "/api/skills", "*", r".*",
        "KNOWN DEBT. The gateway's bundled skill library is forwarded whole: "
        "~100 rows whose author is 'Hermes Agent' or 'Nous Research', plus "
        "skill names and descriptions about the ingredient's own desktop and "
        "TUI. It is a catalog this console does not write and cannot usefully "
        "scrub — the skills genuinely are about those products — so it needs a "
        "decision about which of them a client should see at all, not a "
        "rewrite. Not task 32.",
    ),
    Accepted(
        "/api/adk/teams", "*.description", r".*",
        "KNOWN DEBT. Pipeline descriptions are the workflow modules' own "
        "docstrings, which say 'ADK workflow'. Principle 6 puts ADK in the "
        "workflows tree and out of the console, so the fix is in "
        "workflows/app/agents/*, not a map here. Not task 32.",
    ),
    Accepted(
        "/api/agents", "[].description", r"\bADK\b",
        "KNOWN DEBT. An agent profile's own description, read from the "
        "gateway's profile config rather than written here. Same decision as "
        "the pipeline docstrings below, on a different file. Not task 32.",
    ),
    Accepted(
        "/api/health/services", ".services[].target", r".*",
        "A container URL. It is the address that was actually probed, and an "
        "operator reading a red row needs it to be true.",
    ),
    Accepted(
        "/api/integrations", ".gaps[].detail", r".*",
        "KNOWN DEBT. The integration-gap notes are written for whoever would "
        "fix the gap and name the upstream component that has to change. "
        "Whether that panel should be client-visible at all is the open "
        "question, and it is not this task's.",
    ),
)


def _accepts(endpoint: str, path: str, value: str) -> Optional[Accepted]:
    for entry in ACCEPTED:
        if not fnmatch.fnmatch(endpoint, entry.endpoint):
            continue
        if not fnmatch.fnmatch(_blank_indices(path), entry.path):
            continue
        if re.search(entry.value, value):
            return entry
    return None


def _fetch(base: str, endpoint: str, timeout: float) -> Tuple[Optional[Any], Optional[str]]:
    """(payload, error). A 404 on an OPTIONAL endpoint is (None, None)."""
    try:
        with urllib.request.urlopen(base.rstrip("/") + endpoint, timeout=timeout) as resp:
            return json.load(resp), None
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and endpoint in OPTIONAL:
            return None, None
        return None, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"unreachable: {exc.reason}"
    except (ValueError, OSError) as exc:
        return None, f"unreadable: {exc}"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"console base URL (default {DEFAULT_BASE})")
    ap.add_argument("--payload", action="append", default=[], metavar="ENDPOINT=FILE",
                    help="read a saved response instead of fetching this endpoint; "
                         "repeatable. Use it to check a captured payload with no box up.")
    ap.add_argument("--only", action="append", default=[], metavar="ENDPOINT",
                    help="check only these endpoints; repeatable.")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args(argv)

    saved: Dict[str, str] = {}
    for spec in args.payload:
        endpoint, _, path = spec.partition("=")
        if not path:
            print(f"--payload wants ENDPOINT=FILE, got {spec!r}", file=sys.stderr)
            return 2
        saved[endpoint] = path

    endpoints = tuple(args.only) if args.only else ENDPOINTS
    # A --payload for something outside ENDPOINTS is still checked: it is an
    # explicit instruction, not a typo to silently drop.
    endpoints = endpoints + tuple(e for e in saved if e not in endpoints)

    failures: List[Tuple[str, str, str, str]] = []
    accepted_hits: List[Tuple[str, str, str, Accepted]] = []
    broken: List[Tuple[str, str]] = []
    looked = 0

    for endpoint in endpoints:
        if endpoint in saved:
            try:
                with open(saved[endpoint], "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except (OSError, ValueError) as exc:
                broken.append((endpoint, f"{saved[endpoint]}: {exc}"))
                continue
        else:
            payload, err = _fetch(args.base, endpoint, args.timeout)
            if err:
                broken.append((endpoint, err))
                continue
            if payload is None:
                continue
        looked += 1
        for path, name, value in find_leaks(payload):
            entry = _accepts(endpoint, path, value)
            if entry:
                accepted_hits.append((endpoint, path, value, entry))
            else:
                failures.append((endpoint, path, name, value))

    if accepted_hits:
        print(f"accepted, still present ({len(accepted_hits)}):")
        seen = set()
        for endpoint, path, value, entry in accepted_hits:
            key = (endpoint, entry.reason)
            count = sum(1 for e, _, _, x in accepted_hits
                        if e == endpoint and x.reason == entry.reason)
            if key in seen:
                continue
            seen.add(key)
            print(f"  {endpoint}  x{count}  e.g. {path}")
            print(f"      {entry.reason}")
        print()

    if broken:
        print(f"could not check ({len(broken)}):", file=sys.stderr)
        for endpoint, why in broken:
            print(f"  {endpoint}: {why}", file=sys.stderr)
        print("\nThe check did not look at these, which is not a pass.", file=sys.stderr)
        return 2

    if failures:
        print(f"LEAK: {len(failures)} string(s) name the ingredient in client-facing copy.\n")
        for endpoint, path, name, value in failures:
            shown = value if len(value) <= 200 else value[:197] + "..."
            print(f"  {endpoint}{path}")
            print(f"      [{name}] {shown!r}")
        print(
            "\nThese are what a client reads. Map them where the console adapts the "
            "payload (see backend/client_copy.py), or add an ACCEPTED entry in this "
            "file saying why the name has to stay."
        )
        return 1

    print(f"clean: {looked} endpoint(s) checked, no unaccepted leak.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
