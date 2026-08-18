#!/usr/bin/env python
"""Fetch a batch of inbox messages to JSONL. No model, no agent, no ADK.

    uv run scripts/fetch_inbox.py --query "in:inbox newer_than:2d" --limit 25
    uv run scripts/fetch_inbox.py --out fixtures/monday.jsonl

Standalone on purpose. The fetch is the one part of triage with no judgement in
it, so it should be runnable, inspectable and testable without starting an agent
server. Three things that buys:

  A fixture generator. `--out` writes exactly the shape the classifier consumes,
  so a real inbox becomes an eval dataset by redirecting a file.

  A credential check. If Gmail access is misconfigured, this fails here with the
  API's own error rather than as an empty batch three stages into a pipeline run.

  A dry run. `--dry-run` lists what would be fetched without pulling bodies,
  which is the cheap way to check a query before pointing automation at it.

The ADK stage calls the same functions in app/gmail_api.py, so what you see here
is what the pipeline gets — not a parallel implementation that can drift.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import gmail_api  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--query",
        default=os.getenv("GMAIL_TRIAGE_QUERY", "in:inbox newer_than:2d"),
        help="Gmail search query (default: the pipeline's own query)",
    )
    parser.add_argument("--limit", type=int, default=25, help="max messages (default 25)")
    parser.add_argument("--out", help="write JSONL here (default: stdout)")
    parser.add_argument(
        "--dry-run", action="store_true", help="list matching ids only; do not fetch bodies"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if not gmail_api.configured():
        print(
            "No Gmail credential configured. Set either:\n"
            f"  {gmail_api.SERVICE_ACCOUNT_FILE} + {gmail_api.DELEGATED_USER}  (service account, preferred)\n"
            f"  {gmail_api.TOKEN_FILE}                                        (run scripts/gmail_auth.py)",
            file=sys.stderr,
        )
        return 2

    service = gmail_api.build_service()
    ids = gmail_api.search_ids(service, args.query, args.limit)
    print(f"{len(ids)} message(s) match {args.query!r}", file=sys.stderr)

    if args.dry_run:
        for mid in ids:
            print(mid)
        return 0

    messages, errors = gmail_api.fetch_messages(service, ids)
    for mid, err in errors.items():
        print(f"WARNING: {mid} failed: {err}", file=sys.stderr)

    lines = [json.dumps(m, ensure_ascii=False) for m in messages]
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"wrote {len(messages)} message(s) to {args.out}", file=sys.stderr)
    else:
        for line in lines:
            print(line)

    # Non-zero when anything was requested and nothing came back, so a cron
    # wrapper notices a broken credential instead of logging a quiet success.
    return 1 if ids and not messages else 0


if __name__ == "__main__":
    raise SystemExit(main())
