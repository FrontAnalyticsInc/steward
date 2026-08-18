---
description: The services, what each one is for, and why the networking looks the way it does.
---

# Overview

The stack is nine services defined in `docker/docker-compose.yml`, and a default
`docker compose up -d` starts **seven**. The other two sit behind compose
profiles: the page renderer (`--profile browser`, opt-in because its image is
3.7 GB and nothing depends on it) and the authenticated renderer
(`--profile authenticated`).

It was fifteen. The telemetry stack — an OpenTelemetry collector, a trace store
and that store's four backing services — has been removed, along with the
knowledge graph before it. Six containers and seven required secrets answered questions that the
JSONL run records already answer: the console reads those directly with DuckDB,
and the store it builds is a derived cache you can delete.

The OTLP instrumentation stays in place and unpointed. Set
`OTEL_EXPORTER_OTLP_ENDPOINT` on `workflows` and spans flow to a collector of
your own. See
[Workflows](workflows.md#telemetry).

There is no longer a host process behind any of this. Ollama is gone with the
move to hosted models, and nothing proxies model calls — see below.

## The services

??? note "models — no service, but there is one place a model is chosen"

    Nothing in this stack proxies model calls. Hermes dispatches to Anthropic
    over its own native adapter; the ADK workflows use the LiteLLM **SDK**
    in-process. Both read `ANTHROPIC_API_KEY` from the environment.

    A LiteLLM **proxy** was the original design and was dropped on measurement.
    Its documented floor is 4 CPU / 8 GB — structural rather than conservative,
    because Prisma's query engine holds memory as a high-water mark it never
    returns to the OS — and it requires a Postgres beside it. Measured here it
    was 1.06 GiB resident, 40% of the whole stack's idle footprint, to supply
    two things: model aliasing and a price map. Both are now a few hundred
    lines with no container.

    **Aliasing.** `${HERMES_DATA_DIR}/config/model-aliases.yaml`, seeded from
    `hermes/config/`. Workflows ask for a role — `drafting`, `extraction`,
    `fast` — never a model string, so swapping a model is one line in one file.
    `app/model_aliases.py` caches on the file's mtime, so an edit from the
    operator console applies without a restart. An unknown alias raises rather
    than falling back; a typo answered by the top tier is a bug you find on an
    invoice.

    Hermes's own models are separate, in its `config.yaml`, and the console
    already edits them through `PUT /api/config`. One screen, two files — the
    proxy would have made it one file and a rebuild-free restart, which is not
    enough difference to justify the machine.

    **Cost.** `app/cost_ledger.py` prices each call from LiteLLM's maintained
    map — the same map the proxy used — and appends a row to
    `${ADK_STATE_DIR}/usage/`, in the column shape the dashboard's
    `fact_llm_usage` view already defines. A call it cannot price is written
    `unpriced` with a NULL cost, never a zero that would read as free.

    **The cap.** `WORKFLOWS_DAILY_COST_CAP_USD` replaces the proxy's per-key
    budgets. The invoker asks `GET /cost` before every dispatch and refuses to
    start once the day's spend reaches the cap. It refuses *before* the run
    rather than aborting mid-run — a workflow killed halfway leaves a draft
    created and a label applied — and a `/cost` it cannot reach does **not**
    block the run, because the cap guards spend and is not an authorization
    gate. Scope is workflows only: Hermes chat and cron never appear in the
    ledger, so the real ceiling for the host is the spend limit on the
    Anthropic account.

    Memory needs no service of its own either: it is markdown files plus an
    FTS5 index. See [Wiki memory](wiki-memory.md).

??? warning "hermes-gateway — tool-use and orchestration"

    **Image:** `nousresearch/hermes-agent:latest`

    The agent runtime: tool execution, sessions, cron, memory. On the internal
    bridge like everything else; its API is published to `127.0.0.1:8642`.

    The gateway creates its tool sandbox through the host's Docker socket.
    **Access to this service is equivalent to root on the host.**

??? note "hermes-dashboard — vendor UI"

    **Image:** `nousresearch/hermes-agent:latest`

    The upstream web UI for configuring and chatting with Hermes, on
    `http://127.0.0.1:9119`. Protected by basic auth
    (`HERMES_DASHBOARD_BASIC_AUTH_*`).

??? note "light-dashboard — the operator console"

    **Build context:** `docker/light-dashboard`

    The custom console, and where day-to-day operation happens: chat, kanban,
    cron, approvals, integrations, ADK fleet status, and a Cytoscape graph
    explorer. FastAPI backend on port `9120`, single-file React frontend.

    It mounts the Hermes data directory, the approvals queue, and the workflows
    source read-only. The source mounts exist because a cron job records only a
    script name — recovering which ADK app it launches requires reading that
    script.

??? note "workflows — ADK agent runtime"

    **Build context:** `workflows/`

    Google ADK agents served over HTTP on port `8020` for the Hermes scheduler,
    published to `127.0.0.1`. Reaches the renderers by service name.

    See [Workflows](workflows.md).

??? note "browser — headless page rendering"

    **Built from:** `docker/browser` — Playwright on
    `mcr.microsoft.com/playwright/python`, version-pinned

    A Chromium instance on `127.0.0.1:3010`, for ingest workflows whose target
    builds its pages in the browser. Port 3010 rather than 3000, which is
    already bound on this host.

    **This was browserless until we needed Chrome extensions.** Nothing
    off-the-shelf loads them: browserless gates extensions behind its paid tier,
    `chrome-headless-shell` is the old headless binary and cannot load them at
    all, and Playwright's own `run-server` cannot either — extensions require
    `launch_persistent_context()`, a local launch, and `connect()` has no
    persistent contexts. The process that owns the browser must own the launch,
    so it is ~150 lines of FastAPI here rather than a pulled image. Playwright
    is Apache 2.0; browserless was SSPL.

    It speaks browserless's `/content` contract deliberately, which is why
    nothing in `workflows/` changed in the swap and the existing ingest tests
    passed unmodified. Treat that contract as the service's API.

    Two version couplings are load-bearing. The base image tag pins Chromium and
    `playwright==` in the Dockerfile pins the client; **they must match** or the
    client looks for a `chromium-<build>` that is not there. And the image's own
    `pwuser` is uid 1001 while the mounted profile is owned by the host user, so
    the container runs as uid 1000 — the failure otherwise names a Chromium path
    rather than a permission.

    Unpacked extensions mount read-only from
    `${HERMES_DATA_DIR}/browser/extensions`; the profile persists read-write and
    holds live session cookies. See
    [`docker/browser/README.md`](../../docker/browser/README.md).

    Some sites cannot be read without it at all. A Create React App serves the
    same empty shell at every route — the body is `<div id="root"></div>` and
    even the meta tags match, because they are set client-side. A static fetcher
    sees one page, no links, and no content, and the failure is quiet: the crawl
    "succeeds" and returns nothing. Rendering kestrel.io's homepage takes it from
    2,636 bytes and zero links to 67,100 bytes and 18 links.

    Its own service rather than Playwright inside the workflows image. Chromium
    and its libraries are roughly another gigabyte on an image already at
    2.26 GB; browsers leak, so a long-lived one wants its own restart policy
    instead of taking the ADK server down with it; and the `dev` profile authors
    under `workflows/app`, so keeping the renderer out of that image means a
    workflow author cannot break the build by touching it. Callers need no new
    dependency — `httpx` is already installed.

    **`BROWSER_TOKEN` is a credential, not a formality.** This service fetches
    any URL it is given and executes that page's JavaScript, from a
    host-networked container that can reach every other `127.0.0.1` service in
    the stack. Unauthenticated, it is an open proxy into the network. Generate
    one with `openssl rand -hex 24`.

    A workflow that cannot reach the renderer must fail its checkpoint. There is
    deliberately no fallback to an unrendered fetch: that fallback is how a
    one-page crawl came to report perfect health.

??? warning "browser-linkedin — the authenticated renderer"

    **Build context:** `docker/browser` — the same image as `browser`, with its
    own profile, port (`3011`), and token. Behind `--profile authenticated`, so
    a default `up -d` does not start it.

    A second container rather than a flag on the first, and the separation *is*
    the security property. `browser` renders arbitrary third-party URLs, and
    Chromium shares one cookie jar across a browser context. LinkedIn's `li_at`
    is `SameSite=None`, so a logged-in session living in that profile would be
    reachable — and cookie-bearing — by every hostile or merely compromised page
    the crawler is ever pointed at.

    Two rules follow, and neither is optional: **never point this instance at an
    untrusted URL**, and **never set `ALLOW_SESSION_INJECTION` on `browser`.**

    `BROWSER_LINKEDIN_TOKEN` is a separate credential from `BROWSER_TOKEN` on
    purpose. This instance browses *as you*; leaking its token is a worse event
    than leaking the crawler's.

    No extensions are mounted. An extension in an authenticated profile has
    ambient access to that session, and the one we run matches only `/in/*` and
    `/messaging/*` — nothing it would contribute on a jobs page.

    The session is seeded by `POST /session` from
    `workflows/scripts/seed_linkedin_session.py`, which is why its port is
    published to `127.0.0.1` at all: you copy the cookie from a browser you are
    already signed in to, and no password ever reaches this stack.
    `linkedin_saved_jobs` reaches it as `browser-linkedin:3011` on the bridge and
    refuses to fall back to `BROWSER_URL` — a missing renderer fails the run
    rather than quietly borrowing the crawler's profile.

??? note "docs — this documentation"

    **Image:** `nginx:alpine`, built from `docs/` at image build time

    Serves this site on port `9121`. Static output from MkDocs Material, roughly
    3 MB, with a client-side search index — so it works with no internet
    connection, which is the point. The same build output is what gets published
    publicly.

    Bound to all interfaces by default so it can be read from another machine on
    the network. That is a deliberate exception: this is the one service in the
    stack whose content is public anyway. `DOCS_BIND=127.0.0.1` restricts it.

## Why one bridge network

Every service is on a single internal bridge. **No service uses
`network_mode: host`.**

It used to. Seven services were host-networked, and the reason was Ollama: it
bound `127.0.0.1` on the host, and a bridged container cannot reach that. Host
networking was the cheapest fix that avoided making Ollama listen on a routable
address. With models now hosted, that constraint is gone and the exposure it
forced went with it.

The change is worth stating plainly because it inverts how you reason about
reach. Host-networked, a service's own bind address was the only control, and
`127.0.0.1` inside a container meant the host — so the dashboard called the
workflows service at `http://127.0.0.1:8020`. Now services resolve each other by
**service name** (`workflows:8020`, `browser:3010`), and
what the host or the LAN can reach is exactly the `ports:` list and nothing
else. A container binding `0.0.0.0` is no longer a finding — it has to, to be
reachable on the bridge at all.

Every published port binds `127.0.0.1` except `docs`, which is LAN-readable on
purpose. Two are widened by variable: `DASHBOARD_BIND` for the unauthenticated
operator console, and `DOCS_BIND`.

Separately, every service carries a `mem_limit`. The tool sandboxes are
provisioned at 5 GiB each, which is why the deployed instance is 8 GB rather
than the 4 GB this once assumed: at 4 GB that limit sits above physical memory
and bounds nothing. An unbounded service is the difference between one container
dying and the box going down.

## Pinning

The browser image is version-pinned. A renderer especially wants pinning:
`:latest` would silently become a different
Chromium on the next pull, and a scraper whose browser changes underneath it
turns an ordinary regression into a mystery. The ADK base image,
`google-adk`, `google-agents-cli` and `uv` are pinned in the workflows image, so
a rebuild resolves the same versions.

The two Hermes images are `nousresearch/hermes-agent:latest`, which is **not**
pinned — a rebuild can pick up an upstream change you did not ask for. If you
need reproducibility across hosts, pin these to a digest.
