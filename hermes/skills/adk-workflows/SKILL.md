---
name: adk-workflows
description: >
  Author, run, schedule and monitor ADK workflow agents in the agents-cli
  project at /opt/workflows. Use whenever asked to add a new ADK agent, propose
  or change a workflow, run a workflow, check whether one succeeded, or debug a
  failed run. Load this before creating any cron job that is meant to invoke a
  workflow: such a job must be no_agent=True with a script, and a prompt-driven
  job silently runs Hermes instead of the workflow.
metadata:
  hermes:
    category: automation
    tags: [adk, workflow, automation, authoring, agents-cli]
---

# ADK workflows

ADK agents are multi-agent workflows that run outside Hermes, in the `workflows`
project. You are the overlord: you author and change them, and you read their
results. You do not do their work yourself.

One directory matters:

- `/opt/workflows` — the agents-cli project. **This is where every agent lives.**

For ADK API specifics — agent classes, tools, structured output — load the
`google-agents-cli-adk-code` skill. For project/scaffold questions, load
`google-agents-cli-scaffold`. For eval design, `google-agents-cli-eval`. Those
encode the current ADK surface and are more reliable than recalling it.

**Do not follow `google-agents-cli-deploy` or `google-agents-cli-publish`.** This
project is local-only: no GCP, no Cloud Run, no Agent Engine, no Terraform, and
no Application Default Credentials.

## Is this even an ADK workflow?

Decide the substrate before you build anything. The pull is always toward the
nearest lever — a cron job pointed back at the agent, a `delegate_task` to
another Hermes profile, a task on the board. Each of those automates *Hermes*,
and the request usually wanted a pipeline.

| The work is… | Build |
|---|---|
| judgment over varied or untrusted content, repeatable, worth evaluating | **an ADK workflow agent** + `no_agent` cron |
| mechanical, scheduled, no reasoning | a `no_agent` cron running a script |
| needs a model over Hermes-side context every tick | a prompt cron — rare, justify it |
| building or repairing a workflow | a kanban task assigned to `dev` |
| cheap, high-volume, well-scoped | hand to `worker` |

If you cannot say why a workflow is the wrong shape for it, it is the right one.

## Adding a new agent

**Which profile you are matters here.** Authoring and editing under
`/opt/workflows` belongs to `dev`, which owns that tree and carries GSD. If you
are `default`, do the design conversation — that is the part needing a human —
then file a kanban task assigned to `dev` with the design in the body,
`--workspace dir:/opt/workflows`. A task body reading "add a workflow for X"
discards the only thing the conversation produced. The line is the directory,
not the size of the change.

If you are `dev`: an agent is just Python modules, so the file tools are enough,
no CLI needed. Anything larger than a single file goes through the GSD phase
loop rather than freehand. Read `/opt/workflows/GEMINI.md` first; it is the
authority and it overrides anything here.

Create `/opt/workflows/app/agents/<name>/` with four files, copying
`app/agents/enrich_contact/` as the reference:

- `schema.py` — Pydantic input and output models.
- `prompt.py` — a brace-free `INSTRUCTION` plus `build_user_message()`.
- `agent.py` — the `LlmAgent`, model from `app.config.build_model()`.
- `__init__.py` — exports the agent.

Then two things that are easy to miss and each break the agent silently:

1. **Alias `root_agent = <name>_agent`** at the bottom of `agent.py`. ADK
   resolves an app to `<module>.agent.root_agent`; without the alias the agent
   appears in `/list-apps` but every run 404s with "No root_agent found". It is
   also what `app/registry.py` reads to decide which object to register, so a
   module exporting several `*_agent` names and no `root_agent` fails to load.
2. **No braces in the instruction.** ADK treats `{...}` as a session-state
   variable and raises `KeyError` at request time. This bites on JSON examples.

Hard rules from `GEMINI.md`, restated because they are the ones that matter:

- Typed input/output, always. Pass the output model as `output_schema=`. Never
  parse prose. Note `output_schema` disables tool calling — an agent gets
  structure or tools, not both.
- Untrusted text (email bodies, scraped pages, notes) goes in the **user
  message** via `build_user_message()`, never in the instruction. State the
  do-not-obey rule before the data appears.
- Thin input must yield low confidence and `needs_review: true`. Never invent
  detail to fill a schema.

### After writing

