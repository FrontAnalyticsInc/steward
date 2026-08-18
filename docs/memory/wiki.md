---
description: What your assistant has learned and remembered, and how to search it.
---

# Knowledge

As the assistant works — reading mail, running briefings, having conversations —
it remembers what it learns. People, companies, and how they relate to each
other. The **Memory** screen is where you look at that.

This is what makes it useful over months rather than minutes. It is the
difference between "who is this person emailing me" being answerable and not.

## It is a folder of notes

Memory is one file per person or company, written in plain Markdown. The
**Memory** screen shows you those files. Nothing is hidden behind a database:
what you read on the screen is what is stored on disk, and if you opened the
folder yourself you would see the same thing.

That has a practical consequence worth knowing. If a fact is wrong, it is wrong
in a file you can open and fix — with a text editor, or by asking the assistant
to correct it. There is no re-indexing step and nothing to migrate.

## Two ways to find something

**Documents** lists everyone on file, most recently updated first. Use it when
you want to see what the assistant has been learning lately, or when you know
who you are looking for.

**Search** looks inside the notes and returns matching statements. *"What does
it know about claims forecasting?"* returns the things it has recorded, with the
person or company each came from.

A search that matches nothing returns nothing. That sounds obvious, but it is
the main improvement over what this replaced: the old system always returned its
closest guesses, so asking about someone it had never heard of produced
confident-looking facts about a different person entirely.

## What it remembers

Three kinds of thing:

- **People and companies** — one note each.
- **Facts about them** — who works where, what was discussed, what was agreed.
- **Where each came from** — the refresh or conversation that produced it,
  written beside the facts it produced.

That last one matters. Every entry is traceable back to what produced it, so
when something looks wrong you can see where it came from instead of guessing.

## It remembers *when*, not just *what*

Every entry is dated, and new ones are added rather than overwriting old ones.
So a note reads as a history: what was true in March is still there under its
March heading when the June entry disagrees with it.

This is why the assistant can tell you something has changed, instead of only
ever telling you the latest thing it heard.

It is also how it decides when to look again. If everything it knows about
someone is six months old, it goes and refreshes before writing you a briefing
about them.

## How things connect

A note can point at another with a link, written `[[Like This]]`. When the
assistant records that someone works at a company, the company name becomes a
link to that company's own note.

Open any note and you will see both directions:

- **Links out** — what this note points at.
- **Links in** — everything that points here.

Open a company and *Links in* is everyone it knows who works there. That is the
question the old relationship diagram existed to answer, and it is now a list
you can click.

## What it does not do

It does not merge people for you. Two notes for the same person with different
addresses stay two notes until someone says otherwise. The old system guessed at
this automatically and sometimes guessed wrong, quietly filing one person's
history under another's name — which is a worse failure than a duplicate you can
see.
