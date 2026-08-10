# AI Steward

An AI agent that builds automations for your business, runs them on a schedule,
and repairs them when they break — on a server you own.

<https://frontanalytics.com/steward/>

## Status

**Pre-release. There is no installer here yet.**

The landing page describes a one-command install:

```
curl -fsSL https://steward.build/install.sh | sudo bash
```

That command does not work today, and this repository is where it will work
from once it does. Two things are outstanding:

1. **The script itself.** The working deploy path today is Terraform plus
   cloud-init, held privately in `hermes-infra`. Turning that into a single
   bootstrap a stranger can run on a fresh machine is real work, not a
   copy-paste.
2. **A decision about what becomes public.** The installer has to reference
   images, a compose file, and default configuration. Publishing those is a
   deliberate choice about how much of the stack is open, and it should be made
   on purpose rather than as a side effect of shipping a script.

Until both are settled, nothing here should be piped into `sudo bash`. An
installer that half-works on someone else's server is worse than one that does
not exist.

## What it does

- **Describe** an automation in a sentence, and it builds it.
- **Schedules** it, runs it, and watches whether it produced anything.
- **Repairs** it when a page moves, a field is renamed, or a prompt drifts.
- **Asks** you when it cannot fix something itself — a lapsed credential needs a
  person, and it says so once rather than failing quietly forever.

Everything runs on hardware you control. Your credentials and your mail stay on
that machine; there is no account with us and no service in the middle.

## Licence

MIT — see [LICENSE](LICENSE).

The stack includes a *modified* build of the Hermes agent gateway,
MIT © 2025 Nous Research; that notice and licence travel with the image.
