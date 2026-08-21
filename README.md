# Steward

An AI agent that builds automations for your business, runs them on a schedule,
and repairs them when they break — on a server you own.

This repository holds Steward's full source. The installer fetches it onto
your machine and builds the images there — nothing is pulled from a private
registry, and no access token is needed from anyone.

---

## Install

On an Ubuntu 22.04 or 24.04 machine that stays awake, or on a Mac:

```bash
curl -fsSL https://raw.githubusercontent.com/FrontAnalyticsInc/steward/main/install.sh | bash
```

Not `sudo bash`. The installer refuses to run as root — Steward's data directory
is owned by a real user and every service runs as that user, so a root install
produces a directory the services cannot write.

On Linux it asks for `sudo` for the three things that need it: creating
`/srv/steward`, adding you to the `docker` group, and installing Docker if it is
missing. On macOS it asks for nothing: the install lands under your home
directory, there is no `docker` group, and Docker Desktop is something you
install yourself (see [macOS](#macos) below).

You will be asked for one thing:

- **An Anthropic API key.** It never leaves your machine — it is written to a
  file mode 0600 on your box and read from there.

  You can leave it blank and install anyway. The stack builds and starts, and
  the console comes up so you can look around and finish configuring; nothing
  that calls a model will work until you add the key to
  `/srv/steward/stack/.env` (`~/steward/stack/.env` on macOS) and re-run `up -d`.
  The healthchecks do not call a model, so a keyless Steward looks healthy —
  the installer says so plainly at the end rather than letting you discover it
  when the first job fails.

It can be supplied non-interactively instead:

```bash
curl -fsSL https://raw.githubusercontent.com/.../install.sh \
  | ANTHROPIC_API_KEY=sk-ant-... bash
```

The installer downloads this repository at a release tag, then runs
`docker compose build`. **Budget 20–40 minutes on a 2 vCPU box**, almost all of
it the build — the browser image carries Playwright and Chromium. It is slower
than pulling a published image and it is the reason nothing here needs a
credential beyond your own model key.

### What it needs

| | |
|---|---|
| OS | Ubuntu 22.04 or 24.04, or macOS 13 (Ventura) and newer |
| CPU | x86_64 or arm64 — including Apple Silicon, natively |
| RAM | 8 GB (it refuses below 6 — one tool sandbox is allowed 5 GiB) |
| Disk | 40 GB free where Docker writes — about 13 GB of images plus build cache |
| Stays awake | Steward runs on a schedule, and a sleeping machine misses it |

On GCP that is an `e2-standard-2` with a 60 GB boot disk. The Terraform to
create one is in [`infra/`](infra/).

Nothing here is x86-only. The installer builds every image on your machine from
this source tree, and each base image those build `FROM` publishes an arm64
manifest, so an Apple Silicon Mac gets a native arm64 stack rather than an
emulated one.

### macOS

Everything above applies, plus three things that are specific to a Mac.

**Docker Desktop is not installed for you.** It is a signed application with its
own updater and a licence whose terms depend on the size of your company, so the
installer checks for it and stops with instructions rather than reaching for it
itself:

```bash
brew install --cask docker && open -a Docker
```

Open it once and let it finish its first run before installing Steward. Three of
its settings matter, and the installer fails with the fix rather than letting
any of them become a mystery later:

- **Settings → Resources → Memory** must be at least 6 GB, 8 GB to be
  comfortable. This is Docker Desktop's own limit and it defaults well below
  your Mac's RAM. Containers never see more than this, whatever the machine has.
- **Settings → Advanced → "Allow the default Docker socket to be used"** must be
  on. Steward mounts `/var/run/docker.sock` so the agent can create its tool
  sandboxes; without it the stack starts and only fails when the first tool runs.
- **Settings → General → "Use Rosetta for x86_64/amd64 emulation"** can be off,
  and should be if it gives you any trouble. Docker Desktop installs Rosetta
  while starting its VM and treats a failure there as fatal to the whole engine:
  the daemon never comes up, the whale never stops animating, and the only sign
  of why is a "Rosetta installation failed" dialog. Steward's images are all
  native arm64 and never touch it, so disabling it costs nothing here.

**The install goes under your home directory**, at `~/steward` rather than
`/srv/steward`. Two reasons, and neither is preference: macOS has a sealed
read-only root volume, so `/srv` cannot be created at all without
`/etc/synthetic.conf` and a reboot; and Docker Desktop only shares `/Users`,
`/Volumes`, `/private` and `/tmp` into its VM. A bind mount of an unshared path
does not fail — it mounts an *empty* directory — which would give you a stack
that starts, passes every healthcheck, and behaves as though it had never been
configured. The installer proves the data directory is really visible to Docker
before it builds anything. If you move the install with `--home`, keep it
somewhere Docker Desktop shares.

**A laptop is not a server.** This is the one thing a Mac does not solve.
Steward's value is work that happens while you are not watching, and a sleeping
Mac runs none of it — the schedules are missed, not deferred, and nothing
catches up on wake. If you are relying on automations rather than just chatting
with it, either keep the machine awake and plugged in:

```bash
caffeinate -dimsu &
```

or set "Prevent automatic sleeping when the display is off" in System Settings →
Displays → Advanced. Turn on "Start Docker Desktop when you sign in" in Docker's
General settings too, or the stack is simply down after every reboot. A Mac mini
that stays on is a fine Steward host; a MacBook you close at night is a good
place to try it and a bad place to depend on it.

### Where it puts things

```
/srv/steward/
├── src/                     this repository at the installed tag
├── stack/
│   ├── steward-stack.yml    rendered from src/ at install time
│   ├── config.env           this install's non-secret configuration (0644)
│   └── .env                 your keys and this install's secrets (0600)
├── hermes-update            the upgrade runner — see Upgrading below
├── snapshots/               written by an upgrade, before it touches anything
└── data/                    everything Steward knows — back this up
```

`src/` is not a scratch copy. The rendered stack file records build contexts
that point into it, so moving or deleting it breaks rebuilds and upgrades.

**On macOS the root is `~/steward`** and the tree below it is identical. Every
`/srv/steward/...` path in the rest of this README reads as `~/steward/...`
there. See [macOS](#macos) for why it is not `/srv`.

Override with `STEWARD_HOME=/some/path`, or `--home /some/path`. On a Mac, pick
somewhere Docker Desktop shares — under your home directory is safest.

---

## First run

The installer prints the console URL, and separately a username and password for
the Hermes UI on `:9119`.

> [!WARNING]
> **The console on `:9120` has no authentication.** The username and password
> the installer prints are for the Hermes UI on `:9119`, not for the console.
> Anything that can reach port 9120 can approve a review — which sends mail —
> and can read this deployment's gateway key.
>
> The only thing protecting it is the loopback bind. Leave `DASHBOARD_BIND` at
> `127.0.0.1`; if you cannot reach the console, tunnel to it, do not rebind it.
> Authentication is planned for a later release.

Reach it from another machine with a tunnel rather than by rebinding it:

```bash
ssh -N -L 9120:127.0.0.1:9120 you@your-box
```

Or over a tailnet. The first form needs tailnet HTTPS certificates, which are
off by default — enable them once at
[login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns), or use
the second:

```bash
tailscale serve --bg 9120                             # proxies to :9120, serves on 443
tailscale serve --bg --http=80 http://127.0.0.1:9120  # if HTTPS is not enabled
```

Then **restrict the tailnet**. The default ACL is allow-all, so every device on
your tailnet can reach the box — and with no authentication on the console, that
is the whole of your access control:

```json
{
  "grants": [
    { "src": ["group:operators"], "dst": ["tag:steward:443"], "ip": ["tcp"] }
  ],
  "groups":    { "group:operators": ["you@example.com"] },
  "tagOwners": { "tag:steward": ["autogroup:admin"] }
}
```

Do not grant `:9120` — that reopens the direct path the loopback bind closed.
Never enable Tailscale Funnel on this node; it would put the console on the
public internet.

---

## Smoke test

Run this after installing. If all five pass, Steward works; if one fails, its
number is the useful thing to report.

```bash
cd /srv/steward/stack
export $(grep -E '^(API_SERVER_KEY)=' .env | xargs)
C="docker compose -f steward-stack.yml --env-file config.env --env-file .env"
```

**1. Everything is up.** `hermes-gateway`, `workflows`, `light-dashboard` and
`review-executor` healthy; `hermes-init` exited 0.

```bash
$C ps
```

**2. The gateway answers.** This is the one that proves your Anthropic key, the
seeded config and the gateway are all working together.

```bash
curl -s -H "Authorization: Bearer $API_SERVER_KEY" http://127.0.0.1:8642/v1/models

curl -s -H "Authorization: Bearer $API_SERVER_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"default","messages":[{"role":"user","content":"reply with the word ok"}]}' \
  http://127.0.0.1:8642/v1/chat/completions
```

**3. The console loads.** Open it in a browser, and check the health endpoint
reports every service:

```bash
curl -s http://127.0.0.1:9120/api/health/services
```

**4. A workflow completes end to end.** `summarize_note` is the right one to
try: a single agent with an output schema, no credentials, no page rendering.

```bash
curl -s http://127.0.0.1:8020/list-apps        # should contain summarize_note
```

Run it through the invoker, not by calling the ADK server yourself:

```bash
$C exec hermes-gateway python3 /opt/data/scripts/invoke_workflow.py \
  --app app.agents.summarize_note \
  --payload '{"note":"Met Dana Whitfield at the Denver logistics expo. They run ops for a mid-size 3PL, currently reconciling carrier invoices by hand, and asked whether we do anything with batch latency. Follow up in two weeks."}'
```

You should get back a `note_summary` with a sentence, topics and `needs_review`.

**Use the invoker.** Speaking HTTP to `:8020` directly does run the workflow,
but only `invoke_workflow` writes the run record under `traces/`, and that
record is the only durable evidence a run happened. Bypass it and the run
genuinely succeeds while the console reports the workflow as *never run* — a
confusing way to fail a smoke test that actually passed.

Then confirm it appears in the console's Metrics view. That is the half of this
step worth caring about: it exercises the state mount and the JSONL-to-DuckDB
path, not just the model call.

On a fresh install the Automations tab is **empty**, and that is correct —
scheduled jobs are yours to create, not something the install ships.

**5. It survives a restart.** Catches the two failures that only show up the
second time: a directory that ended up owned by root, and a secret that was
regenerated per process rather than stored.

```bash
$C restart && sleep 30 && curl -s http://127.0.0.1:9120/api/health/services
```

---

## What is not in this release

- **Gmail, Calendar, Attio.** These need Google Workspace domain-wide delegation
  granted by an admin in your own domain — not a credential that can be handed
  over. Not part of a bare install.
- **Page rendering.** The browser service is behind a profile because its image
  is 3.7 GB on its own. This is why a fresh console's Renderer health tile
  reads "down" — that is not a fault, it is the profile being off. Turn it on
  in `stack/config.env`, not with a one-off `--profile browser` flag: set
  `COMPOSE_PROFILES=browser` and clear the `BROWSER_URL=` line at the same
  time — a workflow that needs rendering is required to fail loudly when it is
  unavailable rather than return nothing and call it an answer — then apply
  both by re-rendering the stack:
  `$STEWARD_HOME/hermes-update --to <the version already installed>`. A CLI
  flag on a single command does not persist; the next render (upgrade or
  otherwise) starts from `config.env` and would drop it again.
- **A library of workflows.** Two ship, and both exist to be read rather than
  relied on: `summarize_note`, a single agent with an output schema and no
  credentials, and `intentional_failure_demo`, which fails on purpose so the
  error path and the review queue have something to show. The workflows that
  run this company are not in here — they are specific to one business and
  several of them name its clients. `workflows/app/agents/` is the pattern to
  copy; `workflows/README.md` explains the shape.

---

## Everyday operations

```bash
cd /srv/steward/stack
C="docker compose -f steward-stack.yml --env-file config.env --env-file .env"

$C logs -f              # follow everything
$C logs -f workflows    # or one service
$C restart
$C down                 # stop; your data in /srv/steward/data is untouched
```

## Upgrading

Use `hermes-update`, which the installer put beside the stack. Do **not** re-run
`install.sh` — it does not run migrations, so the images move forward and the
data disk does not.

Steward does not check for updates and will not tell you one exists. Find the
tag you want at
[github.com/FrontAnalyticsInc/steward/releases](https://github.com/FrontAnalyticsInc/steward/releases),
then:

```bash
/srv/steward/hermes-update --to v0.2.0 --dry-run   # what would happen, and nothing else
/srv/steward/hermes-update --to v0.2.0             # do it
```

`--to` is required. The script is installed from the release it upgrades *from*,
so it has no way to know which versions came later; rather than guess, it
refuses without one.

It snapshots the data disk, downloads the target release's source, re-renders
the stack file from it, stops the stack, **rebuilds the images**, runs any
pending migrations against the stopped stack, brings it back up and waits on the
healthchecks. Only then does it record the new version. If any step fails it
restores the snapshot, puts the previous source tree, stack file and version
back, and names the step that failed.

Budget as long as the install took, most of it the rebuild. The console is down throughout —
Settings → About shows the current version and the same two commands.

Upgrades are forward-only; there is no downgrade. Snapshots are kept in
`/srv/steward/snapshots` (the last 3, set `STEWARD_KEEP_SNAPSHOTS` to change
that).

To remove it completely:

```bash
$C down -v && sudo rm -rf /srv/steward
```

---

## The source

Everything Steward runs is in this repository; the installer above is just the
short path to a working box. Two audiences, kept apart on purpose:

- **[`docs/`](docs/)** — for people *using* the assistant: chat, automations,
  channels, knowledge. This is what the `docs` container serves on `:9121`.
- **[`docs-ops/`](docs-ops/)** — for whoever *runs* it: architecture, deploy,
  configuration, console API. Not part of the published site.

```text
steward/
├── docker/
│   ├── docker-compose.yml            all eight services, building from source
│   ├── docker-compose.deploy.yml     overlay: pull images instead of building
│   ├── docker-compose.standalone.yml overlay: no source tree on the box
│   ├── docker-compose.source.yml     overlay: build here — what install.sh uses
│   ├── hermes-gateway-patched/       the gateway image: upstream + one file
│   └── light-dashboard/              the operator console (FastAPI + React)
├── workflows/                        Google ADK agents + deterministic evals
├── hermes/                           versioned config, identity, skills, profiles
├── infra/                            Terraform, cloud-init, and the CI checks
├── install.sh                        what the curl command runs
└── hermes-update.sh                  published to a box as `hermes-update`
```

### Working on it directly

A checkout builds and runs without the installer. This is the development path —
it uses the base compose file, so services build from the tree you are editing:

```bash
cp docker/.env.example docker/.env   # ANTHROPIC_API_KEY, API_SERVER_KEY and
                                     # BROWSER_TOKEN have no working defaults
./hermes/seed.sh
cd docker && docker compose up -d --build
```

Tests, which are what CI runs:

```bash
cd workflows && uv sync --frozen && uv run pytest tests/unit -q
cd docker/light-dashboard && python -m pytest backend -q
python3 infra/check_cloud_init.py && python3 infra/check_migrations.py
```

Adding a workflow is adding a directory under `workflows/app/agents/` — the
registry discovers what is on disk rather than reading a list, so nothing else
needs editing. `HERMES_DISABLED_AGENTS` is how you turn one off.

---

## Licence

MIT — see [LICENSE](LICENSE).

`docker/hermes-gateway-patched/api_server.py` is a modified copy of a file from
Nous Research's Hermes agent gateway, MIT © 2025 Nous Research; that notice
travels with it and is reproduced in [NOTICE](NOTICE). The gateway image is
built `FROM nousresearch/hermes-agent:latest` with that file layered on top.

Built by [Front Analytics](https://frontanalytics.com/steward).