The workflows service **hot reloads**: `app/` is mounted into it and uvicorn
watches that directory, so a new agent is picked up within a few seconds. You do
not need to restart anything, and you should not ask the user to.

Confirm it landed by checking that the app appears:

    GET http://127.0.0.1:8020/list-apps   →   "app.agents.<name>"

You cannot reach that URL from `execute_code` (no network in that sandbox). Ask
the user to check, or verify by reading the files back with the file tools.

If the app does not appear after ~30 seconds, the module almost certainly fails
to import — a syntax error, a bad import path, or a missing `root_agent` alias.
Re-read your files rather than rewriting them.

**Tell the user what you created and let them review it** — `/opt/workflows` is
version-controlled source. Do not commit.

Creating the agent does not make it run. Scheduling it is a separate job with
its own failure mode — see **Running a workflow** below, and do not improvise a
cron call from memory.

## Running a workflow

### The mistake to not make

**A cron job with a prompt does not run a workflow. It runs *you*.**

This is the single most common failure on this system, so read it before writing
any cron call. A job created like this:

    # WRONG — every part of this is wrong
    cronjob(action="create", name="run-my-workflow", schedule="0 9 * * *",
            prompt="Run the my_workflow ADK workflow and report the results")

does not touch ADK at all. `no_agent` defaults to **False**, which means the
scheduler wakes up the default Hermes agent, hands it that sentence, and bills a
model call for it. The workflow never runs. Nothing appears under `traces/`.
The dashboard reports the app as *never run* while the job's `last_status` reads
`ok` — because delivering the agent's reply *was* a success, on its own terms.
There is no error anywhere for you to notice.

There is exactly one correct shape, and it never contains a prompt that
describes the work:

    # RIGHT — no_agent=True, and a script that exists
    cronjob(action="create", name="run-my-workflow", schedule="0 9 * * *",
            no_agent=True, script="run_my_workflow.py")

The rule with no exceptions: **if the job's purpose is to invoke an ADK
workflow, it is `no_agent=True` with a `script`.** A prompt-driven job is for
work that genuinely needs a model to reason each tick. Invoking a workflow is
plumbing — the script speaks HTTP itself and its stdout comes back verbatim.

If you catch yourself writing a prompt that says "run the workflow", stop: you
are about to create a job that asks Hermes to do what the script exists to do.

### Order of operations

The wrapper must exist **before** the job. `no_agent=True` with a `script` that
is not there fails at fire time, not at create time, so a job created in the
wrong order looks fine until its first tick.

**1. Write the wrapper to `/opt/data/scripts/run_<workflow>.py`.**

Use that absolute path. Do *not* write `~/.hermes/scripts/` — you run inside the
gateway container, where your `HOME` **is** the data dir (`/opt/data`), so
`~/.hermes/scripts/` expands to `/opt/data/.hermes/scripts/`, a directory that
does not exist. The host's `~/.hermes` *is* `/opt/data`, with no `.hermes` inside
it. `/opt/data/scripts` is the only directory cron will run a script from.

Copy `/opt/data/scripts/run_gmail_inbox_triage.py` — it is the reference. The
wrapper picks the app name and the run-id policy, then calls
`invoke_workflow(app, {}, run_id)`. It needs
`sys.path.insert(0, "/opt/data/scripts")` before the import.

**2. Create the job**, with the bare filename as `script`.

**3. Fire it once immediately** rather than waiting for the schedule:

    cronjob(action="run", job_id="<id>")

Then confirm a new line landed in `/opt/data/adk/traces/<app>/<date>.jsonl`. A
job you created but never fired is a job you have not tested.

### Where `script` resolves — the trap that has already bitten

`script` is resolved relative to **the running profile's
`HERMES_HOME/scripts`**, and the scheduler refuses any path landing outside it.

That is `/opt/data/scripts` for `default`, and every path below assumes
`default`. Production wrappers belong there: only `default` has a running
gateway, so only its jobs fire on a *schedule*.

It is *not* `/opt/data/scripts` for `dev`: `HERMES_HOME` for `dev` is
`/opt/data/profiles/dev`, so a bare `script` resolves under
`/opt/data/profiles/dev/scripts/` — a different directory, which does not hold
the wrapper.

