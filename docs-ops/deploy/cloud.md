---
description: Running the stack on a cloud VM — sizing, the Terraform, and what changes from a local install.
---

# Cloud deployment

The stack is Docker Compose on one host, so a cloud deployment is a VM, a data
disk and an egress-only firewall. `infra/` holds the Terraform and the
cloud-init; this page is what changes conceptually.

## What is different from a local install

| | Local | Cloud |
|---|---|---|
| Models | Ollama on the host, or Anthropic | Anthropic; no local runtime |
| Memory | Files under `~/.hermes/wiki` | Same, on an attached disk |
| Telemetry | Optional (`--profile telemetry`) | Off — seven containers, ~3.4 GB |
| Images | Built locally | Pulled from GHCR |
| Access | Loopback on your machine | Tailscale; no inbound firewall rules |

Nothing about the application changes. There is no cloud-specific code path,
no managed service, and no second configuration format.

## Sizing

Measured resident memory with telemetry off and no local model:

| Service | RSS |
|---|---|
| workflows | 423 MiB |
| gateway | 354 MiB |
| hermes-dashboard | 302 MiB |
| browser | 241 MiB |
| light-dashboard | 218 MiB |
| review-executor | 117 MiB |
| docs | 13 MiB |
| **Total idle** | **~1.6 GB** |

An earlier version of this stack ran a **LiteLLM proxy**, which measured 1088
MiB — 40% of the footprint on its own, for a Python application supporting
around a hundred providers whose imports cost what they cost. It was dropped:
its documented floor is 4 CPU / 8 GB with a Postgres beside it, which is more
machine than everything above put together. The two things it gave us are now
in-process — `app/model_aliases.py` for aliasing and `app/cost_ledger.py` for
pricing, against the same maintained price map.

- **4 GB / 2 vCPU** — the default in `infra/`; ~2 GB free after the OS
- **8 GB / 2 vCPU** — comfortable if you run several sandboxes at once

There is nothing sensible below 4 GB: 2 GB leaves no room for a sandbox
alongside Chromium, and a 3 GB custom shape carries a per-unit premium that puts
it near or above the 4 GB standard price for less memory.

The headroom is for tool sandboxes, provisioned at 5 GiB each
(`hermes/config.yaml.template`), and the Chromium the browser service runs.
cloud-init provisions a 4 GB swap file so two peaks landing together degrade to
slow rather than to an OOM kill, and every long-lived service carries a
`mem_limit` so the killer takes a runaway rather than whatever it reaches first.

!!! warning "A `mem_limit` is not enough on its own for DuckDB"

    DuckDB backs the Metrics tab, and it sizes its own memory budget at 80% of
    the container limit. That heuristic assumes it is the only thing in the
    container. It is not: the dashboard idles around 218 MiB before any query
    runs, so inside a 512 MiB cgroup DuckDB would budget 409 MiB on top of that,
    reach 627 MiB, and be OOM-killed — taking the dashboard with it instead of
    spilling to disk.

    `metrics_store.py` therefore sets `memory_limit` explicitly
    (`METRICS_DUCKDB_MEMORY_LIMIT`, default 320 MB) and a `temp_directory` on
    the data disk, and the service's `mem_limit` is 768m rather than 512m so
    that subtraction has slack. The two numbers are chosen together; changing
    one without the other reintroduces the problem.

    320 MB is a floor as well as a ceiling: a ten-year range (`outputs(3650)`,
    which the UI offers) needs more than 200 MiB, and **not every allocation
    can spill** — below that it throws `OutOfMemoryException` instead. Spilling
    is slower and always correct; being killed is neither; throwing is in
    between and at least names itself.

    Worth knowing if you add another memory-hungry engine to a shared
    container: the pattern is general, and adding a `mem_limit` is what exposes
    it. DuckDB reads CPU limits correctly, so `threads` needs no such treatment.

Even 4 GB is only possible because Neo4j went: it alone held 2.4 GB.

## Why not a managed container service

Cloud Run, ECS Fargate and Container Apps are all ruled out by one thing: the
gateway bind-mounts `/var/run/docker.sock` to spawn its own tool-execution
sandboxes. There is no equivalent on a serverless container platform without
Docker-in-Docker or a rearchitecture of how Hermes runs tools.

Two secondary reasons, either of which would be enough on its own: the approvals
queue's six-way directory split *is* the producer/reviewer security boundary
(`compose:170, 491, 691-694`), and collapsing it onto shared storage would
remove the gate; and the stack is a stateful single-tenant appliance rather than
a request handler that scales horizontally.

## Access

Tailscale, with **no inbound firewall rules at all** — including SSH, which
Tailscale authenticates by identity (`tailscale up --ssh`).

This is not optional hardening. The operator console binds `0.0.0.0:9120` with
no authentication of any kind, and it holds the gateway's API key, so anything
that can reach it can act as the agent. Publishing it is equivalent to
publishing your mailbox.

## Secrets

Terraform handles one secret: the Tailscale auth key. Everything else is copied
over Tailscale after the box is up.

The reason is specific rather than general caution. A Terraform variable becomes
instance metadata; instance metadata is readable by anything running on the
host; and the agent executes code it writes itself, in a sandbox on that host.
One manual `scp` is a better trade than putting a billable provider key
somewhere the agent can read it.

## Backups

The only irreplaceable state is `HERMES_DATA_DIR` — sessions, kanban, cron jobs,
memories, the wiki, and credentials — plus the approvals queue. Both live on the
attached data disk, which is a separate Terraform resource with
`prevent_destroy` so an accidental `terraform destroy` takes the instance and
stops.

`${HERMES_DATA_DIR}/browser/profile` holds live session cookies for everything
the headless browser has logged into. Treat the backup archive as a credential.

## Verifying

The one test worth running before anything depends on the box:

```bash
terraform taint 'module.vm_aws[0].aws_instance.this'
terraform apply
```

The disk must reattach with the wiki, sessions, approvals and browser profile
intact. That validates the IaC and is the disaster-recovery drill at the same
time. Running it on **both** providers once is what proves the abstraction.

See [`infra/README.md`](https://github.com/FrontAnalyticsInc/hermes-infra/blob/master/infra/README.md)
for the apply commands and the three places the provider abstraction genuinely
leaks.
