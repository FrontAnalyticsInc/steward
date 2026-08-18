# Your own workflows

Anything you put in this directory becomes a workflow agent this Steward can
run, without forking or rebuilding anything we ship. The workflows service
mounts it read-only at `/code/agents_local` and `app/registry.py` scans it after
the built-in agents.

## The shape

One directory per agent, named the way you want the agent named, containing at
minimum an `__init__.py` that exports the agent:

    agents/
      weekly_supplier_digest/
        __init__.py
        agent.py

`__init__.py` must export either `root_agent`, or exactly one name ending in
`_agent`. Two of those and no `root_agent` is an error rather than a guess —
registering the wrong one silently is worse than refusing.

    # __init__.py
    from .agent import root_agent

    __all__ = ["root_agent"]

Directories whose name starts with `.` or `_` are skipped. `_` is not "disabled":
it marks a shared module that other agents import, which is why it is not itself
loaded as an agent.

## Replacing and disabling shipped agents

A directory here whose name matches a built-in **replaces** it. Built-ins are
searched first and overlays win, so this is how you keep our name and routing
while changing what the workflow does.

To switch a built-in off instead, set `HERMES_DISABLED_AGENTS` in your `.env` to
a comma-separated list of bare module names, then restart:

    HERMES_DISABLED_AGENTS=kestrel_site_kg,summarize_note

## Making a change take effect

Agents are imported once, when the workflows service starts. Editing a file here
does nothing until you restart it:

    docker compose -f /srv/steward/stack/steward-stack.yml \
                   --env-file /srv/steward/stack/.env restart workflows

## If one of them is broken

An agent in this directory that fails to import is **skipped, not fatal** — the
service starts, every other workflow keeps running, and the error is logged and
served at `/agent-load-errors` on the workflows service. Check there first if a
workflow you added is not in the playground.

That is the opposite of how a built-in behaves: a broken shipped agent stops the
service on purpose, because it means the release is wrong. Here it means your
file is wrong, and taking every other workflow down with it would be a poor
trade.

## What is versioned

This directory is inside the tracked part of the data disk, so your agents are
committed alongside `SOUL.md` and your memories, and survive upgrades. See
`docs-ops/reference/customization.md`.
