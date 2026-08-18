---
description: Reviewing anything the assistant wants to do on your behalf — emails, filters, facts, tasks.
---

# Reviewing its work

Anything the assistant wants to do on your behalf waits here until you say yes.

It started as outbound email and that is still the bulk of it, but not all of it.
A proposed Gmail filter, a fact about to enter its memory, a task about to land
on someone's CRM record, a page or a post about to go out — each is a different
kind of decision, so each looks different here and offers different buttons.

This is the safety catch that makes the rest of it usable. You can leave an
automation drafting replies to your inbox every fifteen minutes precisely because
nothing it writes reaches anyone until you have read it.

## The queue

Every item says at the top what kind of thing it is, and carries its own link.
Copy the address of an item you want to come back to, or send it to someone.

Failures sit above everything else, in red, under **Needs attention** — something
that was supposed to have gone out and did not is more urgent than anything still
waiting to be decided.

## Deciding

**Reject** is the same for everything: it discards the item and asks you why.

What **approve** means depends on what you are looking at, and the buttons say so
rather than making you guess.

### Email

Two ways to approve, because they are genuinely different decisions:

- **Approve & create draft** puts the message in your Gmail Drafts, threaded
  under the message it replies to. You send it, from Gmail, whenever you like.
  Nothing has left the building yet.
- **Approve & send** sends it immediately, as you. There is no undo.

Create-draft is the safe default and the one bound to the `a` key. Send is on a
number key and asks a second time, deliberately: it should not be one keystroke
away from a list you are skimming.

Either way you can edit the text first — you are not limited to yes or no on the
exact words. Both actions produce identical mail, so choosing between them is
only ever a choice about who presses the final button.

### Gmail filters

**Approve & file the examples** applies the proposed label to the messages the
rule was derived from. It is reversible from Gmail and only touches mail already
in your inbox.

**Approve & record only** notes that the rule is good and changes nothing.

**Approve & create the filter** — a standing rule for future mail — needs a
permission this deployment has not been granted, so it is greyed out with the
reason on it. Nothing is silently half-done.

### Code

A worker that finishes a coding task it does not want to sign off on itself
blocks the task and asks for you. Those tasks appear here as **Code review**
items, with the files it changed attached in full — the question is "is this
right", and it cannot be answered from a summary, so the code is on the page
next to the buttons rather than somewhere you have to go and find it.

**Approve & mark done** completes the task. Whatever was waiting on it moves.

**Request changes** is the reject button, renamed because that is what it does
here: your reason becomes a comment on the task and the task goes back on the
board, where the worker picks it up and reads it. It is the one case where a
rejection is not the end of the item — so say what to change, not just no.

The task's own page links here whenever it is waiting on you, and until you
decide, nothing else happens to it.

### Everything else

A kind of item with no page of its own yet still shows up, with its own type
label, its details, and Approve / Reject. Approving records your decision; it
does not yet trigger anything automatically, and the button says so.

## Why the rejection reason matters

A rejection reason is not a form field — it is feedback. "Too formal" or "we
already replied to this last week" tells the assistant what went wrong, and the
automation that produced it gets better. Rejecting with no reason discards the
item and teaches it nothing, so you will see the same mistake tomorrow.

!!! tip "Reject the pattern, not just the item"

    If you find yourself rejecting the same kind of draft repeatedly, stop
    rejecting and go to [chat](../chat.md) instead: *"the outreach drafts are
    too formal, make them shorter and less salesy"*. One sentence there fixes
    every future draft; rejecting fixes one.

## When something fails

Approving is a request to do something, and things that reach other systems can
fail. A failed item moves to **Needs attention** with what went wrong.

Most failures offer **Retry**. Some do not, and that is deliberate: if a send
failed in a way that leaves it unclear whether the mail actually went, retrying
could send it twice. An email cannot be recalled, so an uncertain send is treated
as one that happened and left for you to check in Gmail. **Dismiss** takes it off
the list once you have.

## What gets counted

Everything the queue actually carries out is counted on the dashboard's
**Metrics** screen, under separate names — because the question worth asking is
not how much mail there was but how much of it anyone looked at:

| Counted as | What it means |
|---|---|
| `review_item` | Something was queued for you. Nothing has happened yet. |
| `draft_email` | A draft now exists in Gmail. Delivered to nobody; you still press send. |
| `approved_email` | Mail that left, because you approved it. |
| `auto_email` | Mail that left with nobody looking. |

The counts move only when the effect is real. Queuing a reply is a
`review_item`; it becomes a `draft_email` or an `approved_email` later, if you
approve it and the action succeeds. A send that failed is counted as no mail at
all, and a send whose outcome is unknown — the stalled case above — is counted
as nothing rather than guessed at, which is why the item is surfaced for you to
check in Gmail instead.

So a rising `approved_email` against a flat `auto_email` is the healthy shape:
things are going out, and a person is releasing each one.

The same screen draws two flow diagrams — what a batch of mail turned into, and
what became of the items you were asked about. **Still pending** is a real box on
the second one rather than a rounding error: an item queued and not yet decided
has to go somewhere, and seeing the backlog is usually the point.

## Nothing in the queue

Two possibilities, and they look identical:

1. Nothing needed reviewing. Normal.
2. An automation that should have produced something did not run.

If you expected something, check **Last run** on the
[Automations](index.md) screen. That distinguishes the two immediately.

## Letting something act without asking

You can tell it to, in chat: *"you don't need to ask me before sending the daily
briefing — that one's just to me"*.

This is reasonable for things addressed to you. Be much more careful about
anything going to other people.

!!! warning

    Review is the only thing standing between a misunderstood instruction and an
    email your client actually receives. That was always true; it is more true
    now that approving can send rather than merely file.

    Turn it off for briefings to yourself. Think hard before turning it off for
    anything else.
