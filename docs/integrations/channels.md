---
description: Reaching your assistant from Telegram, Slack, WhatsApp and others.
---

# Channels

A channel is somewhere you can talk to your assistant that is not this screen.
Connect one and you get the same assistant — same conversations, same memory,
same automations — from your phone.

You will find them under **Settings → Channels**.

## What you can connect

| Channel | What it gives you |
|---|---|
| **Telegram** | Direct messages, groups, and topics. The simplest to set up. |
| **Slack** | Your workspace, with a list of who is allowed to use it. |
| **Microsoft Teams** | Teams chats. |
| **Discord** | Direct messages, channels and threads. |
| **WhatsApp** | Through a bundled bridge, using a QR code to log in. |
| **Signal** | Through a Signal bridge. |

## Reading the status

Each channel shows one of four states:

| State | Meaning |
|---|---|
| ✓ **connected** | Set up and running. You can message it now. |
| — **off** | Not turned on. Credentials may or may not exist. |
| ✗ **error** | Turned on, but something is wrong — usually an expired or wrong credential. |
| ? **unknown** | It could not determine the state. |

**error is the one to act on.** A channel in error is switched on and failing,
which usually means messages you send it go nowhere with no bounce. If a channel
you rely on has stopped answering, this screen is the first place to look.

## Connecting one

Each channel needs credentials from that platform — a bot token, an app
password, or a QR scan. The screen tells you exactly which values are needed for
the one you picked, with a link to where you get them, so you do not have to
guess what a "Slack app token" is.

Fill them in, save, and turn the channel on.

!!! note "It restarts to pick up the change"

    Channel settings are read when the assistant starts, so saving one restarts
    it. This takes a moment, and it finishes whatever it was in the middle of
    first. A conversation open in another window may pause briefly.

## Connecting is not the same as delivering

This is the one thing on this page worth reading twice.

Connecting a channel lets you **talk to** your assistant there. It does not, on
its own, tell it where to **send** anything. Those are two separate settings,
and the second one is easy to miss because nothing looks wrong when it is
missing: the channel shows as connected, the automation runs, the run is
recorded as successful — and no message arrives. Nothing is logged as an error,
because from the automation's point of view nothing went wrong.

So after connecting a channel, do this:

1. Open the channel and start a conversation with your assistant.
2. Go to the chat, group or channel you actually want its work delivered to.
3. Send `/sethome` there (in Slack: `/hermes sethome`).

That chat becomes the **home channel**: the default destination for anything
scheduled. You can change it later by sending the same command somewhere else.

!!! warning "Check it, do not assume it"

    **Setup → Where finished work goes** shows, per channel, the exact
    destination a scheduled job would resolve to, and offers a test message.
    Send one. A channel that has never delivered anything is not yet proof of
    anything, and this is the failure that is hardest to notice on your own —
    the first sign is usually a client wondering why they stopped hearing from
    you.

    If that page says a channel is *connected and discarding scheduled output*,
    it means the home channel was recorded but the setting the scheduler reads
    was not. Sending `/sethome` again fixes it.

## Which one to pick

**Telegram** if you just want it on your phone with the least setup.

**Slack or Teams** if you want colleagues to reach it too — both let you control
who is allowed.

**WhatsApp or Signal** if that is genuinely where you live. Both use a bridge,
which means an extra moving part that can need re-authenticating.

You can connect several at once.

## Why bother

An automation that delivers only to this screen is one you have to remember to
check, which is most of the work back again. A channel is what turns finished
work into something that reaches you.

The automations that benefit most are the time-sensitive ones. A
[meeting prep brief](../cookbook/meeting-prep.md) an hour before a
meeting is worth having; the same brief sitting in a browser tab you are not
looking at is not.

It also makes asking things frictionless. "What's on tomorrow?" from your phone
on the way home is the question you would never open a laptop for.
