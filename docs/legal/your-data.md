---
description: What stays on the server, what gets sent elsewhere, and how to find out which is which.
---

# Where your data goes

Your assistant reads your mail, your calendar and your contacts. It is
reasonable to want to know what happens to all of that.

The honest answer has two halves, and people usually get them backwards.

!!! abstract "The short version"

    **What is stored stays on the server.** Your messages, the knowledge
    graph, everything it remembers — all of it sits on the one computer Hermes
    is installed on.

    **What does the thinking usually does not.** The model that reads and
    writes for you is, by default, a hosted service. Text from your mail is
    sent to it to be understood.

Both halves are true at once. "It runs on my own server" is a fair description
of where your data *lives* and a misleading one about where it is *processed*.

!!! warning "\"The server\" might not be a machine you own"

    This page says *the server* rather than *your machine* on purpose. Hermes
    might be installed on your laptop, or on a virtual machine you rent from a
    hosting company — and "it stays on the server" means something weaker in
    the second case, because the disk belongs to somebody else.

    [:octicons-arrow-right-24: Where it runs](deployment.md)

## What stays there

| | Where it lives |
| --- | --- |
| Your conversations with it | On the server's disk |
| What it has learned — people, companies, what happened when | A database on the server |
| What it remembers about you | A file on the server |
| Records of what automations did | On the server |

None of this is uploaded anywhere else. There is no account, no sync, and no
company holding a copy on your behalf. If you turn the server off, it is off.

## What gets sent out

**The model.** An assistant needs something to do the reading and writing, and
that something is a large language model. Out of the box Hermes is configured to
use a hosted one, which means the text it is working on — the body of an email,
your calendar entries, whatever you typed — is sent to that provider to be
processed.

**Building what it has learned.** Working out that an email mentions a person, a
company and a date is itself a model task. By default that also runs on the
hosted model, which means mail content passes through it on the way into the
graph.

**Your channels.** If you talk to your assistant over Telegram, Slack or
WhatsApp, your messages cross that company's servers first. That is true of
every message you send on those apps, but it is easy to forget it applies here
too. Talking to it through the web console instead avoids this.

## It can be made fully self-contained

Nothing above is a fixed property of Hermes — it is configuration. The software
can run against a model on the server itself instead of a hosted one, and parts
of a normal install already do.

That trades away quality: a model small enough to run on one ordinary machine is
noticeably less capable than a hosted one at reading a messy email thread and
working out what matters. Whether that trade is worth it depends on what is in
your mailbox.

Note what this does and does not buy you. It stops your mail being sent to a
model provider. It does **not** make the data private if the server itself is
rented — the disk is still in somebody else's building. Those are two separate
questions and it is easy to think you have solved both by solving one.

!!! tip "How to find out what yours is doing"

    This is set up once, when the system is installed, and is not exposed on any
    screen. Whoever installed it will know — and if that was you, it is the
    model provider settings in the Hermes configuration.

    The useful question to ask is not "is it local?" but **"which provider, and
    is it the same one for what it has learned?"** Those two are configured
    separately and are easy to set differently by accident.

## What nobody can see

Whatever the model provider is, **Hermes itself sends nothing home.** There is
no telemetry, no usage reporting, and no channel back to whoever wrote it. The
only outbound traffic is the work you asked for: the model, the services you
connected, and the websites an automation was told to read.

That is a statement about what Hermes *sends*. It says nothing about who can
reach the server and read the data at rest, which depends entirely on where you
put it.

[:octicons-arrow-right-24: Where it runs](deployment.md)

## Related

- [What it knows about you](../memory/about-you.md) — reading and editing what
  it has stored
- [What it has learned](../memory/wiki.md) — what ends up in there
- [Integrations](../integrations/index.md) — what it is connected to