A manual `cronjob(action="run")` *does* fire in-process without a gateway, so
that is not the obstacle; the path is. And it cannot be bridged with a symlink:
the scheduler calls `.resolve()` and then rejects anything landing outside the
profile's own scripts dir, symlink escape included. Short of keeping a second
copy of every wrapper in `dev`'s scripts dir — two files to drift apart —
**`dev` cannot test-run a workflow it has written.**

So a `dev` task must not carry "manual run succeeds" as acceptance. `dev`
authors the wrapper and places it in `/opt/data/scripts`; the run is an
operator step from `default`.

| `script` value | resolves to | |
|---|---|---|
| `run_my_workflow.py` | `/opt/data/scripts/run_my_workflow.py` | correct |
| `scripts/run_my_workflow.py` | `/opt/data/scripts/scripts/run_my_workflow.py` | `Script not found` |
| `/opt/workflows/scripts/run_my_workflow.py` | outside the scripts dir | **blocked** |

So: **a bare filename, never a directory prefix.** The second row is not
hypothetical — the `one-shot-scrape-kestrel-pages` job on this system is sitting
in `last_status: error` for exactly that reason.

The third row matters because `/opt/workflows/scripts/` also contains
`run_*.py` files with the same names. They are decoys as far as cron is
concerned: the path guard rejects them no matter how you spell it. A wrapper is
only reachable by cron if it is in `/opt/data/scripts/`.

**Do not set `workdir` on these jobs.** It does nothing here — the script
subprocess runs with `cwd` set to the script's own directory, and `workdir`'s
real effect (injecting `AGENTS.md`, pointing the file tools somewhere) applies
to agent-driven jobs, of which this is not one. Two of the working jobs set
`workdir=/opt/workflows` and it has never had any effect.

### Working examples, copied from jobs that are actually running

Recurring, every ten minutes, 89 successful runs:

    cronjob(action="create", name="worker-gmail-inbox-triage", schedule="10m",
            no_agent=True, script="run_gmail_inbox_triage.py")

Daily at 13:00 UTC:

    cronjob(action="create", name="calendar daily briefing", schedule="0 13 * * *",
            no_agent=True, script="run_calendar_daily_briefing.py")

One-shot, a minute from now — the shape to use when testing a new workflow:

    cronjob(action="create", name="run-my-workflow-once", schedule="1m", repeat=1,
            no_agent=True, script="run_my_workflow.py")

`prompt` is ignored when `no_agent=True`. The running jobs still carry a
one-line one (`"Run gmail_inbox_triage via …. no_agent job; prompt ignored."`)
purely so a human reading `cronjob(action="list")` can tell what the job does.
Copy that habit; just never let the prompt be load-bearing.

Before creating anything, check `cronjob(action="list")` — if a job for this
workflow already exists, fire it with `action="run"` rather than adding a
duplicate that will double every side effect.

### Before you say a workflow is scheduled

Check all four. Any one of them failing means it is not running:

1. `no_agent` is `True` on the job.
2. `script` is a bare filename, and that file exists in `/opt/data/scripts/`.
3. You fired it once with `action="run"` and it did not error.
4. A line for it appeared in `/opt/data/adk/traces/<app>/<date>.jsonl`.

Point 4 is the only one that proves the workflow itself ran. The first three can
all pass while the wrapper fails on its own import.

### Never bypass the invoker

**A wrapper must not speak HTTP to the ADK server itself.** It will appear to
work — the workflow really does run — but only `invoke_workflow` writes the run
record under `traces/`, so the dashboard shows an app whose every run succeeded
as *never run*. The run record is the only evidence there is. The dashboard flags a wrapper
that bypasses the invoker, on the job's card in the Agents tab.

You also cannot reach the service from `execute_code`: that sandbox has its own
network namespace and every request fails with `Connection refused`. Do not try.
The `no_agent` cron path exists precisely because it runs in the gateway
container, which has host networking.

## The result contract

`invoke_workflow` decides whether a run worked by reading one thing: an
`emit_result` function response carrying

    {status, items, needs_review, errors, metrics}

`status` is one of `ok` / `partial` / `failed`; `items`, `needs_review` and
`errors` are lists; `metrics` is an object whose `input_count` and
`output_count`, if present, are integers. Anything else — including a final text
event summarising the run in prose — is treated as a contract violation, retried
`MAX_ATTEMPTS` times, and recorded as failed.

