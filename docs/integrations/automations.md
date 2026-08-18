---
description: What the scheduled jobs can reach, and why it is a shorter list than chat gets.
---

# For automations

What your [automations](../automations/index.md) can reach when they run. This is
a different, usually narrower list than the assistant gets
[in conversation](assistant.md).

## Reading it

On the **Integrations** screen, each connection lists who uses it. Automations
appear by name — the daily briefing, the inbox triage — alongside what each is
allowed to do.

That per-automation detail is the point. It answers "why did the briefing come
without my meetings" in one look: the briefing's calendar access is failing,
while everything else using the calendar is fine.

## Why it is narrower

Automations run when nobody is watching. That changes what they should be
trusted with.

A useful example: an automation may be allowed to **draft** an email and put it
in your queue, but not to **send** one. The same connection, a smaller
permission. If it misreads an instruction at 3am, the result is a bad draft
waiting for you rather than a bad email your client has already read.

This is the same reasoning behind [approvals](../automations/review.md), one
layer down. Approvals catch a bad draft; narrow permissions mean it could not
have sent it even without you.

## Reading a connection

Each one shows:

- **Who uses it** — which automations, and the assistant if it has access too
- **What each may do** — read, draft, send, modify
- **When each last worked** — and whether the last attempt failed

**Last worked is the column to check.** A connection that has not been used in a
while is either an automation that stopped running, or one running and failing
quietly. Both look like nothing happening.

## Common states

| State | Meaning | What to do |
|---|---|---|
| **working** | Used recently, succeeded | Nothing |
| **stale** | Has not been used when it should have been | Check **Last run** on the automation — it may not be running at all |
| **never** | Granted but never used | Normal for something set up recently; suspicious for something old |
| **failed** | Last attempt errored | Usually an expired credential. Needs whoever set it up |
| **unverified** | Granted, not confirmed working | Run the automation to find out |

!!! tip "stale is the one to look at"

    Failed announces itself. **stale** is the quiet failure — the automation
    stopped weeks ago and nothing went wrong loudly enough to notice. Nothing
    arrives, and nothing arriving looks exactly like a quiet week.

## When an automation is not producing

In order:

1. **[Last run](../automations/index.md)** — did it run at all? If not, the
   problem is the automation, not the connection.
2. **It ran, but nothing arrived** — check
   [approvals](../automations/review.md). Drafts waiting on you are the most
   common answer.
3. **It ran and produced something incomplete** — now check here. A briefing
   with no calendar in it is a calendar connection that failed.

## Next

- [In conversation](assistant.md) — the wider list chat gets
- [Reviewing its work](../automations/review.md) — where drafts wait
