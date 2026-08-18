---
description: These docs say "the server" a lot. What that machine actually is, and why it changes the answers.
---

# Where it runs

Hermes runs on one computer. These pages call it **the server**, and everything
they say about your data staying put means *staying put on that computer*.

Which computer that is changes the answer to "is my mail private" quite a lot,
and only you know which one you have.

## Three ways people run this

<div class="grid cards" markdown>

-   __Your own computer__

    ---

    Installed on the laptop or desktop you sit at.

    The data is as private as anything else on that machine. It also only works
    while the machine is awake and online — an overnight briefing needs the
    computer to still be running at 6am.

-   __A machine you own__

    ---

    A spare box, a home server, something in the office cupboard.

    Same privacy story: it is your hardware, in your building. It stays on, so
    scheduled work actually runs.

-   __A rented cloud server__

    ---

    A virtual machine from a hosting company, billed monthly.

    Always on and reachable from anywhere. But the disk your mail sits on is
    in someone else's datacentre — see below.

</div>

## What changes on rented hardware

This is the case worth being clear about, because the rest of these docs say
things like "it stays on the server" and that sentence sounds more reassuring
than it is when the server belongs to a hosting company.

**The disk is not yours.** Your messages, what it has learned and everything it
has learned about you sit on storage owned and physically controlled by your
hosting provider. Their staff can, in principle, access it. They will hand it
over if compelled to by a court in their jurisdiction. Their backups may keep
copies after you delete things.

**It is on the public internet.** A cloud server has a public address that
anyone can try to connect to, which a laptop behind a home router does not.

None of this makes cloud hosting a bad choice — it is the only practical option
if you want automations running at 6am without leaving a computer on at home.
It just means "self-hosted" and "private" are two different claims, and on
rented hardware you get the first one, not automatically the second.

!!! tip "Which one do I have?"

    If you type an address like `192.168.x.x` or a `.local` name to reach the
    console, it is on your own network. If you type a public address or a
    domain name and it works from a coffee shop without a VPN, it is on a
    server that the internet can also reach.

## If it is on a public server, close the doors

Hermes is built for a trusted network — a machine you or your household can
reach and nobody else. Several parts of it are open by design on that
assumption:

| What | Why it matters |
| --- | --- |
| The console | No password. Anyone who opens it can chat as you and read everything. |
| What it has learned | Stored as files the console reads. Anyone who can open the console can read every note. |
| The headless browser | Protected by a token — but the token has a default too, and an unprotected one will fetch any address it is given. |

On your own network that is a reasonable trade — it is your network. On a public
address it means anyone who finds the machine has your mailbox.

!!! note "The browser is worth a second look"

    Web-reading recipes drive a real browser, and a browser that anyone can
    send instructions to is a browser that will fetch things on their behalf —
    including addresses inside your own network that are not otherwise
    reachable from outside.

    It ships with a placeholder token. Changing it is the single highest-value
    thing on this page if you have not done it.

!!! danger "Do not put this on a public IP without a firewall"

    If you are running on rented hardware, the machine should accept
    connections only from you. In practice that means one of:

    - a firewall allowing only your own address
    - a VPN, so the server is on your private network
    - an SSH tunnel, connecting from your laptop when you need it

    Whoever set the server up will know which. If nobody did any of these, it
    is worth asking today rather than at the weekend.

## What does not change

Wherever it runs, the parts that were true stay true: nothing is reported back
to whoever wrote the software, there is no account you can be locked out of,
and no company can switch off your copy. Moving to rented hardware changes who
can physically reach the disk, not who controls the software.

## Related

- [Where your data goes](your-data.md) — what leaves the server, wherever it is
- [No warranty](no-warranty.md) — what the license does not promise
