#!/usr/bin/env python
"""One-time OAuth consent for this pipeline's OWN Gmail credential.

    uv run scripts/gmail_auth.py --client-secrets ~/secrets/adk-gmail-client.json \
                                --out ~/secrets/adk-gmail-token.json

Only needed for the authorized-user path. A service account with domain-wide
delegation needs none of this — no browser, no refresh token on disk, and an
admin can revoke it centrally. Prefer that for anything scheduled; this exists
for a single mailbox without Workspace admin access.

Use a NEW OAuth client, not the one under ~/.hermes/gmail-mcp. Sharing that grant
would give an unattended, high-volume pipeline the same identity as the
interactive assistant: one consent screen, one quota pool, one thing to revoke,
and no way to tell in an audit log which of the two acted.

Requires a browser on the machine that runs it. On a headless host, run it on a
laptop and copy only the resulting token file across.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import gmail_api  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--client-secrets", required=True, help="OAuth client JSON from Google Cloud console"
    )
    parser.add_argument("--out", required=True, help="where to write the authorized token")
    parser.add_argument(
        "--port", type=int, default=0, help="local redirect port (0 = pick a free one)"
    )
    args = parser.parse_args()

    from google_auth_oauthlib.flow import InstalledAppFlow

    print(f"Requesting scopes: {', '.join(gmail_api.SCOPES)}", file=sys.stderr)
    flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets, gmail_api.SCOPES)
    creds = flow.run_local_server(port=args.port)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(creds.to_json(), encoding="utf-8")
    # The token carries a refresh token: a long-lived credential, readable by
    # anyone who can read the file.
    os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)

    print(f"wrote {out} (mode 0600)", file=sys.stderr)
    print(f"Set {gmail_api.TOKEN_FILE}={out} for the workflows service.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
