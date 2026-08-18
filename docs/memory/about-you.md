---
description: The short profile your assistant keeps about who you are and what you are working on.
---

# What it knows about you

A short profile the assistant keeps about you: who you are, what you are trying
to do, and how you like things done. It reads this before every reply, in every
conversation.

This is the highest-leverage memory in the system. It is small, so everything in
it counts.

## Reading it

Open the **Context** panel in the chat sidebar and click `memories/USER.md`.

Or just ask:

> What do you know about me?

The file is a list of short statements, each one thing it has concluded — your
role, your company, what you are working towards, preferences you have expressed.

## What belongs in it

Things that are true across conversations and change slowly:

- **Who you are** — role, company, how to reach you
- **What you are working towards** — the handful of goals that shape what is
  relevant
- **How you like things done** — short replies, no preamble, always draft rather
  than send
- **Standing context** — the projects you are in the middle of

What does *not* belong: anything about one specific conversation, or facts about
other people. Those go to the [what it has learned](wiki.md).

## Changing it

Say it in chat. There is no form.

> Remember that I prefer short answers with no preamble.

> I've moved — I'm at Northwind now, not Acme.

> Stop assuming I want a summary at the end of every reply.

Corrections work as well as additions. *"That's wrong"* followed by the right
version is understood, and it replaces rather than piling a contradiction on top.

## Because it is small, it drops things

!!! warning

    This profile is capped at roughly a paragraph. When it fills up, the
    assistant decides what to keep — and a preference you mentioned once in
    passing loses to something you have said three times.

    If something matters, check it stuck:

    > What do you know about me?

    If it is missing, say it again, plainly and on its own. "By the way, I hate
    bullet points" buried in a long message is exactly the kind of thing that
    gets summarised away.

## Why it matters more than it looks

Because it is read before every reply, one line here changes every future
answer — which is far more effective than correcting the same thing repeatedly
in conversation.

If you find yourself giving the same instruction a third time, stop correcting
and say *"remember this"*. That is the difference between an assistant that
adapts and one you have to keep re-training.

## Next

- [Its character](character.md) — the difference between what it knows about you
  and how it behaves
- [What it has learned](wiki.md) — where facts about *other* people live
