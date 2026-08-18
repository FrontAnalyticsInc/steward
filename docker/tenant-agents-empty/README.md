# tenant-agents-empty

Placeholder so `docker compose up` works without a tenant repo. The workflows
service mounts a tenant's agent directory at `/code/agents_local`, and Docker
creates a missing bind source as **root** — which the service, running as a
normal user, then cannot read. An empty directory that exists is cheaper than
that failure.

Point `TENANT_AGENTS_DIR` at the real one to use it:

    TENANT_AGENTS_DIR=/home/you/hermes-tenant-you/agents

Every immediate subdirectory with an `__init__.py` is discovered as a workflow,
exactly like the shipped ones under `app/agents/`. A directory whose name
matches a shipped agent replaces it, which is how a tenant customises a
workflow without forking this repo.

Nothing tenant-specific belongs in *this* directory — it is part of the
platform repo and is meant to stay empty.
