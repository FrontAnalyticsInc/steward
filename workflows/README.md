# workflows — ADK agent workflows

Local ADK project holding our workflow agents. Scaffolded with `agents-cli`
v1.3.1, evaluated with `agents-cli eval`, served over HTTP for the Hermes
scheduler.

**Local only.** No GCP deployment and no Application Default Credentials — the
Google Cloud SDK is *not* a requirement here. See `GEMINI.md` for the
conventions every agent follows.

## Agents

| Agent | Purpose |
|---|---|
| `enrich_contact` | Turns a conference contact into a structured record with three independent fit scores (product, consulting, employment). |

## Project structure

```
workflows/
├── app/
│   ├── agent.py               # root agent; names no workflow
│   ├── registry.py            # discovers workflows; overlay + disable rules
│   ├── config.py              # the model config (only place a model is named)
│   ├── fast_api_app.py        # local FastAPI server
│   ├── agents/<name>/         # one package per workflow
│   └── app_utils/
├── tests/
│   └── eval/                  # dataset builder, deterministic grader, config
├── Dockerfile                 # pinned by digest
└── pyproject.toml
```

## Requirements

- **uv** — package manager
- **agents-cli** — `uv tool install google-agents-cli==1.3.1`
- **`ANTHROPIC_API_KEY`** — the default provider, and the only one a deployed
  stack uses. Ollama and Gemini remain as local-development branches; neither is
  part of a deployment.

## Quick start (host)

```bash
cp .env.example .env      # then set ANTHROPIC_API_KEY
agents-cli install        # sync dependencies

agents-cli playground     # interactive, hot reload
agents-cli run "..."      # one-shot, non-interactive
```

## Quick start (compose — the deliverable)

From `../docker`:

```bash
docker compose up -d --build workflows
```

- Workflows API: `http://127.0.0.1:8020` — loopback only, never the LAN

If you switch this host to `WORKFLOWS_MODEL_PROVIDER=ollama`, note that the
container cannot reach an Ollama bound to `127.0.0.1`: on a bridge network that
address is the container, not the host. Bind Ollama somewhere the bridge can
reach and point `OLLAMA_API_BASE` at it.

## Evals

```bash
# Regenerate the dataset (renders cases through the agent's own message builder,
# so the eval exercises the real untrusted-input fencing)
uv run python tests/eval/build_dataset.py

# Inference + grading.
# --concurrency 1 is REQUIRED on a local model: the default fires every case at
# once and Ollama serialises them until they all time out.
agents-cli eval run \
  --dataset tests/eval/datasets/enrich_contact/enrich_contact.json \
  --concurrency 1
```

Inside the container — this is what actually gates a release:

```bash
docker compose exec workflows uv run agents-cli eval run \
  --dataset tests/eval/datasets/enrich_contact/enrich_contact.json \
  --concurrency 1
```

Grading is **deterministic** (`tests/eval/schema_conformance.py`), no LLM judge.
Schema conformance is a hard pass/fail, and each case category adds behavioural
assertions: thin input must be low-confidence and flagged, an injection attempt
must not be obeyed, an ambiguous title must not resolve upward.

## Adding an agent

1. `app/agents/<name>/` with `__init__.py`, `schema.py`, `prompt.py`,
   `agent.py` — copy `enrich_contact` for the shape.
2. Typed Pydantic input/output; pass the output model as `output_schema=`.
3. Untrusted text goes in the **user message** via `build_user_message()`, never
   interpolated into the instruction.
4. Nothing to register — `app/registry.py` discovers any directory under
   `app/agents/` with an `__init__.py`. Export `root_agent` (see step 5 of the
   authoring skill) so discovery knows which object to register.
5. If it drafts anything a human reviews and sends, add
   `before_model_callback=apply_user_context` (`app/user_context.py`) so it
   writes from Hermes's profile of the operator rather than a sketch of its own.
6. Add `tests/eval/datasets/<name>/` and extend the dataset builder.
7. Run the evals. An agent without a passing suite is not done.

Full rules, including the ADK gotchas that will bite you, are in `GEMINI.md`.

## Telemetry

**No spans leave the box.** ADK exports OTLP automatically whenever
`OTEL_EXPORTER_OTLP_ENDPOINT` is set, and compose deliberately does not set it:
the trace store was removed once nothing turned out to read it, and the
collector in front of it went too, since a receiver with no backend only batches
spans and drops them.

What records a run instead is a JSON line per invocation under
`traces/<app>/<date>.jsonl` — agents, turns, tokens, timings, status — read with
DuckDB by the console's scorecard, and reviewed on a schedule by the Hermes
worker so a degrading workflow becomes a kanban task. That is the observability
this project has, and it needs no service.

What it does not give you: the prompt and response text of a run is not kept
anywhere, so a bad answer cannot be read back after the fact. Per-case eval
detail under `artifacts/` is still not mounted and still dies on a container
recreate.

To add spans, restore a collector service with a config naming a real exporter
and set `OTEL_EXPORTER_OTLP_ENDPOINT` on this service again — pointing at the
collector, never at a store directly. That indirection is what let a store be
removed without this service changing at all.

## Portability

A fresh clone needs only:

```bash
cp workflows/.env.example workflows/.env    # then set ANTHROPIC_API_KEY
cd docker && docker compose up -d --build workflows
```

There is no host prerequisite beyond Docker. The default provider is Anthropic
and it is reached over the network, so nothing has to be installed, pulled or
kept running beside the stack.

Base image, `google-adk`, `google-agents-cli` and `uv` are all version-pinned,
so a rebuild resolves the same versions.
