---
description: Moving the stack to another machine without dragging one machine's state along.
---

# Installing on a new host

The goal is that **a fresh clone plus a `.env` produces a working stack**. What
is versioned, what is runtime state, and what has to exist on the host are kept
strictly separate — getting that split wrong is how a portable stack quietly
grows a dependency on one machine.

## Deploy

```bash
git clone <repo-url> hermes-infra && cd hermes-infra

cp docker/.env.example docker/.env             # fill in; nothing is committed

# optional: put runtime state somewhere other than ~/.hermes
# echo 'HERMES_DATA_DIR=/srv/hermes-data' >> docker/.env

./hermes/seed.sh                               # config, skills, profiles, scripts

cd docker && docker compose up -d --build
docker compose exec hermes-gateway hermes auth # one-time, per host

cd ..                                          # then create the dev profile and
./hermes/install-gsd.sh --profile dev          # install GSD into it
```

The `dev` profile is deliberately not seeded from this repo — a cloned profile
config carries credentials. Create it on the host; see
[Profiles](../architecture/profiles.md#recreating-dev-on-a-new-host).

## What `seed.sh` does

It renders the versioned material in `hermes/` into the runtime data directory.

It is **idempotent and only writes files that are absent**, so it is safe to
re-run against a live deployment — it will not overwrite a config you have since
edited. The corollary is that it will not update one either: to pick up a changed
template, move the existing file aside and re-run.

!!! warning "The exception: `--update-instructions`"

    `./hermes/seed.sh --update-instructions` overwrites `skills/` and every
    `SOUL.md` from the repo. Those are *instruction* rather than state — nothing
    an agent does edits them — so copy-if-absent would mean a correction never
    reaches a deployment that already has the old copy.

    **Run it after editing any skill or SOUL**, or the running agent keeps
    following the old one with no sign anything is stale. Config, memories and
    the board remain copy-if-absent.

It writes **real host paths** into `config.yaml`. These cannot be environment
variables, because Hermes reads the file directly and the tool sandbox is created
through the host's Docker socket — so the mount paths in it must resolve on the
host, not inside a container.

!!! warning

    Run `seed.sh` and any `hermes` CLI command as the host user who owns the data
    directory, or through `docker compose exec`. Commands run inside the
    container execute as **root** and leave root-owned files behind that the
    gateway then cannot read. Placeholders such as `${HERMES_HOME}` also resolve
    only inside the gateway container; they are unset for a host-side CLI
    invocation.

## Credentials are per host

Each deployment mints its own with `hermes auth`. **Never copy credentials
between hosts** — a shared OAuth token can be invalidated by another host's
refresh, which presents as an intermittent, hard-to-attribute auth failure on the
machine that did not do the refreshing.

## Verifying a new deployment

### 1. Models resolve, and the day's spend is known

```bash
curl -s http://127.0.0.1:8020/cost | python3 -m json.tool
```

Returns today's spend, the cap, and the alias map actually in force. If
`aliases` shows the built-in defaults rather than your file, the
`${HERMES_DATA_DIR}/config` mount is not landing — run `hermes/seed.sh`.

### 2. An alias edit applies without a restart

Change a model in `${HERMES_DATA_DIR}/config/model-aliases.yaml` and re-run the
`curl` above. The new value should appear immediately: the file is read live,
keyed on its mtime. That is what replaced the proxy, so it is worth proving
once.

Hermes's own models are separate — they live in its `config.yaml` and the
operator console edits them. Changing one there does restart the gateway.

### 3. The cost cap refuses rather than warns

Set `WORKFLOWS_DAILY_COST_CAP_USD` to something already exceeded, then invoke
any workflow. It must exit `WORKFLOW REFUSED` **before** dispatching, and the
refusal must appear in the trace file alongside ordinary run outcomes — not
only on stderr. Put the cap back afterwards.

### 4. All containers are up

```bash
docker compose ps
```

Expect `hermes-gateway`, `hermes-dashboard`,
`hermes-light-dashboard`, `hermes-workflows`, `hermes-review-executor`,
`hermes-browser`, `hermes-docs`.

### 5. Memory actually writes

Run a contact refresh, then read the file it produced:

```bash
ls "${HERMES_DATA_DIR:-$HOME/.hermes}/wiki"
```

The file is the record — if it is there, the write happened. There is no
asynchronous ingestion step behind it and no `202` to misread.

### 6. A workflow completes end to end

```bash
docker compose exec workflows uv run agents-cli eval run \
  --dataset tests/eval/datasets/<name>/<name>.json \
  --concurrency 1
```

This exercises the model path, which the container healthchecks do not.

## Backups

The only irreplaceable state is the contents of `HERMES_DATA_DIR` — sessions,
kanban, cron jobs, memories, the wiki, and credentials. It is not in git, by
design, and it is a directory rather than a volume, so an ordinary file backup
covers it.

```bash
tar czf "hermes-data-$(date +%Y%m%d).tar.gz" -C "${HERMES_DATA_DIR:-$HOME/.hermes}" .
```

!!! warning "Back up the browser profile like a credential"

    `${HERMES_DATA_DIR}/browser/profile` holds live session cookies for
    everything the headless browser has logged into. It is in the archive above
    — treat that archive accordingly.
