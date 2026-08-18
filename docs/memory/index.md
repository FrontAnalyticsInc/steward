---
description: The three different things your assistant remembers, and which one to look at.
---

# How it remembers

Your assistant remembers three separate things, and they behave differently. Most
confusion about "why doesn't it know that" comes from looking in the wrong one.

<div class="grid cards" markdown>

-   __What it knows about you__

    ---

    Who you are, what you are working on, how you like things done. Short, and
    it applies to every conversation.

    [:octicons-arrow-right-24: About you](about-you.md)

-   __Its character__

    ---

    How it behaves — tone, what it does on its own, what it refuses. Written by
    you, not learned.

    [:octicons-arrow-right-24: Character](character.md)

-   __What it has learned__

    ---

    People, companies and facts picked up from your mail and calendar. Large,
    and searchable.

    [:octicons-arrow-right-24: What it has learned](wiki.md)

</div>

## Which is which

| | Holds | Size | Who writes it |
|---|---|---|---|
| **About you** | Your identity, goals, preferences | Small — about a paragraph | It does, as it learns |
| **Character** | How it should behave | Medium | You do |
| **What it has learned** | People, companies, facts, when each was true | Large and growing | It does, from your mail and calendar |

The first two are always in front of it. What it has learned is not — it is
looked up when relevant, the way you would check a file rather than memorise it.

That distinction explains most surprises. Something in *About you* affects every
answer. Something in what it has learned only surfaces when the conversation
gets close enough to it.

## Where to look

Open the **Context** panel in the sidebar of any chat. It lists the memory files
and lets you read them.

There will normally be three:

- `SOUL.md` — [its character](character.md)
- `memories/USER.md` — [what it knows about you](about-you.md)
- `memories/MEMORY.md` — general things it has learned that are not about you

Click any of them to read the whole thing.

## You read here, you change it by talking

!!! warning "The Context panel is read-only"

    You cannot edit these files from this screen. That is deliberate — it is a
    window onto what the assistant is reading, not a text editor.

    To change what it remembers, **say so in chat**: *"remember that I prefer
    short replies"*, or *"that's wrong, I left Acme last year"*. It updates its
    own memory.

    [Its character](character.md) is the exception — that one is a file
    maintained by whoever runs the assistant.

## It has a memory limit

This is the part worth knowing.

What it knows about you is capped at roughly a paragraph, and general memory at
roughly two. When it hits the limit it does not stop remembering — it decides
what to drop.

So memory is a summary, not a log. If something matters, it is worth checking
that it actually landed rather than assuming it did:

> What do you know about me?

If it is not there, say it again more plainly. Things stated once in passing are
the ones that get dropped.

!!! note "It saves without asking"

    You will not be prompted before it writes something down. It saves as it
    goes, and you can read the result in the Context panel at any time.
