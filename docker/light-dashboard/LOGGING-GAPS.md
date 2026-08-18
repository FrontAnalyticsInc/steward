# What the Integrations screen cannot tell you, and where the fix belongs

The screen derives status from the outcome of real calls. Where an outcome is
not recorded it says so — `◌ used` with the basis "a call was made; the gateway
records no outcome" — rather than rendering activity as a green tick. This file
is the long form of the gaps panel on the screen itself.

Every fix listed here is a fix in the **logging**. None of them is a probe. A
synthetic ping proves a credential authenticates; it does not prove the scope
you need is still granted, and a narrowed scope is the failure that actually
happens. A probe layer would report green through exactly the outage the screen
exists to catch.

## 1. MCP tool calls record no outcome — the one that matters

**Status today.** `~/.hermes/state.db` holds one `messages` row per MCP tool
result, with `tool_name` (`mcp__<server>__<tool>`), `timestamp`, and the
session it belongs to. That is enough for *who called what, when*, and the
screen uses it. It is not enough for *how it ended*: there is no status column,
no error column, and the stored `content` is the tool payload — a 401 and a
successful empty result are both just text.

**Consequence.** Every Hermes-side grant reads `used`, never `working` and
never `failed`. A revoked Gmail scope is invisible here until someone notices
the replies stopped.

**Fix, in `nousresearch/hermes-agent`:**

1. `agent/tool_dispatch_helpers.py` → `make_tool_result_message()` already
   receives the MCP `CallToolResult`, which carries `.isError`. Add
   `tool_status` (`"ok"` / `"error"`) and a trimmed `tool_error` to the message
   dict it builds.
2. `hermes_state.py` → `append_message()` (and the second insert in
   `_insert_message_rows()`) persists the message. Add the two columns to the
   `messages` table and to both INSERT statements.
3. `run_agent.py` around line 1965 passes the message fields through; add the
   two new ones.

Then delete the `unverified` branch here: `integrations.mcp_calls()` sets
`ok=None` in exactly one place, and it becomes `row["tool_status"] == "ok"`.

**Constraint.** The gateway runs from the prebuilt image
`nousresearch/hermes-agent:latest`, and nothing bind-mounts over `/opt/hermes`.
The host clone at `~/.hermes/hermes-agent` is a *different, older* commit than
the image (the image's revision is not even an object in that clone), so a
patch written against it will not apply cleanly to what is running. This change
is upstream work, or an image-overlay patch extracted with `docker cp` — not an
edit anyone can make in this repo. That is why it was not made here.

## 2. Consumers are session sources, not agent profiles

**Status today.** `sessions.profile_name` is NULL for every session the gateway
writes, so a grant is attributed to `api_server` / `cron` / `tui` / `telegram`
— which the screen labels *chat*, *scheduled jobs*, and so on. Useful, but
coarse: every scheduled job collapses into one "scheduled jobs" consumer, so
you cannot tell *which* cron job's Gmail grant went stale.

**Fix.** The gateway knows the profile when it creates a session; populate
`sessions.profile_name`. For cron specifically, `~/.hermes/cron/executions.db`
already records per-job executions and could be joined on time, but writing the
job id onto the session is the honest version.

## 3. Workflow calls: covered, and how

The ADK side *does* record outcomes, because that code is ours.
`workflows/app/integration_log.py` appends one JSON line per outbound call —
source, consumer, capability, operation, ok, error — to
`$INTEGRATION_CALL_LOG_DIR` (`~/.hermes/adk/integration-calls/<date>.jsonl`,
mounted into the workflows container at `/code/adk-state`).

Wired in at `gmail_api._retry()`, the single funnel every Gmail call already
passes through, and attributed by `integration_log.consumer_scope()` set once
per pipeline stage.

Not yet covered on this side:

- **`graph_sink.py` makes no HTTP calls** — it appends JSONL and a separate
  replay script ingests it. Its "did the graph accept this" outcome lives in
  that replay step, which is where a `record()` call belongs.
- **Any new outbound client** must call `integration_log.record()` or it will
  show as `never used` no matter how often it runs. There is no interception
  layer and deliberately so: a wrapper that guessed at outcomes would
  reintroduce the problem this file is about.

## 4. Run traces are per-run, not per-integration

`~/.hermes/adk/traces/<app>/<date>.jsonl` (written by
`hermes/scripts/invoke_workflow.py`) records `status`, `tool_calls_by_name` and
per-agent turn counts for a whole invocation. It cannot say which integration a
failed run failed against, so the Integrations screen does not read it. The
scorecard on the Agents tab is the right consumer for that data.

## 5. `integrations.json` is optional, and without it nothing is "stale"

Sources, consumers and last-used are all derived. The config supplies only what
no log can state: a human label, the credential type, and each grant's
**expected interval**. Without an interval nothing can be overdue, so a daily
job that died six days ago looks exactly like one that ran this morning.

Copy `integrations.example.json` to `~/.hermes/integrations.json` and set the
intervals. A grant in the logs but not in the config still displays, under raw
identifiers; a grant in the config but never seen in the logs displays as
`never used`, which is usually a credential issued and forgotten.
