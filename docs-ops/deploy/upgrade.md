# Upgrading a Steward install

Steward is upgraded by `hermes-update`, a shell script installed at
`${STEWARD_HOME}/hermes-update` (default `/srv/steward/hermes-update`) and
published as an artifact on every release.

```bash
/srv/steward/hermes-update --to v0.4.0 --dry-run   # print the plan, change nothing
/srv/steward/hermes-update --to v0.4.0             # apply it
```

`--to` is **required**, and there is no default. The script ships inside a
release and is installed from it, so the copy on a v0.1.0 box has v0.1.0 pinned
into it and no way to learn that anything newer exists. Defaulting to that
pinned value meant a bare `hermes-update` snapshotted the data disk, re-pulled
the images already running, and printed `Steward is on v0.1.0` — a convincing
report of an upgrade that did not happen. It now refuses and names the flag.

Steward does not check for updates. Releases are at
[github.com/FrontAnalyticsInc/steward/releases](https://github.com/FrontAnalyticsInc/steward/releases);
the tag from that page is what goes after `--to`.

Passing `--to` with the version already installed is allowed, and warns. That is
the supported way to finish an upgrade that failed at the health check — the
marker is written last, so the pending migrations are still pending and get
re-applied from the same starting point.

It runs **on the host**, as the user that installed the stack — the one in the
`docker` group. Not in a container: every step it takes (stopping the stack,
pulling, bind-mounting the data disk into a one-shot migration container) is
resolved by the host's Docker daemon.

## Do not upgrade by re-running the installer

`install.sh` sets up a *new* deployment. Pointing it at a newer version on a box
that already has one is not an upgrade:

- it runs no migrations, so the images move forward and the data disk does not;
- it takes no snapshot, so there is nothing to go back to;
- it never advances `last_migration`, so the marker then lies about what has
  been applied.

It will also refuse to start, because the running stack still holds ports 8642
and 9120. The tempting recovery — `compose down`, then re-run the installer — is
the silent-skew path above. Use `hermes-update`.

## What it does, in order

```
snapshot -> down -> pull -> migrate -> up -> health -> record
```

The ordering is the design. The version marker is written **last**, only after
the upgraded stack answers its healthchecks. An upgrade that dies anywhere
before that leaves a marker naming the *old* version, so a retry re-runs the
same pending migrations from the same starting point. Migrations are required to
be idempotent precisely so that a half-finished upgrade is a repeat rather than
an investigation.

Expect several minutes, most of it the image pull. The console is down for all
of it.

## When it fails

It restores the snapshot, reverts `IMAGE_TAG` in `.env`, prints
`compose logs --tail 50` for whatever was unhealthy, and exits non-zero naming
the step that failed.

The data directory it replaced is **moved aside, not deleted** —
`${HERMES_DATA_DIR}.failed-<timestamp>`. If a migration corrupted something, the
evidence is still there. Remove it yourself once you no longer need it; nothing
cleans it up.

Re-running after a health-check failure is the supported recovery, not a
workaround. `hermes-update --to <the same tag>` warns and continues when the
target equals the current tag, for exactly this case.

## Snapshots

Written to `${STEWARD_HOME}/snapshots/` as gzipped tars of the whole data disk,
before anything else happens. The last 3 are kept; set `STEWARD_KEEP_SNAPSHOTS`
to change that. They are not a backup strategy — they are on the same disk as
the thing they protect, and they exist only to make a failed upgrade
recoverable.

## Forward-only

There is no downgrade path, and there should not be one. A migration that has
restructured data generally cannot be un-run, and a `--downgrade` flag that
quietly fails to restore your data is worse than not having one. Rolling back a
*failed* upgrade is a different thing, and is what the snapshot is for.

Migrations must also tolerate a version skip — an install four releases behind
upgrades straight to current, running every pending migration in order in one
pass. They never touch customer-modified files: wiki content, `SOUL.md`, and
workflows on the data disk belong to the operator.

## Checking state

Settings → About in the console shows the running version, when the install was
first seeded, when it was last updated, and how far migrations have been
applied. It reads `${HERMES_DATA_DIR}/.steward-version` through
`/api/health/services`:

```bash
curl -s http://127.0.0.1:9120/api/health/services | python3 -m json.tool
```

```json
"version": {
  "version": "v0.1.0",
  "seeded_version": "v0.1.0",
  "last_migration": "0000",
  "pending_migrations": [],
  "last_update_at": "2026-08-14T09:12:03Z",
  "steward_home": "/srv/steward"
}
```

**`pending_migrations` non-empty on a running stack means an upgrade applied the
images and did not finish.** The stack is running new code against an unmigrated
data disk. Re-run `hermes-update --to <the version the console reports>`.

The console cannot start an upgrade, by design. It has no Docker socket — giving
it one would trade a button for root on the host, on a service that is reachable
from the LAN — and `compose down` would kill the console mid-request anyway.

## Writing a migration

See `docker/hermes-init/migrations/README.md` in the infrastructure repo.
Numbered `NNNN_short_name.{sh,py}`, forward-only, idempotent, non-zero exit on
failure. They run with the data disk mounted at `/opt/data` and the rest of the
stack stopped.