So **every pipeline needs a final deterministic stage that emits it**. Give it
no model: the numbers must be read from the state keys the measuring stages
wrote, never inferred. See `EmitResultAgent` in
`workflows/app/agents/gmail_inbox_triage/stages.py` for the shape to copy, and
note the split it keeps — `items` is what the run did on its own authority,
`needs_review` is what it deliberately left for a human.

A stage that fails should record *why* into an accumulating `run_errors` state
key, not only into its progress note: the note is prose for a human, and
`errors` is a field something else reads.

## Self-assessment

The result also carries `self_assessment` — the pipeline's read on its own
health, which is a different question from whether its output was any good. Use
`app/self_assessment.py`; do not hand-roll it.

**`score` is measured.** Every stage calls `checkpoint(ctx, name, ok, detail)` to
say whether it did the job it exists to do — which is *not* "did not raise". A
fetch returning nothing because the inbox is empty passes; one returning nothing
because there is no credential fails. `score` is the fraction that passed, and
`None` when no stage declared one: unmeasured, not bad.

**`self_reports` are claimed.** A stage that already makes an LLM call may add an
optional `self_report` to its output schema — `score`, `went_well`,
`could_improve` — so it costs no extra model call. Ask for the concrete obstacle:
a missing tool, an ambiguous instruction, malformed input. Treat it as a lead,
never as evidence: on this codebase's own first run, three stages self-scored 1.0
while the pipeline was measurably at 0.667 and doing nothing at all.

**Never average the two.** They are separate fields all the way to the UI, and
mixing them lets an optimistic model raise a measured number.

## Which board does a problem go to

- **Review queue** — the *output* of a working run. "Is this draft good enough to
  send." A human decision the pipeline correctly declined to make.
- **Kanban** — the *pipeline itself*. A failing self-assessment means it is not
  doing its job, which is nobody's decision and needs development work.

The wrapper files the second automatically: `file_health_task(app, run_id,
assessment)` from `invoke_workflow`. It is idempotent on the app plus the set of
failing stages, so a job firing every ten minutes opens one task rather than 144
a day, and a new kind of fault opens a new one.

### Self-healing

That task is a repair job, not a notification. It is assigned to `default` and
opens with its workspace on `/opt/workflows`, so the agent that picks it up has
the pipeline's own source in front of it and can edit it — that tree is inside
the profile's write-safe root. The body carries the failing checkpoints, the
error log, the self-reports, and instructions to work them.

Two rules make the loop trustworthy rather than merely automatic:

- **Only a code fault is the agent's to fix.** A missing credential, mount or
  env var is a deployment fault; no edit makes it work, and editing around it
  makes the pipeline lie about its own health.
- **Never make a checkpoint pass without making the stage work.** Removing a
  checkpoint, loosening it, or reporting success unconditionally raises the score
  while the pipeline stays broken — worse than the original fault, because it
  also destroys the signal.

So a fault the agent cannot genuinely fix ends as `blocked` with a typed reason
(`needs_input`, `capability`, `dependency`, `transient`) naming the concrete
obstacle. That is a successful outcome, not a failure of the loop.

## Running evals

Evals need the `agents-cli` binary, which lives in the workflows container, not
here. Ask the user to run:

    docker compose exec workflows uv run agents-cli eval run \
      --dataset tests/eval/datasets/<name>/<name>.json --concurrency 1

`--concurrency 1` is required on a local model: the default fires every case at
once and Ollama serialises them until they all time out.

An agent without a passing eval suite is not done. A new agent needs a dataset
covering rich, thin, ambiguous, correct-answer-is-low, oddly formatted, and
prompt-injection cases.

## Reading results

- Workflow invocations append to `/opt/data/adk/traces/<app>/<date>.jsonl` —
  `run_id`, `status`, `attempt`, `duration_ms`, `error`. Read with file tools.
  **This is the only durable record.** There is no tracing backend in this
  stack: the console reads these JSONL files directly with DuckDB, and
  `metrics.duckdb` is a cache derived from them.
- Eval results have no durable home yet. Per-case detail lands under
  `artifacts/`, which is not mounted and does not survive a container recreate,
  so copy anything you need out of a run before it ends, or write it into the
  JSONL record.

A workflow is healthy when a recent line reads `status: ok`. **No recent line at
all is the failure worth catching** — it means the job stopped firing, which is
silent unless someone looks.
