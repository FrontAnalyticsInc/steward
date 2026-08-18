---
description: What the host needs, and the one dependency that is deliberately not containerised.
---

# Prerequisites

## Host software

| Requirement | Version |
|---|---|
| Docker | 20.10+ |
| Docker Compose | v2.0+ |
| Git | any recent |
| Ollama | **not required** — development-only option, see below |

For developing workflow agents outside the container you also want
[`uv`](https://docs.astral.sh/uv/) and `uv tool install google-agents-cli`.
Neither is needed to run the stack.

## Ollama is optional, and not a container

Nothing in a deployment uses it. `WORKFLOWS_MODEL_PROVIDER` defaults to
`anthropic`, every model call goes straight to the vendor, and the installer
never asks about a model runtime. This section is for a development host that
wants to run the workflows locally.

If you do want it, **Ollama runs on the host**, serving the model named in
`WORKFLOWS_OLLAMA_MODEL`:

```bash
ollama serve
ollama pull <your-configured-model>
```

This is a deliberate exception to "everything runs in containers", for two
reasons:

1. **Weights are large and shared.** Model files are measured in gigabytes and
   are usually already on the machine for other purposes. Baking them into an
   image, or bind-mounting a cache into a container that duplicates the runtime,
   buys nothing.
2. **GPU passthrough is host-specific.** Containerising it would push a
   per-machine hardware configuration into a file that is supposed to be
   portable.

This used to be the reason several services ran with `network_mode: host`:
Ollama binds `127.0.0.1`, and a container on a bridge network cannot reach that.
No service uses host networking now — every one is on the internal bridge and
resolves the others by name — so running against a local Ollama means binding it
somewhere a container can reach and pointing `OLLAMA_API_BASE` at that address.

!!! note

    To skip Ollama — which is the default and what every deployment does —
    leave `WORKFLOWS_MODEL_PROVIDER` at `anthropic`. Every model call —
    from the workflows and from Hermes alike — goes straight to the vendor. No
    proxy container sits in between. Nothing else needs a model runtime either:
    memory is files and an FTS5 index, so there is no extraction path to
    configure separately.

## Resources

The model runtime dominates, if you run one. As a working floor for the full
stack with a mid-size local model: **8 CPU cores, 32 GB RAM, 50 GB disk**, plus
whatever your model weights occupy.

Against a hosted provider, with tracing disabled, the remaining containers idle
at roughly **1.6 GB** — so **4 GB / 2 vCPU** is comfortable for one operator,
with room for a tool sandbox and the headless Chromium. Two deletions made that
possible and both were large: Neo4j held 2.4 GB, and the LiteLLM proxy — which
an earlier version of this stack ran — held another 1.06 GB to supply model
aliasing and a price map that are now a few hundred lines in-process.

## Before you expose anything

The gateway mounts the host's Docker socket to build its tool sandbox. Anything
that can reach the gateway can create containers on the host, which is
equivalent to root access.

The operator console (`9120`) publishes on **all interfaces with no
authentication at all**, and it holds the gateway's API key — so anything that
can reach it can act as the agent.

Deploy on a host you trust, and read
[Network exposure](configuration.md#network-exposure) before attaching it to a
network you do not.
