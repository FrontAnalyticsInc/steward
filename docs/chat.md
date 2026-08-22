---
description: Talking to your assistant — asking things, and asking it to do things regularly.
---

# Chatting

Chat is the main way you use the assistant. It is also how you create
automations — there is no form to fill in. You describe what you want in
ordinary words and it sets it up.

## Asking it something

Type and press enter. It answers in the conversation, and shows what it is doing
while it works — searching your mail, checking the calendar, looking something
up. You can watch that happen; it is not hidden.

Useful things to ask:

> What's on my calendar tomorrow?

> Who is Sarah Chen and when did we last speak?

> Find the email where we agreed the pricing with Northwind.

> Summarise everything you know about the Acme account.

It answers from what it can actually reach. If it has not been connected to
something, it tells you rather than inventing an answer.

!!! tip "Ask follow-ups"

    It remembers the conversation you are in. After it answers, "and the week
    after?" or "draft a reply to that" works — you do not need to repeat the
    context.

## Conversations

The left-hand list holds your conversations. Each keeps its own thread and its
own memory of what you were discussing.

Start a new one when you change subject. Keeping a single endless thread makes
answers worse, because it drags everything you talked about earlier into every
new question.

Old conversations can be archived from the list when you are done with them.

## Asking it to do something regularly

This is the part worth learning. Anything you can ask it to do once, you can ask
it to do on a schedule — just say when.

> Every morning at 8, email me what's on my calendar for the day.

> Check my inbox every 10 minutes and draft replies to anything that needs one.

> Every Monday, tell me who I haven't replied to in the last month.

It will confirm what it is about to set up — what it will do, and when. Read that
back before agreeing, particularly the **when**: "every morning" and "every
hour" are a very different amount of mail.

Once you agree, it appears on the [Automations](automations/index.md) screen and
runs whether or not you are here.

### The two kinds, and why it tells you which

There are two ways it can build an automation, and it will say which one it has
picked and why, in a sentence, when it sets it up.

- A **prompt cron** is the assistant doing the job on a schedule, in the same
  way it would if you asked it now. Nothing to build — it exists the moment you
  agree. This is the right shape for reading and summarising: a morning
  briefing, a weekly digest, a scan of a source you care about. What comes out
  is written fresh each time, so it varies a little run to run, the way its
  answers in chat do.
- A **pipeline** is a piece of tested code that runs the job the same way every
  time. It takes longer to set up, because it has to be built and checked before
  it runs unattended. (The Automations screen labels these *ADK pipeline* — ADK
  is the toolkit they are built with.) This is the shape it will choose when
  the job involves judgement over content from outside — emails, web pages, anything
  someone else wrote — or when the result *does* something: sends, files,
  updates a record, spends money.

The short version: **if it only reads and tells you, it is a prompt cron. If it
acts, or if it has to weigh up something an outsider wrote, it is a pipeline.**

You do not have to choose. Ask for what you want in plain words and it picks,
then tells you. If you disagree — "no, just summarise it, do not reply to
anything" — say so, and it will pick again; that usually changes the job into
one the cheaper kind can do.

The [Automations](automations/index.md) screen labels each one, so you can see
at a glance which kind you have.

!!! warning "Say if it should send, or only draft"

    By default, things that leave — emails, messages — are drafted and wait for
    your approval. If you want an automation to send without asking, you have to
    say so explicitly. If you want to be certain it never sends, say "draft
    only, don't send" when you set it up.

    See [Reviewing its work](automations/review.md).

## Changing or stopping one

You do not need to find a settings page. Say it in chat:

> Stop the daily calendar briefing.

> Change the inbox check to every 30 minutes instead of 10.

> The morning briefing is too long — just the meetings, not the CRM notes.

You can also switch any automation off directly on the
[Automations](automations/index.md) screen, which is faster when you just want
it to stop.

## When it gets it wrong

Tell it. "That's not what I meant — I wanted X" works, and it will adjust rather
than starting over.

If an automation has been producing bad output, say so and describe what you
wanted instead. It rewrites the instruction rather than making you delete it and
start again.

## Next

<div class="grid cards" markdown>

-   __Cookbook__

    ---

    Six recipes worth stealing, with the exact wording for each.

    [:octicons-arrow-right-24: Cookbook](cookbook/index.md)

-   __Automations__

    ---

    Seeing what is running and what it did.

    [:octicons-arrow-right-24: Automations](automations/index.md)

</div>
