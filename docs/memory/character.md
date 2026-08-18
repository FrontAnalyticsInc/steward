---
description: The instructions that decide how your assistant behaves — its tone, judgement and limits.
---

# Its character

Where the assistant's personality and standing instructions live: how it talks,
what it does on its own, what it hands to you instead, and what it refuses.

This is not something it learned. It was written, and it can be rewritten.

## Reading it

Open the **Context** panel in the chat sidebar and click `SOUL.md`. It is the
first file in the list.

Read it if the assistant is behaving in a way you did not expect. The answer is
usually in there — a standing instruction doing exactly what it says, which is
not what you assumed.

## What it covers

- **Tone** — direct or warm, brief or thorough, how it handles uncertainty
- **Judgement** — what it decides alone versus what it brings to you
- **Limits** — what it will not do without asking
- **Standing rules** — things that must hold in every conversation, whether or
  not the subject comes up

## Character versus what it knows about you

Easy to confuse, and worth keeping straight:

| | |
|---|---|
| [**About you**](about-you.md) | Facts about *you*. It writes this. Changes as it learns. |
| **Character** | How *it* behaves. A person writes this. Changes when someone decides. |

"Alton prefers short replies" is about you. "Always draft, never send without
approval" is character.

The practical test: if it should still be true for a completely different user,
it is character.

## Changing it

Unlike your profile, this is not something you adjust by chatting. It is a file
maintained by whoever runs the assistant.

For most changes you do not need it. Preferences — tone, length, format — belong
in [what it knows about you](about-you.md), where you can change them by asking.

Reach for the character file when you want something that must hold no matter
what: a rule it cannot talk itself out of, or behaviour that should apply to
everyone using this assistant rather than just you.

!!! warning "A local edit can be overwritten"

    The character file is also kept in version control, and there is a command
    that refreshes it from there. If someone edits the running copy directly and
    someone else later runs that refresh, the local edit disappears — with the
    assistant simply going back to how it used to behave.

    It is no longer lost, though. Whoever runs this assistant can see exactly
    what changed and put it back; the file is committed on every upgrade for
    that reason. If its behaviour reverts for no apparent reason, this is the
    first thing to ask them about.

## When behaviour surprises you

1. **Read the character file.** Most surprises are a standing instruction you had
   not read.
2. **Check [what it knows about you](about-you.md).** A preference recorded
   loosely can have wider effects than you meant.
3. **Say so in chat.** *"Why did you do that?"* is a fair question and it will
   tell you which instruction it was following.

That third step is usually fastest, and it tells you which of the first two to
fix.

## Next

- [What it knows about you](about-you.md) — where preferences actually belong
- [How it remembers](index.md) — all three memories side by side
