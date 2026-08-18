# What you can change, and what an upgrade does to it

Two questions, and they only have one answer between them: if you change
something on this box, does it still exist after `hermes-update`?

For everything below the answer is yes. This page is about how that is arranged,
because the arrangement is what tells you where to put a change so it stays put.

## The rule

**The data directory belongs to you. Everything else belongs to the release.**

`${HERMES_DATA_DIR}` — `/srv/steward/data`, or `~/steward/data` on macOS — is
never overwritten by an upgrade. `hermes-init` re-seeds it with copy-if-absent
and `rsync --ignore-existing`, so a file that is already there is left alone,
and `hermes-update` tars the whole directory to `${STEWARD_HOME}/snapshots/`
before it stops anything.

`${STEWARD_HOME}/src` is the opposite: `hermes-update` deletes and replaces it
from the release tarball. It is a checkout, not your state.

!!! danger "Do not edit anything under `/srv/steward/src`"

    It is the extracted release. An upgrade replaces the whole directory, with
    no warning and no diff, and an agent you added there is simply gone. Every
    customisation below has a home on the data disk instead — use it.

## Where each thing lives

| What | Path under the data dir | How you change it |
|---|---|---|
| Character, standing rules | `SOUL.md` | Edit the file, restart the gateway |
| What it knows about you | `memories/USER.md` | Ask it in chat; it writes the file |
| What it knows about others | `wiki/` | Written by workflows; readable in the console |
| Which models workflows use | `config/model-aliases.yaml` | Edit the file, restart `workflows` |
| **Your own workflows** | `agents/` | See below |
| Uploaded credentials | `secrets/` | Console → Settings → Integrations |
| Channels, MCP servers | (console-managed) | Console, which writes them for you |
| Secrets, ports, model keys | `${STEWARD_HOME}/stack/.env` | Edit, then `up -d` |

`.env` is preserved too: `install.sh` keeps an existing one with its secrets, and
`hermes-update` rewrites exactly one line in it, `IMAGE_TAG`.

## Your own workflows

`${HERMES_DATA_DIR}/agents` is mounted read-only into the workflows service at
`/code/agents_local`, which is where `HERMES_AGENTS_PATH` points. Anything there
is loaded after the agents we ship.

    /srv/steward/data/agents/
      weekly_supplier_digest/
        __init__.py          # exports root_agent
        agent.py

A directory whose name matches one of ours **replaces** it — overlays are
searched last and win — which is how you keep our routing and change the
behaviour. To switch one of ours off instead, set `HERMES_DISABLED_AGENTS` in
`.env` to a comma-separated list of bare module names.

Agents are imported once, at startup. A new or edited agent does nothing until:

    docker compose -f /srv/steward/stack/steward-stack.yml \
                   --env-file /srv/steward/stack/.env restart workflows

If one of your agents fails to import it is **skipped, not fatal** — everything
else keeps running, and the reason is logged and served at `/agent-load-errors`
on the workflows service. Check there when a workflow you added is not in the
playground. (An agent *we* ship behaves the opposite way and stops the service,
because that means the release is wrong rather than your file.)

`${HERMES_DATA_DIR}/agents/README.md` has the full contract.

## The data directory is a git repository

`hermes-init` runs `git init` on it the first time and commits on every run —
including the one at the end of an upgrade. Nobody has to remember to do this,
which is the only reason it is worth anything.

    cd /srv/steward/data

    git log --oneline              every release this box has passed through
    git diff HEAD~1                what the last upgrade changed
    git status                     what you have changed since
    git checkout HEAD~1 -- SOUL.md put something back

So "did the upgrade touch my character file?" is a command rather than a
recollection, and if it ever does, the previous version is one checkout away.

### What is committed

An allowlist, in `${HERMES_DATA_DIR}/.gitignore`: `SOUL.md`, `agents/`,
`config/`, `memories/`, `profiles/`, `skills/`, `wiki/`, `.steward-version`.

Ignore-by-default is deliberate. Anything a future release starts writing to the
data disk is untracked until someone decides otherwise — a new state file merely
misses a commit, whereas a new secret would be committed permanently.

Two things are excluded that otherwise look like they belong:

- **`config.yaml`** and **`profiles/*/config.yaml`** carry the dashboard secret
  and password hash, and record absolute host paths true of this box alone.
- **`*.db`, `sessions/`, `adk/`, `secrets/`, `approvals/`** are runtime state:
  regenerated, enormous, or private.

### Exporting

The repository is the export:

    git -C /srv/steward/data bundle create ~/steward-config.bundle --all

One file, complete history, restorable with `git clone`. It carries no
credentials, by construction — the excluded paths above are exactly the ones
that hold them.

!!! note "Not a backup of everything"

    The bundle is your *configuration*. Sessions, the kanban board, the approval
    queue and ADK state are not in it. Those are what the upgrade snapshots in
    `${STEWARD_HOME}/snapshots/` are for.

## The one thing that does not update

Instruction files — `SOUL.md`, `skills/`, `profiles/*/SOUL.md` — are seeded
copy-if-absent and **never refreshed by an upgrade**. A correction we make to a
shipped skill does not reach a box that already has the old copy.

`hermes/seed.sh --update-instructions` overwrites them from the release, and is
the only way to get such a correction — but it is all-or-nothing and will
discard your edits to those files.

Being able to see exactly what it would discard is most of why the data
directory is a git repository. Run it, then look:

    git -C /srv/steward/data diff

Keep what you want, `git checkout` the rest.
