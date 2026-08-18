---
description: The FastAPI surface behind the operator console.
---

# Dashboard API

The operator console's backend (`docker/light-dashboard/backend/main.py`) serves
both the single-page frontend and the JSON API it runs on. Everything below is
under `http://127.0.0.1:9120`.

!!! danger

    These endpoints have **no authentication**, and the service binds `0.0.0.0` —
    see [Network exposure](../deploy/configuration.md#network-exposure). CORS is
    also fully open. Do not expose port 9120 beyond the host.

## Sessions

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/sessions` | Active chat sessions |
| `GET` | `/api/sessions/archived` | Archived sessions |
| `GET` | `/api/sessions/{id}/messages` | Full transcript |
| `POST` | `/api/sessions/{id}/read` | Mark read |
| `POST` | `/api/sessions/{id}/archive` | Archive |

## Chat

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat` | Send a message, await the reply |
| `POST` | `/api/chat/stream` | Same, streamed over SSE |

## Kanban

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/kanban` | Board state |
| `PATCH` | `/api/kanban/{task_id}` | Update a task |

The agent files its own work here — the self-improvement loop opens tasks from
degraded workflow traces.

## Cron

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/cron/jobs` | Scheduled jobs |
| `POST` | `/api/cron/jobs/{job_id}/run` | Trigger immediately |
| `GET` | `/api/automations/{job_id}` | One automation: config, run history, traces |

!!! note

    A cron job records only the script it runs, not the ADK app that script
    launches. That is why the console bind-mounts `workflows/scripts` read-only —
    the link is recoverable only by parsing the script.

`/api/automations/{job_id}` is the whole of one automation in a single response:
the same job object `/api/cron/jobs` serves, its executions newest-first from
the cron store — each with its own `error` — and totals over the window. It
404s for an id no profile schedules.

Where the job launches an ADK app, each execution carries an `adk_run` holding
the matching trace. That match is **by time**, because nothing links the two by
id: the cron store knows a process exited and the trace knows a workflow ran,
and neither writes the other's identifier. Treat `adk_run` as evidence about a
window rather than a join. `adk_run: null` means the job records traces and none
covers this execution — usually a run that died before `invoke_workflow` opened
one, which is exactly the run worth reading. The key is **absent** entirely for
a job that launches no app; absent and null are different claims.

Executions are read through `metrics_store`, so they span every profile's cron
database — a job that moved profiles keeps its history.

## Approvals

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/approvals/queue` | Pending outbound actions |
| `GET` | `/api/approvals/health` | Queue reachability |
| `POST` | `/api/approvals/decision` | Approve or reject |

The queue is a directory on disk (`APPROVALS_DIR`), which is what lets an
approval survive a container restart.

## Agents, skills, and context

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/agents` | Hermes profiles |
| `GET` | `/api/agents/{name}/skills` | Skills for one profile |
| `GET` | `/api/agents/{name}/context` | Context files for one profile |
| `GET` | `/api/agents/{name}/content` | Raw content for one profile |
| `GET` | `/api/skills` | Default agent's skills |
| `GET` | `/api/skills/content?rel_path=` | One `SKILL.md` |
| `GET` | `/api/context/files` | Markdown context files |
| `GET` | `/api/context/content?rel_path=` | One file, verbatim |

Content reads are confined to their subtree by `realpath` prefix checks and
restricted to `.md`, so `rel_path` cannot traverse out.

Each Hermes profile is a self-contained agent — its own `config.yaml`,
`SOUL.md`, `skills/`, `memories/` and `cron/`. The default agent is
`HERMES_HOME` itself; the rest live under `HERMES_HOME/profiles/<name>`.

## Stack health

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health/services` | Every service in this stack, red/amber/green |

Backs the health indicator in the console's header. Each service in
`docker-compose.yml` — plus the host's Ollama — is probed over loopback and
reported as `ok`, `degraded` or `down`; the payload's `overall` is the worst of
them, which is what the header light shows. Add `?fresh=1` to bypass the
few-second response cache.

Targets are read from the same environment compose reads (`HERMES_API_BASE`,
`WORKFLOWS_URL`, `BROWSER_URL`, `DOCS_URL`), so an address changed in `.env`
does not leave the probe watching something nothing is on. These used to be
`*_PORT` variables, from when the console probed published ports on the host;
on the bridge it reaches services by name, and a published port no longer
determines whether the probe finds anything. The roster and the
rules that turn a response into a colour live in `backend/health.py`.

!!! note

    The probe asks over the network, not Docker — this container deliberately
    has no docker socket. That is the more useful question anyway: a wedged
    uvicorn still reads `Up` in `docker ps` and answers nothing.

    `degraded` is not a milder `down`. It means the service answered, but
    slowly (over `HEALTH_SLOW_MS`, default 1500 ms) or with something other
    than health — an ADK server serving zero apps, a metrics store that found no
    Hermes profiles, or the renderer rejecting the probe's token. Only `down`
    flashes the header light.

    The **Metrics Store** row is probed over loopback like the rest even though
    it runs inside the dashboard process. Calling it in-process would report on
    a connection the probe opened rather than the one the Metrics tab uses, and
    the failures worth catching — an unmounted data directory, a DuckDB file
    that will not open, a missing sqlite extension — are invisible until
    something asks the real endpoint.

## ADK

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/adk/health` | Workflows service reachability |
| `GET` | `/api/adk/teams` | Registered agent teams |
| `GET` | `/api/adk/fleet` | Fleet status |
| `GET` | `/api/adk/runs` | Recent runs |
| `GET` | `/api/adk/scorecard` | Eval scorecard |

!!! note

    Fleet listing falls back to parsing source. ADK's own app-info cannot
    describe an app whose root is a `SequentialAgent` or `LoopAgent`, and omits
    such agents from the routing root's listing — so a pipeline is invisible
    without reading the source. Hence the read-only `workflows/app` mount.

## Metrics

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/metrics/health` | Which profiles and sources the store can see |
| `GET` | `/api/metrics/cost` | Spend and usage, per cost class |
| `GET` | `/api/metrics/models` | Usage broken down by model and provider |
| `GET` | `/api/metrics/activity` | Runs, chats and automations by kind and source |
| `GET` | `/api/metrics/timeseries` | Daily activity and tokens |
| `GET` | `/api/metrics/outputs` | What runs produced and touched, by kind |
| `GET` | `/api/metrics/agents` | Per-agent utilization and scores |
| `GET` | `/api/metrics/evals` | Eval pass rates and the cases now failing |
| `GET` | `/api/metrics/automations` | Scheduled-job executions, counted per job |

System-wide, unlike `/api/adk/*` above: these read every producer at once — ADK
runs, Hermes chat and Hermes automations across every profile — through one
grain. Backed by `backend/metrics_store.py`, which queries the Hermes state
databases (attached `READ_ONLY`) and the ADK trace logs in place with DuckDB.
The only copy it makes is the per-profile Hermes tables, read one profile per
statement to work around a DuckDB cross-catalog resolution bug, and rebuilt on
every connect — so deleting `metrics.duckdb` still rebuilds the whole store. All routes accept `?days=`.

!!! warning "Costs are reported per class and never totalled"

    Usage falls into three classes that cannot be added together, so
    `/api/metrics/cost` returns one line per class and no total:

    - **metered** — real money at a published rate. This is spend.
    - **included** — covered by a subscription already paid for. The marginal
      cost is zero but the usage is real, and it is most of the fleet's traffic.
    - **unpriced** — a local model with no rate. Tokens only.

    A number combining them would be neither spend nor capacity. Note also that
    `0.0` (priced, and the price is zero) and `null` (never measured) are
    different values: only `cost_source` distinguishes them, and a store that
    collapsed the two would let unmeasured usage look free.

!!! note "Absent is not zero, and neither is unknown"

    A field a producer had not yet started writing reads as `null`, never `0` —
    a trace written before per-agent model capture cannot name its model, and a
    run whose provider reported no usage did not spend nothing. The same rule
    governs outcomes: `succeeded`/`failed` come back `null` wherever
    `outcome_known` is `0`, because Hermes records how a session *stopped*
    (`cli_close`, `session_reset`), not whether it worked. Only ADK runs carry a
    real outcome vocabulary.

    On `/api/metrics/agents`, `checkpoint_pass_rate` is **measured** from what
    the stages recorded and `self_score` is a model's **claim** about its own
    turn. They are returned side by side and never averaged — see
    `workflows/app/self_assessment.py` for why blending them destroys the only
    one worth trusting.

!!! note "Executions and sessions are different populations"

    `/api/metrics/automations` is a separate route from `/api/metrics/activity`
    on purpose: most scheduled runs never open a model session, so the two
    counts are not addable. Adding them would misstate both how often
    automations run and what they cost.

## Memory

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/wiki/health` | Document and fact counts; whether an index exists |
| `GET` | `/api/wiki/documents?q=&limit=` | Documents, newest first |
| `GET` | `/api/wiki/document/{slug}` | One document: markdown, sections, both link directions |
| `POST` | `/api/wiki/search` | Full-text search over facts |
| `GET` | `/api/wiki/backlinks/{slug}` | What links here — the one-hop neighbourhood |

!!! note "Read-only, and no mutations at all"

    These open the index read-only and never build it. The dashboard and the
    workflows service run as different users, and a dashboard-created index
    would leave the writer unable to refresh its own store — so a missing index
    is reported rather than repaired.

    There are no delete routes. The graph API gated deletes behind
    `GRAPH_ALLOW_DELETE` because this port is unauthenticated on `0.0.0.0` and a
    delete cost minutes of GPU re-ingestion to undo. Here the equivalent is `rm`
    on the source of truth, so it is not exposed.

    Unlike the search this replaced, a query matching nothing returns nothing.
    See [Wiki memory](../architecture/wiki-memory.md).

## Integrations

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/integrations` | Configured integrations and status |
| `GET` | `/api/integrations/consumer/{consumer}` | One consumer's view |

!!! note

    An integration reporting as configured means its environment is populated,
    not that its credentials work. Google Workspace integrations in particular
    will report ready while token exchange fails, if the service account has not
    been granted domain-wide delegation. Test the actual call path.

## Frontend routes

Non-`/api/` paths return the SPA shell so deep links survive a hard refresh:
`/metrics`, `/chat`, `/kanban`, `/automations`, `/automations/{job_id}`,
`/agents`, `/integrations`, `/approvals`, `/graph`, `/settings/{section}`,
`/health`, and several `/agents/...` variants. Every response carries
`Cache-Control: no-store`.

`/automations/{job_id}` is one automation's page — configuration, what it is
attached to, and its run history in one place. It is where the Metrics ledger's
execution rows, the Automations list, and a team's Launch section all lead.
`/agents/hermes/{profile}/{job_id}` predates it and now redirects there; the
profile's own address, `/agents/hermes/{profile}`, still opens the profile.

`/health` is the health modal's own address — the page, not the JSON. The data
behind it is `/api/health/services`; the two cannot collide, since every API
route on this service is under `/api/`.

Adding a tab means registering its path in **both** the frontend's `TAB_PATHS`
and the backend's — a tab missing from the backend list works when navigated to
in-app and 404s on refresh.
