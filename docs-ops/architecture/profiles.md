---
description: The three Hermes profiles, why the split exists, and how GSD Core fits into it.
---

# Profiles

A Hermes profile is a self-contained agent: its own `config.yaml`, `SOUL.md`,
`skills/`, `memories/` and `cron/`. The default agent is `HERMES_HOME` itself;
the rest live under `HERMES_HOME/profiles/<name>`.

There are three, split by role. The kanban decomposer routes tasks on the profile
*description* (`hermes profile describe <name>`), not the name, so each one
carries a description saying what it is good at:

| Profile | Model | Role |
|---|---|---|
| `default` | gpt-5.5 | Conversation and general operation. Kept deliberately lean. |
| `dev` | gpt-5.5 | ADK workflow authoring, pipeline repair, GSD phase loops. Owns `/opt/workflows`. No MCP servers. |
| `worker` | gemma4 12b (local) | Cheap, well-scoped, high-volume tasks. `kanban.default_assignee`. |

## Why the split exists

Skills are charged to whichever profile carries them. Every installed skill
contributes a line to the always-on `<available_skills>` index in the system
prompt, so GSD's 70 skills on `default` made that index 8.9 KB — 78% GSD — and
put `gsd-autonomous` ("run all remaining phases autonomously") in front of the
agent you chat with.

Moving GSD to `dev` cut `default`'s index to 2.8 KB while giving repair work a
*better* planner. Check any profile's cost with:

```bash
hermes -p <name> prompt-size
```

Workflow repair tasks therefore assign to `dev` (`WORKFLOW_HEALTH_ASSIGNEE` in
`invoke_workflow.py`).

!!! note "The split is about role, not permissions"

    Write access does **not** distinguish these profiles.
    `HERMES_WRITE_SAFE_ROOT` is a container-level environment variable read
    identically by every profile, so any of them can write `/opt/workflows`.

## Who authors a workflow

`default` designs; `dev` builds. Anything that writes under `/opt/workflows` is
`dev`'s work, down to a one-line change — the line is the directory, not the
size, because every edit looks small from the seat proposing it.

`default`'s job is the design conversation, then a kanban task assigned to `dev`
with that design in the body and `--workspace dir:/opt/workflows`. A task reading
"add a workflow for X" throws away the only part that needed a human in the room.

For that handoff to work at all, `default`'s `toolsets` includes `kanban` (see
`hermes/config.yaml.template`). The kanban toolset is otherwise live only for
dispatcher-spawned workers — the gate is literally `"kanban" in cfg["toolsets"]`,
with an exemption for processes carrying `HERMES_KANBAN_TASK`. Without the opt-in
`default` has no `kanban_create`, and since `terminal` is disabled it cannot shell
out to `hermes kanban` either.

That combination fails in the worst possible way, and did once: the design
conversation succeeds, the agent produces a complete task spec, and then the
handoff dies in an approval prompt that does not exist over the API. The work is
stranded in a chat log with nothing on the board. Telling a profile to route work
somewhere is not enough — it has to be given the means.

Only `default` opts in. `dev` and `worker` receive the kanban tools when the
dispatcher spawns them, which is what correctly scopes a worker to its own task
rather than the whole board.

This rule lives in `default`'s `SOUL.md` rather than only in a skill, because it
has to hold on requests that never mention ADK. Asked to automate something, an
agent reaches for the nearest lever — a cron pointed back at itself, a
`delegate_task` to another profile, a task on the board — and all three automate
*Hermes* when the request wanted a pipeline. `SOUL.md` is always in context; a
skill has to be selected first.

## Recreating `dev` on a new host

`dev` is deliberately **not** seeded from the repo:

```bash
hermes profile create dev --clone-from default \
  --description "Software development: ADK workflow authoring, pipeline repair, and GSD spec-driven phase loops. Owns the /opt/workflows source tree."

# A clone inherits default's MCP servers. dev must have none.
for s in gmail attio; do hermes -p dev mcp remove "$s"; done

# dev's tool sandbox must also reach the cron scripts dir (see below).
hermes -p dev config set terminal.docker_volumes \
  '["/home/alton/hermes-infra/workflows:/opt/workflows","/home/alton/.hermes/scripts:/opt/data/scripts"]'

./hermes/install-gsd.sh --profile dev
```

