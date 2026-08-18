---
description: >-
  Self-hosted agent infrastructure: a tool-use gateway, a temporal knowledge
  graph, and evaluated ADK workflows, on one machine.
---

# Hermes

Hermes is an infrastructure-as-code stack for running an autonomous agent on
hardware you control. It bundles a tool-use gateway, a temporal knowledge graph,
a scheduler, and a workflow runtime into one `docker compose up`.

It is designed around a specific constraint: **everything runs on a single host,
and the model can run locally.** There is no managed control plane, no cloud
credential requirement, and no per-token bill for the workflow agents. That
shapes most of the decisions documented here.

## What you get

<div class="grid cards" markdown>

-   __Agent gateway__

    ---

    Tool execution, sessions, cron, and a kanban board the agent files its own
    work into.

    [:octicons-arrow-right-24: Overview](architecture/overview.md)

-   __Wiki memory__

    ---

    Markdown files with an FTS5 index — one document per entity, appended to
    with dated sections, so it records what was learned and when.

    [:octicons-arrow-right-24: Wiki memory](architecture/wiki-memory.md)

-   __Evaluated workflows__

    ---

    Google ADK agents with deterministic eval suites. An agent without a passing
    suite is not shippable.

    [:octicons-arrow-right-24: Workflows](architecture/workflows.md)

-   __Operator dashboard__

    ---

    Chat, kanban, approvals, integrations, and a graph explorer over one FastAPI
    backend.

    [:octicons-arrow-right-24: Dashboard API](reference/dashboard-api.md)

</div>

## Design commitments

**Local models are a first-class path, not a fallback.** The workflow runtime
defaults to Ollama on the host. A Gemini API key is the alternative, not the
assumption.

**A fresh clone plus a `.env` produces a working stack.** Configuration is
versioned; runtime state and credentials never are. The split is enforced and
[documented explicitly](deploy/configuration.md#what-lives-where), because
getting it wrong is how a portable stack quietly grows a dependency on one
machine.

**Credentials are minted per host.** They are never copied between deployments —
a shared OAuth token can be invalidated by another host's refresh.

**Evaluation gates releases.** Workflow grading is deterministic rather than
LLM-judged, so a pass means the same thing on every run.

!!! note

    Hermes assumes a trusted host. The gateway mounts the Docker socket to
    create its tool sandbox, which is equivalent to root on that machine. Read
    [Network exposure](deploy/configuration.md#network-exposure) before putting
    it on a network you do not control.

## Start here

<div class="grid cards" markdown>

-   __Quickstart__

    ---

    From clone to a running stack.

    [:octicons-arrow-right-24: Quickstart](quickstart.md)

-   __Prerequisites__

    ---

    The one thing that is deliberately not containerised, and why.

    [:octicons-arrow-right-24: Prerequisites](deploy/prerequisites.md)

</div>
