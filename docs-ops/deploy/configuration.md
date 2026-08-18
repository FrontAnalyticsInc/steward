---
description: Environment variables, the versioned/runtime split, and what is actually exposed on the network.
---

# Configuration

All configuration is in `docker/.env`, created from `docker/.env.example`. Start
from the template every time — nothing secret is committed, so there is no
inherited state to reason about.

## What lives where

The separation between versioned material and runtime state is the thing that
makes this stack portable. It is worth internalising before changing anything.

| | Location | Versioned |
|---|---|---|
| Compose, Dockerfiles | `docker/` | yes |
| ADK workflow agents | `workflows/` | yes |
| Documentation source | `docs/` | yes |
| Hermes config, identity, skills, profiles, scripts | `hermes/` → seeded to `$HERMES_DATA_DIR` | yes, as templates |
| GSD Core skills | `$HERMES_DATA_DIR/profiles/dev/skills/gsd/` | pinned version only (`hermes/install-gsd.sh`) |
| `dev` profile config | `$HERMES_DATA_DIR/profiles/dev/` | **no** — cloned on the host; carries credentials |
| Sessions, kanban, cron jobs, memories | `$HERMES_DATA_DIR` | **no** — runtime state |
| Credentials (`auth.json`, `.env`) | `$HERMES_DATA_DIR`, `docker/.env` | **no** — never commit |

See [Profiles](../architecture/profiles.md) for why `dev` is cloned on the host
rather than seeded, and why GSD is installed rather than vendored.

## Variables

### Secrets — set these before first launch

| Variable | Default | Notes |
|---|---|---|
| `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` | *(empty)* | Guards the Hermes UI, which listens on all interfaces. |
| `HERMES_DASHBOARD_BASIC_AUTH_SECRET` | *(empty)* | `openssl rand -base64 32` |
| `API_SERVER_KEY` | *(empty)* | Required. The gateway refuses to start without it. See below. |
| `ANTHROPIC_API_KEY` | *(empty)* | The model key. Read by the gateway, the dashboard and the workflows. |
| `GEMINI_API_KEY` | *(empty)* | Only if `WORKFLOWS_MODEL_PROVIDER=gemini`. |
| `BROWSER_TOKEN` | *(empty)* | Required when the renderer runs. It fetches any URL it is given; treat as a real credential. |

!!! danger "`API_SERVER_KEY` must be set, and now fails closed"

    This used to default to a placeholder string published in this repository,
    while the gateway's API server ran with `API_SERVER_HOST=0.0.0.0` — so a
    deployment that never set it exposed that API on every interface behind a
    known key. It ships empty now and the gateway refuses to start, which is
    the whole point: a published placeholder is a published credential, and
    failing to boot is louder than hiding one.

    `install.sh` generates it. Setting it by hand:

    ```bash
    echo "API_SERVER_KEY=$(openssl rand -hex 32)" >> docker/.env
    ```

    The same value must reach the gateway, the Hermes dashboard and the
    operator console — desyncing them presents as a console that has lost the
    gateway rather than as a credential problem.

### Models

| Variable | Default | Notes |
|---|---|---|
| `WORKFLOWS_MODEL_PROVIDER` | `anthropic` | Or `ollama` for local development against a host GPU. |
| `WORKFLOWS_OLLAMA_MODEL` | *(see `.env.example`)* | Must be pulled on the **host**. |
| `OLLAMA_API_BASE` | `http://127.0.0.1:11434` | Host loopback; see [Prerequisites](prerequisites.md#ollama-is-not-a-container). |
| `WORKFLOWS_DEFAULT_ALIAS` | `drafting` | Which role a workflow gets when it names none. |
| `MODEL_ALIASES_PATH` | `/code/config/model-aliases.yaml` | The alias map, mounted from `${HERMES_DATA_DIR}/config`. |
| `WORKFLOWS_DAILY_COST_CAP_USD` | `10` | Refuse to dispatch past this much spend in a UTC day. `0` disables. |

### Paths

| Variable | Default | Notes |
|---|---|---|
| `HERMES_DATA_DIR` | `~/.hermes` | All runtime state. Point elsewhere for a fresh deployment. |
| `APPROVALS_DIR` | `~/approval-queue/approvals` | Outbound approval queue, read by the operator console. |
| `OBSIDIAN_VAULT_PATH` | *(unset)* | Only if a workflow targets a real vault. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(unset)* | Optional. Unset means no spans leave the box; run records still land as JSONL either way. Point it at a collector of your own to turn tracing on. ADK speaks `http/protobuf` only. |

### Ports

`HERMES_GATEWAY_PORT` `8642` · `HERMES_UI_PORT` `9119` ·
`LIGHT_DASHBOARD_PORT` `9120` · `WORKFLOWS_PORT` `8020` ·
`BROWSER_PORT` `3010` · `DOCS_PORT` `9121`

`DOCS_BIND` (default `0.0.0.0`) controls which interface the documentation is
served on. It is the only service here whose exposure is a one-variable change,
because it is the only one whose content is meant to be read by anyone. Set it to
`127.0.0.1` to restrict the docs to the host.

## Network exposure

This is the single most important section on this page. Since the move to a
single bridge network, **what a service binds inside its container no longer
determines what the network can reach** — the compose port mapping does, and
every mapping but one is loopback.

| Service | Published on | Auth | Notes |
|---|---|---|---|
| ADK workflows | `127.0.0.1:8020` | none | Loopback only, deliberately. |
| Gateway API | `127.0.0.1:8642` | `API_SERVER_KEY` | Required; the gateway will not start without it. |
| Hermes UI | `127.0.0.1:9119` | basic auth | Set a real password before widening. |
| Operator console | `127.0.0.1:9120` | **none** | `DASHBOARD_BIND` widens it. See below. |
| Browser renderers | `127.0.0.1:3010`, `:3011` | `TOKEN` | Behind `--profile browser` / `authenticated`. Tokens are real credentials. |
| Documentation | **`0.0.0.0:9121`** | none | The one deliberate exception; `DOCS_BIND` closes it. |

!!! danger "The operator console has no authentication at all"

    It exposes chat, the kanban board, the approvals queue, and the contents of
    the Hermes data directory, and it holds the gateway's API key — so anything
    that can reach it can act as the agent. There is no password on it.

    It is bound to `127.0.0.1` by default, and **`DASHBOARD_BIND` is the only
    thing keeping it there.** Setting `DASHBOARD_BIND=0.0.0.0` publishes an
    unauthenticated admin surface to every network the host is on. Do that only
    for a network you control, and prefer reaching it over Tailscale instead.

    The container still binds `0.0.0.0:9120` internally. That is correct and not
    a second control — a bridged container must listen on its own external
    interface to be reachable at all. Changing it accomplishes nothing; change
    the port mapping.

Treat the whole stack as **trusted-host software**. The gateway mounts the Docker
socket, so reaching it is equivalent to root on the machine. If the host is not
on a network you fully control, put the stack behind a firewall that allows only
the ports you actually use, and do not rely on the defaults to be restrictive.

## Changing configuration

Most variables are read at container start, so:

```bash
cd docker
docker compose up -d          # recreates only what changed
```

Changes to `hermes/` templates need a re-seed, and `seed.sh` will not overwrite
an existing file — move it aside first:

```bash
mv "$HERMES_DATA_DIR/config.yaml" "$HERMES_DATA_DIR/config.yaml.bak"
./hermes/seed.sh
```

Dashboard backend and frontend sources are bind-mounted with `--reload`, so edits
there take effect without an image rebuild. Documentation is baked into its image
at build time, so it needs `docker compose up -d --build docs`.
