---
description: What the license's all-caps disclaimer means in practice, and how to work with it.
---

# No warranty

Near the bottom of the [license](license.md) there is a paragraph in capital
letters. Almost nobody reads it. It is short, and here it matters more than
usual, so here is what it says in ordinary words.

!!! quote "THE SOFTWARE IS PROVIDED \"AS IS\"…"

    **Nobody promises this works.** Not that it is fit for your purpose, not
    that it is correct, not that it will keep running.

    **Nobody is liable if it goes wrong.** If it sends the wrong email to the
    wrong person, that is your problem, not the authors'.

This is standard for open-source software. It is worth pausing on here because
most software you accept those terms for does not read your mail and act on a
schedule while you are asleep.

## What actually goes wrong

Not crashes. Crashes are obvious and you notice them. The failures that cost you
something are quieter:

- **It misreads.** A sarcastic email read as a genuine complaint. A "let's not"
  read as a "let's".
- **It fills in gaps.** Asked for something it does not have, a model will often
  produce a confident, plausible, wrong answer rather than say it does not know.
- **It works from stale information.** It knows what it was told last week. The
  deal that fell through on Friday is still on in its head.
- **It quietly stops.** A connection expires and a briefing arrives thinner
  rather than not at all — which looks like a slow week, not a broken system.
  [Integrations](../integrations/index.md) is where you catch this one.

## What to do about it

**Keep approvals on for anything that leaves the building.** Reading, sorting
and summarising are low-risk — the worst case is a summary you disagree with.
Sending is different, because you cannot take it back. Approvals are the
practical answer to everything on this page.

[:octicons-arrow-right-24: Reviewing its work](../automations/review.md)

**Read the first few runs of anything new.** A recipe that is right four weeks
running has earned some trust. One that has never been checked has not.

**Judge the digest, not the individual item.** If a weekly summary reads like a
plausible week rather than *your* week, something upstream is broken and the
polished writing is hiding it.

!!! warning "The confident tone is not evidence"

    It writes the same way when it is sure and when it is guessing. Fluency is a
    property of the model, not a signal about accuracy — do not read a
    well-written answer as a checked one.

## The trade this is asking you to make

The license disclaims all responsibility. In exchange, the software is yours
outright: free, modifiable, running on hardware you chose, answerable to nobody,
with no vendor who can raise the price or shut it off.

That is a genuinely good deal. It just means **you are the last check**, and the
system is designed on that assumption rather than pretending otherwise.