!!! warning "dev's sandbox needs `/opt/data/scripts` mounted"

    A workflow is only half-built until its cron wrapper exists, and cron
    resolves `script` under `HERMES_HOME/scripts`, rejecting anything outside
    it — so the wrapper is only reachable at `/opt/data/scripts`. dev's tool
    sandbox mounts `/opt/workflows` alone by default, so a build would finish,
    pass its tests, register in ADK, and then fail acceptance with
    `mkdir: cannot create directory '/opt/data': Permission denied`. That
    happened on the first real task.

    Mount the scripts directory specifically, never `/opt/data` wholesale:
    that path also holds `auth.json`, `state.db` and the kanban DB, none of
    which belong in a sandbox running generated code.

    What dev still cannot do is run anything: `terminal` is disabled and
    `execute_code` hits an approval barrier in a headless kanban run. It can
    author and lint, and it can fire a one-shot `no_agent` cron once the
    wrapper is in place, but `pytest` and eval runs remain an operator step.
    Do not put "manual test run succeeds" in a dev task and expect dev to
    close it.

!!! warning "`dev` carries no MCP servers, and that is load-bearing"

    `gmail` and `attio` are runtime integrations — how a workflow
    reaches the outside world, not how one gets built. A build seat holding them
    can quietly do the job itself instead of writing the pipeline that does it:
    the same failure the substrate rule in `SOUL.md` exists to prevent, with
    better tools to hand. It also keeps live mail and CRM credentials out of the
    profile that runs autonomous repair loops against a source tree.

    Verify with `hermes -p dev mcp list` — it should report none. Do not skip
    this when rebuilding: **the clone brings them back every time.**

Unlike `worker`, there is no `hermes/profiles/dev/config.yaml.template`. A cloned
profile config contains the dashboard `secret` and `password_hash` copied from
the source profile, so vendoring one would commit credentials. Clone it on the
host instead.

A profile spawned by the dispatcher does not need its own running gateway —
workers are `hermes -p <profile> --cli` subprocesses.

## GSD Core

[GSD Core](https://github.com/open-gsd/gsd-core) gives the agents a spec-driven
development loop — Discuss → Plan → Execute → Verify → Ship — with the heavy
research and planning work pushed into fresh subagents so the main session does
not fill up.

Hermes is a supported runtime upstream, so the vendor installer handles the port:
it rewrites Claude's `Task()` dispatch to Hermes's `delegate_task` and adds the
`version:` frontmatter the SKILL.md spec requires.

```bash
./hermes/install-gsd.sh --profile dev              # where it actually lives
./hermes/install-gsd.sh --profile dev --uninstall
```

GSD is installed into `dev`, not `default` — see [above](#why-the-split-exists)
for why.

!!! note "The namespace-router layout does not reduce cold-start cost here"

    The installer lays the 72 skills out as 6 namespace routers with the rest
    nested beneath them, intending cold start to pay 6 descriptions rather than
    72. Hermes's skill loader recurses, so `hermes skills list` shows all 71
    registered and enabled under category `gsd`. If that is more context than a
    profile should carry, trim it with `skills.disabled` in `config.yaml`.

Unlike everything else under `hermes/`, GSD is **not** seeded from this repo. It
is ~626 vendor files that upstream manages through its own installer, so the
script pins a version (`GSD_VERSION` at the top) and installs from npm instead;
upgrading is a one-line change.

It runs the installer *inside* the gateway container, as uid 1000 — the installer
bakes absolute paths into what it writes, and only a container-side run produces
paths (`/opt/data/...`) that the gateway can actually resolve. That also means
the stack must already be up, which is why `seed.sh` does not call it.

GSD supplies the *process*, not domain knowledge. For ADK work it pairs with the
`google-agents-cli-*` and `adk-workflows` skills, where
`/gsd-ai-integration-phase` drives framework selection and research into an
`AI-SPEC.md` design contract.

!!! warning "One known no-op"

    The installer also writes a `settings.json` of guard hooks, and Hermes has no
    `settings.json` hook surface, so those never fire. The skills are the part
    that works.
