---
description: What your assistant can reach while you are talking to it.
---

# In conversation

What the assistant can actually touch when you ask it something. If it cannot
reach your calendar, "what's on tomorrow?" has no answer — and this is where you
find that out.

## Reading it

Open the **Connections** panel in the chat sidebar. It lists what *this
conversation* can use, and how many.

This is the fast check. When a reply comes back thinner than you expected, the
answer is often a broken connection rather than a bad question — and a failure
shows on the panel even while it is collapsed, for exactly that reason.

## What is usually here

| Connection | What it lets it do in chat |
|---|---|
| **Mail** | Search your mail, read a message, write a draft, send |
| **Contacts / CRM** | Look someone up, check the history on an account |
| **What it has learned** | Search what it has learned — people, companies, facts |

Yours may differ. The panel is the authority; this table is what a typical setup
looks like.

## Chat can do things automations cannot

The lists are deliberately different.

Conversation is the seat where a person is present. It gets the broader
permissions — reading widely, and sending when you ask directly — because you are
there to see what happens.

[Automations](automations.md) run unattended, so they get narrower ones. An
automation that can *draft* mail but not *send* it cannot email a client at 3am
because it misread an instruction.

!!! note

    So "it did that when I asked, why won't the automation do it?" usually has a
    real answer: the automation was not granted that. It is a deliberate limit,
    not a fault.

## When it cannot do something

It tells you. *"I don't have access to your calendar"* is a real answer, and a
more useful one than a guess.

If you get that and expected otherwise:

1. **Open Connections** in the sidebar — is it listed at all?
2. **If listed but marked failed**, the credential has probably expired. That
   needs whoever set it up.
3. **If not listed**, it was never connected for conversation. It may still exist
   for [automations](automations.md) — the two lists differ.

## The full picture

The Connections panel shows only what *this conversation* can use. The
**Integrations** screen shows everything, including connections only automations
hold.

Same underlying information, read from the other end. Use the sidebar for "why
was that answer thin", and the full screen for "what is connected overall".

## Next

- [What automations can use](automations.md) — the other half
- [Channels](channels.md) — reaching the assistant from your phone
