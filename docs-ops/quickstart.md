---
description: From a fresh clone to a running stack.
---

# Quickstart

This gets the whole stack running on one host. Budget 15 minutes, most of it
image pulls and the first local builds.

!!! note

    Every model call goes to Anthropic, so you need one provider key before
    anything will answer. There is no proxy container to start, and no
    interactive login — the key is read from the environment. Pointing the
    workflows at a local Ollama is a development-only option; no deployment
    uses it.

## 1. Clone

```bash
git clone <repo-url> hermes-infra
cd hermes-infra
```

## 2. Create your environment file

Nothing secret is committed, so you always start from the template.

```bash
cp docker/.env.example docker/.env
```

Open `docker/.env` and set, at minimum:

- `ANTHROPIC_API_KEY` — the model key, read by the gateway and the workflows
- `WORKFLOWS_DAILY_COST_CAP_USD` — workflows refuse to start past this much
  spend in a UTC day. Defaults to `10`; `0` disables it
- `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` and `..._SECRET`
- `API_SERVER_KEY` — ships EMPTY and the gateway refuses to start without it.
  Generate one with `openssl rand -hex 32`. It used to ship as a placeholder
  string, which is to say it shipped as a published credential
- `BROWSER_TOKEN` — also ships empty; the page renderer 503s everything without
  it rather than serving openly

Full reference: [Configuration](deploy/configuration.md).

## 3. Seed the Hermes home

`seed.sh` renders config, identity, skills, profiles and scripts into the
runtime data directory.

```bash
./hermes/seed.sh
```

It is idempotent and **only writes files that are absent**, so it is safe to
re-run against a live deployment. It writes real host paths into `config.yaml` —
these cannot be environment variables, because the tool sandbox is created
through the host's Docker socket and its mount paths must resolve on the host.

## 4. Build and launch

```bash
cd docker
docker compose up -d --build
```

First run builds the dashboard, workflows and browser images; later runs reuse them.

## 5. Authenticate the gateway

One-time, and per host — credentials are never copied between deployments.

```bash
docker compose exec hermes-gateway hermes auth
```

!!! warning

    Run `hermes` through `docker compose exec`, not on the host. Commands run
    inside the container execute as root and leave root-owned files in the data
    directory that the gateway itself cannot read afterwards.

## 6. Create the `dev` profile

The stack ships three profiles. Two are seeded from the repo; `dev` is not,
because a cloned profile config carries credentials.

```bash
cd ..
./hermes/install-gsd.sh --profile dev
```

Full procedure and the reasoning behind the split:
[Profiles](architecture/profiles.md#recreating-dev-on-a-new-host).

## 7. Verify

```bash
# Every service that can answer for itself declares a healthcheck, so compose
# is the honest check — "up" alone only means a container was created.
cd docker && docker compose ps

# The gateway, end to end. This is the one that proves the model key works.
curl -s -H "Authorization: Bearer $API_SERVER_KEY" \
     http://127.0.0.1:8642/v1/models
```

Then open the interfaces:

| Interface | Address |
|---|---|
| Operator dashboard | `http://127.0.0.1:9120` |
| Hermes UI | `http://127.0.0.1:9119` |
| ADK workflows | `http://127.0.0.1:8020` |
| Documentation | `http://<host>:9121` — also readable from the LAN |

## Everyday operations

```bash
cd docker

docker compose logs -f              # follow everything
docker compose logs -f workflows    # or one service

docker compose down                 # stop, keep data
docker compose down -v              # stop and remove named volumes
```

!!! note

    `down -v` is far less dangerous than it used to be. Every piece of state
    that matters — config, databases, the wiki, the approvals queue — lives in
    bind mounts under `HERMES_DATA_DIR`, not in named volumes, so `down -v`
    does not touch it. The only named volumes left are orphans from the
    telemetry stack that was removed.

## Next

<div class="grid cards" markdown>

-   __Architecture__

    ---

    What each of the eight services actually does.

    [:octicons-arrow-right-24: Overview](architecture/overview.md)

-   __Deploy elsewhere__

    ---

    Moving the stack to another host without dragging state along.

    [:octicons-arrow-right-24: Installing on a new host](deploy/install.md)

</div>
