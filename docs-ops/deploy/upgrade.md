# Upgrading a Steward install

Steward is upgraded by `update`, a shell script installed at
`${STEWARD_HOME}/update` (default `/srv/steward/update`, or
`~/steward/update` on macOS) and
published as an artifact on every release.

It was called `hermes-update` up to and including v0.1.3. The rename is a clean
break with no shim — see [the crossing](#crossing-the-rename) below, which is
the one upgrade that needs a hand.

```bash
/srv/steward/update --to v0.4.0 --dry-run   # print the plan, change nothing
/srv/steward/update --to v0.4.0             # apply it
```

`--to` is **required**, and there is no default. The script ships inside a
release and is installed from it, so the copy on a v0.1.0 box has v0.1.0 pinned
into it and no way to learn that anything newer exists. Defaulting to that
pinned value meant a bare `update` snapshotted the data disk, re-pulled
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
the silent-skew path above. Use `update`.

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
workaround. `update --to <the same tag>` warns and continues when the
target equals the current tag, for exactly this case.

## Crossing the rename

The runner installs itself: at the end of a successful upgrade it copies
`${STEWARD_HOME}/src/update.sh` over `${STEWARD_HOME}/update` and deletes
`${STEWARD_HOME}/hermes-update` if it is still there. That step was added in the
release that did the rename, which means the copy of the runner that performs
*that* upgrade — the old one, already on the box — does not have it. It is the
only upgrade in the sequence that cannot fix its own name.

So on a box installed at v0.1.3 or earlier, run the crossing upgrade with the
old name, then do the swap once by hand:

```bash
/srv/steward/hermes-update --to <tag>
install -m 0755 /srv/steward/src/update.sh /srv/steward/update
rm -f /srv/steward/hermes-update
```

Every upgrade after that keeps the runner current on its own. There is
deliberately no `hermes-update` shim: a wrapper that forwards to `update` is a
second name to keep working, and the failure it would prevent — "command not
found" — is the one that tells the operator exactly what happened.

## The containers this release renamed

`container_name:` is a claim on a name and on the ports the container holding it
publishes, so a rename is only safe if the old container is gone before the new
one starts. The runner's `down --remove-orphans` does that (the containers carry
this project's labels, and `steward-init`'s *service* rename makes the old one a
true orphan), and immediately after the `down` it checks the six legacy names by
hand and removes anything left. That check runs on every upgrade and finds
nothing on a box that has already crossed.

| was | is | why |
|---|---|---|
| `hermes-light-dashboard` | `steward-console` | the product's console, :9120 |
| `hermes-workflows` | `adk-workflows` | the ADK pipeline runner |
| `hermes-review-executor` | `steward-review-executor` | |
| `hermes-browser` | `steward-browser` | a page renderer |
| `hermes-docs` | `steward-docs` | the docs site |
| `hermes-init` | `steward-init` | **also a compose service name** |
| `hermes-gateway` | unchanged | genuinely the Hermes agent gateway |
| `hermes-dashboard` | unchanged | genuinely Hermes's own dashboard, :9119 |

No compose **service** name changed except `hermes-init` -> `steward-init`, so
`docker compose ... logs light-dashboard` and friends are unaffected. Anything
you have scripted against the old *container* names — `docker logs
hermes-workflows`, a monitoring check keyed on the name — needs updating.

## What it replaces, and what it leaves alone

The data directory is left alone. `steward-init` re-seeds it copy-if-absent, so
anything already there survives, and the whole directory is snapshotted first.
`.env` survives too — exactly one line of it, `IMAGE_TAG`, is rewritten.

`${STEWARD_HOME}/src` does **not** survive. It is deleted and replaced from the
release tarball on every upgrade. It is the extracted release, not state, and
nothing you add there is preserved or reported.

Since the source install builds the workflows image from `src/workflows`,
dropping a custom agent in there looks like it works and is destroyed by the
next upgrade. Put it in `${HERMES_DATA_DIR}/agents` instead, which is mounted
into the running service and is on the disk that survives. See
[customization.md](../reference/customization.md).

To see what an upgrade actually changed, the data directory is a git repository
and is committed on every `steward-init` run:

    git -C /srv/steward/data log --oneline
    git -C /srv/steward/data diff HEAD~1

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
data disk. Re-run `update --to <the version the console reports>`.

The console cannot start an upgrade, by design. It has no Docker socket — giving
it one would trade a button for root on the host, on a service that is reachable
from the LAN — and `compose down` would kill the console mid-request anyway.

## Writing a migration

See `docker/hermes-init/migrations/README.md` in the infrastructure repo.
Numbered `NNNN_short_name.{sh,py}`, forward-only, idempotent, non-zero exit on
failure. They run with the data disk mounted at `/opt/data` and the rest of the
stack stopped.
