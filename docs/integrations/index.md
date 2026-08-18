---
description: What your assistant is connected to — how you reach it, and what it can reach.
---

# Integrations

Three different kinds of connection, and it is worth knowing which is which when
something stops working.

<div class="grid cards" markdown>

-   __Channels__

    ---

    How *you* reach *it*. Telegram, Slack, WhatsApp and others — the same
    assistant, from your phone.

    [:octicons-arrow-right-24: Channels](channels.md)

-   __What the assistant can use__

    ---

    What *it* can reach when you are talking to it. Your mail, calendar,
    contacts.

    [:octicons-arrow-right-24: In conversation](assistant.md)

-   __What automations can use__

    ---

    What the scheduled jobs can reach. Not the same list, and that is
    deliberate.

    [:octicons-arrow-right-24: For automations](automations.md)

</div>

## The difference that matters

**Channels are the way in.** They carry your words to the assistant and its
answers back. A broken channel means you cannot reach it.

**The other two are the ways out.** They are what it can touch on your behalf. A
broken one of those means it answers, but with less than it should — and it may
not be obvious that anything is missing.

That second failure is the quiet one. A briefing that arrives with no calendar
in it looks like a thin briefing, not a broken connection.

## Same connection, different users

A single connection — your mail, say — can be used by several things: by the
assistant while you chat, and by each automation that needs it.

**Those are granted separately and they fail separately.** The assistant can
still read your mail in conversation while the automation that triages it every
15 minutes has a credential that expired last week.

This is why the Integrations screen lists *who uses what* rather than just what
is connected. A connection that reads "fine" because most of its users are fine
would be misleading.

!!! note "A source shows its worst status"

    When one connection is used by five things and one of them is broken, the
    connection is shown as broken. Open it to see which user is actually
    failing — the others are probably fine.

## Reading the screen

You will find it from the **Automations** header. Connections are grouped by
what they connect to — mail, calendar, contacts — not by how they connect.

Each shows who uses it, what each user is allowed to do, and when each last
worked.

!!! warning "It is a status report, not a control panel"

    You cannot connect, re-authorise, or remove anything from this screen, by
    design. Setting up connections happens elsewhere.

    The reasoning: a screen with a "reconnect" button on it is a screen you are
    tempted to fix things from, and a thing you fix from is not a thing you can
    trust to tell you the truth about what is broken. This one only reports.

    [Channels](channels.md) are the exception — those you do set up here, under
    **Settings → Channels**.

## When something looks wrong

1. **Is it a channel or a connection?** Cannot reach the assistant at all →
   channel. It answers but the answer is thin → connection.
2. **Check who is failing.** One automation, or everything using that source?
3. **Check [Last run](../automations/index.md)** on the automation. A connection
   that broke a week ago usually shows up as an automation that quietly stopped
   producing anything.
