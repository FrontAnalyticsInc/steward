# The automation library

Four automations ship with every install. None of them runs, and none of them
*can* run, because a template is not a job.

That is a stronger guarantee than a switched-off job. A disabled job is one
click and one accident away from firing with a placeholder competitor name in
it. A template has no schedule entry in `cron/jobs.json`, no job id, no
`next_run_at`, and required parameters with no values — the scheduler never
reads this directory at all. The only route from here to a running job is
`render_job()`, which refuses while a required parameter is empty. "Nothing
acts without you" is a property of the data rather than a promise.

Templates live here in the repo and are seeded copy-if-absent to
`<data dir>/automations/library/`. Edit them on a box and an upgrade keeps your
edits; delete one and the next upgrade restores it.

## What a template is

A job with holes in it, plus the schema of those holes. One YAML file per
template, named after its `id`.

```yaml
id: source-scan            # lower-kebab-case, matches the filename
version: 1
title: Weekly scan of a named source
summary: One line. This is what an operator reads when choosing.

tier: prompt-cron          # prompt-cron | adk-pipeline
requires_capabilities: [web_extract]

parameters:
  - name: source_url       # lower_snake_case
    type: url              # string | url | list | integer | boolean
    required: true
    ask: Which page should be watched?     # the question an operator is shown
    help: Optional. Why the answer matters, and what a good one looks like.
    example: https://example.com/changelog

schedule:
  default: "0 9 * * 1"     # anything cron.jobs.parse_schedule accepts
  editable: true
  note: Optional. Why this default.

delivery:
  default: origin          # origin | local | telegram | slack | ...
  editable: true

change_detection:
  mechanism: monitor       # monitor | prompt | none
  memory: monitor_state
  verify: How the operator confirms it is actually working.

job:
  name: "Source scan: {{source_name}}"
  enabled_toolsets: [web]
  monitor_url: "{{source_url}}"
  prompt: |
    ... {{source_url}} ...
```

The rules, all enforced by
`docker/light-dashboard/backend/automation_library.py`:

* **A required parameter may not have a default**, and an optional one must
  have one. A required parameter with a default is not required — the default
  fills the hole this format exists to keep open.
* **Every parameter must be used, and every placeholder must be declared.**
  `{{name}}` is the only substitution: no conditionals, no loops, no
  expressions. A list renders comma-separated, a boolean as `yes`/`no`.
* **The `job:` block is not a second job format.** Its keys are the argument
  names of the scheduler's own `cron.jobs.create_job`, and `render_job`
  returns exactly that kwargs dict. Only `name`, `prompt`, `enabled_toolsets`,
  `monitor_url` and `monitor_script` may appear. `schedule` and `deliver` have
  their own sections; `enabled`, `next_run_at` and the rest are the scheduler's
  runtime state and are rejected by name. `script`, `no_agent` and `workdir`
  are rejected too — a template must not be able to ship code that runs
  unattended, or to point at a host path. That exclusion is also what makes a
  filled template classify as a **prompt cron** in the console: the badge is
  derived from the job, and a job carrying `script`/`no_agent` would be
  labelled a script instead.
* **`change_detection` is required.** See below.

## Change detection is the section that earns the library

`docs/cookbook/competitor-watch.md` carries the lesson: without *"only tell me
what's new"* the reader gets the same summary every week, stops opening it by
week three, and is then **worse off than with no automation**, because they now
believe they are covered. A weekly automation that repeats itself is not a
weak automation; it is a harmful one.

So every template declares how it avoids that, and the claim is checked:

| mechanism | what remembers | strength |
|---|---|---|
| `monitor` | the scheduler hashes `monitor_url` / `monitor_script` output and stores it in `job.monitor_state`, with the previous body under `cron/output/<job_id>/monitor_last_output.txt` | strong — an unchanged page never reaches the model at all, and the tick is recorded as `no_change` |
| `prompt` | the agent's own recall, plus the `[SILENT]` contract the scheduler honours by suppressing delivery | works, and depends on the model. Must be checked in week two |
| `none` | nothing | only for automations meant to produce output every run. Requires a `why` |

Two of those are enforced structurally rather than trusted:

* `mechanism: monitor` requires the job to carry a `monitor_url` or
  `monitor_script`, or there is nothing to compare.
* `mechanism: prompt` requires the literal string `[SILENT]` in the prompt.
  That exact token is the scheduler's suppression contract; a paraphrase of it
  does nothing at all, and a paraphrase is what you write if you have not read
  this.

`verify:` is required for both. Change detection nobody checks is the failure
this section exists to prevent, so the template says how to check it.

`monitor` is narrow — one URL, fetched as plain HTTP, no browser. Pick a page
that changes only when something happened. A page carrying a build hash, a
rotating advert or a "generated at" line is a noise generator, because
comparison is on exact bytes.

## Adding a template

1. Write the YAML. Keep it client-agnostic: whose competitors, which sources
   and what schedule are configuration, not content. A template that names a
   company is a demo.
2. Check it:
   ```
   python3 docker/light-dashboard/backend/automation_library.py \
       --check hermes/automations/library
   ```
3. Fill it, and read what comes out:
   ```
   python3 docker/light-dashboard/backend/automation_library.py \
       --fill hermes/automations/library/source-scan.yaml \
       '{"source_url": "https://example.com/changelog", ...}'
   ```
   The output is the kwargs for `cron.jobs.create_job`. Filling with `{}`
   should fail, naming every required parameter.
4. `docker/light-dashboard/backend/test_automation_library.py` validates every
   template in this directory, so CI fails on a malformed one.

## Creating the job

`render_job` returns arguments. It creates nothing — creation is a human act,
performed today with `hermes cron create` and eventually from the console.

`create_job` has no `enabled` argument and always returns an *enabled* job. A
caller that wants "created but switched off" must follow it with
`update_job(job["id"], {"enabled": False})`.
