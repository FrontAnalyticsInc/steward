---
description: The open-source software Hermes is built on, and the terms each one comes under.
---

# Third-party software

Hermes is assembled from other people's work. This is the list, with the terms
each piece comes under.

| Component | What it does here | License |
| --- | --- | --- |
| [Hermes](https://github.com/NousResearch/hermes-agent) | The assistant itself — chat, tools, scheduling | MIT |
| [LiteLLM](https://github.com/BerriAI/litellm) | SDK the workflows call models through, and the price map costs are read from | MIT |
| [Playwright](https://github.com/microsoft/playwright) | Opens web pages so automations can read them | Apache 2.0 |
| [Chromium](https://www.chromium.org/) | The browser Playwright drives | BSD 3-Clause |
| [Material for MkDocs](https://github.com/squidfunk/mkdocs-material) | The site you are reading | MIT |
| [MkDocs](https://github.com/mkdocs/mkdocs) | Builds the site you are reading | BSD 2-Clause |

All of these are free to run for yourself, and every one is permissive — MIT,
Apache 2.0 or BSD. There is no copyleft anywhere in the stack.

!!! note "This changed, and in your favour"

    Earlier versions stored what the assistant learned in Neo4j Community
    Edition, which is **GPL v3**. That was defensible — Neo4j ran as its own
    program and Hermes spoke to it over the network — but it was the one
    component that made "can I ship this to customers?" a question for a
    lawyer rather than a reading of the table above.

    What it has learned is now plain files, indexed with SQLite, which is
    public domain and already part of Python. The GPL dependency is gone
    rather than argued around.

## The browser

Some of the [cookbook recipes](../cookbook/index.md) read websites — competitor
watch, the website topic map. Many modern sites build their pages in the
browser, so fetching the raw address returns an empty shell. Playwright drives a
real Chromium to open the page properly and read what a person would see.

Both are permissive: Playwright is Apache 2.0, Chromium is BSD 3-Clause. Neither
places conditions on selling or hosting what you build.

!!! note "This used to be Browserless"

    Earlier versions used Browserless here, which is dual licensed **SSPL 1.0 or
    commercial** — the SSPL is not considered open source by the Open Source
    Initiative, and Browserless state that commercial applications require their
    paid license.

    That is worth knowing if you are running an older install. Current versions
    do not include it.

Nothing else in Hermes depends on the browser. If web-reading is not something
you need, the container can be left out entirely — the recipes that use it stop
working and the rest is unaffected.

## Nothing records what automations did

There is no tracing in this stack at all — no store, and nothing collecting for
one — so there is no third-party license here to read about it.

!!! note "What that costs, plainly"

    Nothing keeps the prompt or response text of a model call, so an automation
    that misbehaves cannot be read back afterwards. What is kept is one JSON
    line per run — status, duration, error, and counts of what the run read and
    produced — which says *that* something went wrong and how often, but not
    what was said.

    Earlier versions of this stack ran a trace store and a collector in front of
    it. Both were removed once it was clear nothing in the repo read them, and
    no third-party component here records model text today.

## Checking for yourself

Each name above links to its source. Licenses do change between versions — the
ones listed here were read off the versions this actually runs.
