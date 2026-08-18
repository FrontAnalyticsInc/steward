---
description: Seeing what runs on its own, checking it worked, and turning things off.
---

# Automations

An automation is something the assistant does on a schedule without being asked
again. You create them by [asking in chat](../chat.md#asking-it-to-do-something-regularly).
This screen is where you see them.

## The list

Every automation shows five things:

| Column | What it tells you |
|---|---|
| **Automation** | What it is called. You named it when you set it up, or it named it. |
| **Schedule** | When it runs — "every 10m", "0 7 \* \* \*" (7am daily). |
| **Status** | Whether it is on, and whether the last run worked. |
| **Last run** | When it last went. The most useful column on the screen. |
| **Where** | Whether it is a conversation-style job or a fixed script. |

**Last run is the one to look at.** An automation that is switched on but has not
run when it should have is the failure you actually want to catch, and it is
invisible in every other column.

!!! tip "Drift"

    If an automation ran later than its schedule says it should have, the list
    flags it as *drift*. Usually this means the machine was asleep or busy. If
    it keeps happening, the schedule is probably tighter than the work takes.

## Checking one worked

Click an automation to open it. You get what it is instructed to do, when it last
ran, and — if the last run failed — the error.

**Run now** fires it immediately rather than waiting for the schedule. This is
the fastest way to check a new automation does what you meant before you leave it
running for a week. It is also the honest way to test one that is meant to run at
6am.

!!! note

    Some automations are owned by a different part of the system and cannot be
    triggered from this screen. Those show **Run now** greyed out, with a note
    saying so. They still run on schedule normally.

## Turning one off

Switch it off here and it stops running but stays in the list, so you can turn it
back on without setting it up again. That is usually what you want — most "delete
this" impulses are really "stop this for now".

To remove one properly, say so in chat: *"delete the daily briefing automation"*.

## What automations produce

Most automations do not just run silently. They produce something:

- **An email to you** — briefings, digests, summaries.
- **A draft waiting for approval** — anything addressed to someone else. See
  [Reviewing its work](review.md).
- **Things it learned** — people, companies and facts that go into
  [Knowledge](../memory/wiki.md).

If an automation ran and you cannot find what it produced, check the approvals
queue first. Drafts waiting on you are the most common answer.

## Next

<div class="grid cards" markdown>

-   __Cookbook__

    ---

    Six recipes worth stealing.

    [:octicons-arrow-right-24: Cookbook](../cookbook/index.md)

-   __Reviewing its work__

    ---

    Approving or rejecting what it wants to send.

    [:octicons-arrow-right-24: Approvals](review.md)

</div>
