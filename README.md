# Steward

An AI agent that builds automations for your business, runs them on a schedule,
and repairs them when they break — on a server you own.

<https://frontanalytics.com/steward/>

## Status

**Pre-release. `install.sh` is a placeholder and installs nothing.**

```
curl -fsSL https://raw.githubusercontent.com/FrontAnalyticsInc/steward/main/install.sh | bash
```

Running that prints a short notice and exits. It takes no action at all: no
packages, no containers, no files, no configuration. It exists so the address
on the landing page resolves to something real and harmless while the actual
bootstrap is finished.

That is deliberate. People are being invited to pipe this into a shell, and a
placeholder that half-configures a stranger's server would be far worse than
one that prints a paragraph and stops. Note also that it does not ask for
`sudo` — nothing it does needs root, and asking for root you do not need is
how people learn to hand it over without looking.

Two things stand between here and a real installer:

1. **The script itself.** The working deploy path today is Terraform plus
   cloud-init, held privately in `hermes-infra`. Turning that into a single
   bootstrap a stranger can run on a fresh machine is real work, not a
   copy-paste.
2. **A decision about what becomes public.** The installer has to reference
   images, a compose file, and default configuration. Publishing those is a
   deliberate choice about how much of the stack is open, and it should be made
   on purpose rather than as a side effect of shipping a script.

## What it does

- **Describe** an automation in a sentence, and it builds it.
- **Schedules** it, runs it, and watches whether it produced anything.
- **Repairs** it when a page moves, a field is renamed, or a prompt drifts.
- **Asks** you when it cannot fix something itself — a lapsed credential needs a
  person, and it says so once rather than failing quietly forever.

Everything runs on hardware you control. Your credentials and your mail stay on
that machine; there is no account with us and no service in the middle.

## What you will need

- A Linux server of its own — not your laptop, and not a machine that sleeps.
- An Anthropic or OpenAI key, with a spend cap set at the provider.
- About $35 a month for the server, if you do not already have one. That figure
  is estimated from list prices and has not yet been measured over a full month.

## Licence

MIT — see [LICENSE](LICENSE).

The stack includes a *modified* build of the Hermes agent gateway,
MIT © 2025 Nous Research; that notice and licence travel with the image.
