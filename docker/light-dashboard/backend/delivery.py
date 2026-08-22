"""Whether finished work can actually reach anyone, and proof that it did.

The failure this module exists to catch
---------------------------------------

A scheduled job with ``deliver: telegram`` and no home channel is dropped
silently. Not logged as an error, not surfaced, not retried — the job runs,
succeeds, records a successful execution, and nobody receives anything.

The mechanism is ``cron.scheduler._resolve_single_delivery_target``. For a bare
platform name it does exactly this::

    chat_id = _get_home_target_chat_id(platform_name)   # os.getenv(...)
    if not chat_id:
        return None                                     # <- output dropped

and ``_get_home_target_chat_id`` reads **one env var**:
``TELEGRAM_HOME_CHANNEL`` / ``SLACK_HOME_CHANNEL`` (``_HOME_TARGET_ENV_VARS``).

That single fact drives everything below, because it disagrees with every other
indicator on the box:

* Hermes's platform catalog reports ``configured: true`` from the *token*
  alone. A box with a valid token and no destination is green there.
* Hermes's ``home_channel`` field comes from ``config.yaml``, which is where
  ``/sethome`` persists a ``HomeChannel``. Cron does not read config.yaml.
* The gateway's own "you have not set a home channel" nudge (gateway/run.py)
  accepts ``config.get_home_channel()`` — i.e. config.yaml — as sufficient and
  stops nagging. Cron still drops.
* ``POST /api/messaging/platforms/<id>/test`` re-reads the same config and
  never sends anything, so it cannot notice either. (It is also useless from
  the hermes-dashboard container, where the gateway process is invisible and
  its ``gateway_running`` is permanently false — the blind spot recorded in
  :mod:`channels`.)

``/sethome`` normally writes both — config.yaml *and* the env var — but the env
write is best-effort and only logs a warning when it fails, so the two can and
do diverge. A config.yaml restored from backup without its .env diverges. A
home channel set by hand in config.yaml diverges.

So this module treats ``<PLATFORM>_HOME_CHANNEL`` in ``$HERMES_HOME/.env`` as
the authority on whether unattended output has anywhere to go, reports a
config.yaml home channel that is not backed by it as the trap it is, and proves
the rest by sending a message down the same address a cron job would use.

Four states, and the two in the middle are the point
----------------------------------------------------

``no_credential``   no token. Nothing can be sent.
``no_destination``  token, no ``<P>_HOME_CHANNEL``. Scheduled output is being
                    dropped, and every other indicator on the box says fine.
``unproven``        token and destination, and nothing has ever been delivered
                    with them. Plumbing, not water.
``verified``        a message was handed to Telegram/Slack at that exact
                    address and the platform acknowledged it.

Why an unauthenticated console may hold a send button
------------------------------------------------------

This console has no login, so every capability it gains, anything reaching the
tailnet gains. Three properties keep this one small:

* **The body is fixed.** The caller supplies no text, so the console cannot be
  used to put chosen words into a client's Slack.
* **The target is fixed.** It is the configured home channel, never a chat id
  from the request, so the console cannot be used to reach anything else.
* **The token never leaves this process.** It is read from ``$HERMES_HOME/.env``
  at send time and appears in no response, no stored record and no log line.
  That file is on a volume this container already mounts read-write; reading
  one key from it grants nothing it did not already have.

A cooldown bounds the remaining nuisance to one message per channel per minute.

This module writes no configuration. The only file it creates is its own record
of what was delivered, which is evidence, not settings. It never restarts
anything: a test send talks to the platform directly and so does not need the
gateway to have picked the credential up.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from . import channels as _channels
from .hermes_api import HermesUnavailable

# The two day-one channels, in the order the setup page shows them. Telegram
# first, deliberately: a BotFather token takes two minutes and needs no
# administrator, where a Slack app costs a workspace approval. The four other
# channels :mod:`channels` exposes are reachable from Settings and are not
# first-run material — WhatsApp and Signal each need a bridge, Teams and
# Discord an app registration.
CHANNEL_IDS = ("telegram", "slack")

# Mirrors cron.scheduler._HOME_TARGET_ENV_VARS for these two platforms. Copied
# rather than imported because that module is not in this container, and named
# here rather than derived from the platform id because the mapping is not
# uniform upstream (matrix is MATRIX_HOME_ROOM, email is EMAIL_HOME_ADDRESS).
# If upstream renames one of these, the row goes to `no_destination` on a box
# that delivers fine — wrong, but wrong in the direction that makes someone
# look, which is the correct way for this particular check to fail.
HOME_ENV = {
    "telegram": "TELEGRAM_HOME_CHANNEL",
    "slack": "SLACK_HOME_CHANNEL",
}

SETUP: Dict[str, Dict[str, Any]] = {
    "telegram": {
        "name": "Telegram",
        "required_env": ("TELEGRAM_BOT_TOKEN",),
        "cost": (
            "Two minutes, no administrator: message @BotFather, send /newbot, "
            "and it hands you a token."
        ),
        "docs_url": "https://core.telegram.org/bots/features#botfather",
        "sethome": "/sethome",
    },
    "slack": {
        "name": "Slack",
        "required_env": ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
        "cost": (
            "One workspace approval: create an app at api.slack.com/apps, turn "
            "on Socket Mode, install it to the workspace."
        ),
        "docs_url": "https://api.slack.com/apps",
        # Slack routes every Hermes command through one parent slash command,
        # so bare /sethome is not registered there and answers "app did not
        # respond" (gateway/run.py says the same).
        "sethome": "/hermes sethome",
    },
}

# Where the last delivery is remembered. A dot-file beside the other
# dashboard-owned marker in HERMES_HOME (UNREAD_BASELINE_MARKER in main.py).
# Deliberately not configuration: deleting it loses the evidence and costs one
# more click, and changes nothing about how the box behaves.
CHECKS_FILE = ".dashboard_channel_checks.json"

# One test message per channel per minute. Not a security control — the fixed
# body and fixed target are that — only a bound on how much noise an idle
# stranger can make in someone's Slack.
COOLDOWN_SECONDS = 60

TEST_MESSAGE = (
    "Steward test message. If you can read this, finished work has somewhere "
    "to go: this is the exact address scheduled automations deliver to."
)

# Long enough for a slow platform API, short enough that a page waiting on it
# does not look hung. Both platforms normally answer well inside a second.
SEND_TIMEOUT = 15.0


# --- reading what is on disk ------------------------------------------------


def env_path(data_dir: str) -> str:
    """$HERMES_HOME/.env — the file Hermes's own ``get_env_path()`` returns.

    Not the stack's compose .env. Two different files with the same name: the
    compose one carries ANTHROPIC_API_KEY and is read by docker, this one
    carries channel credentials and is read by the gateway at start. Telling an
    operator to put a bot token in the wrong one produces a box that looks
    configured from the shell and is not.
    """
    return os.path.join(data_dir, ".env")


def host_env_path(data_dir: str) -> str:
    """The same file, named as it exists on the machine the operator is on.

    Every container here mounts the data directory at /opt/data, so that is
    the only path this process can see — and it is the wrong thing to print in
    an instruction, because it does not exist on the host. compose passes the
    mount source as STEWARD_DATA_DIR_HOST for this one purpose.

    Falls back to the container path rather than to a guess: a path that is
    merely unhelpful is better than one that is confidently wrong, and an
    install predating that compose variable is exactly where a guess would be
    wrong.
    """
    host_dir = os.environ.get("STEWARD_DATA_DIR_HOST", "").strip()
    return os.path.join(host_dir or data_dir, ".env")


def read_env(data_dir: str, keys: Tuple[str, ...]) -> Dict[str, str]:
    """The requested keys from $HERMES_HOME/.env, or {} if it cannot be read.

    A deliberately small parser, only ever asked for keys this module names,
    and it exports nothing into os.environ: a value read here lives in one
    local for the length of one request.
    """
    wanted = set(keys)
    found: Dict[str, str] = {}
    try:
        with open(env_path(data_dir), "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, _, value = line.partition("=")
                key = key.strip()
                if key not in wanted:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                found[key] = value
    except OSError:
        return {}
    return found


def cron_destination(data_dir: str, channel_id: str) -> Optional[dict]:
    """Where a bare ``deliver: <channel>`` would actually land, or None.

    Resolved the way cron.scheduler resolves it, from the same env vars, so
    that "this page says it will deliver" and "it delivers" cannot come apart:

      * ``<P>_HOME_CHANNEL``            the chat id, and the whole question
      * ``TELEGRAM_CRON_THREAD_ID``     Telegram only, and it WINS over the
                                        home-channel thread id for cron —
                                        _get_home_target_thread_id checks it
                                        first, because a delivery into a topic
                                        -mode root DM lands in the lobby the
                                        user cannot reply in (#24409)
      * ``<P>_HOME_CHANNEL_THREAD_ID``  otherwise

    Read from the .env on disk rather than this process's environment: this
    container is handed no channel configuration, so os.getenv would report
    every box as undeliverable.
    """
    home_key = HOME_ENV[channel_id]
    keys = [home_key, f"{home_key}_THREAD_ID"]
    if channel_id == "telegram":
        keys.append("TELEGRAM_CRON_THREAD_ID")
    env = read_env(data_dir, tuple(keys))
    chat_id = (env.get(home_key) or "").strip()
    if not chat_id:
        return None
    thread_id = ""
    if channel_id == "telegram":
        thread_id = (env.get("TELEGRAM_CRON_THREAD_ID") or "").strip()
    if not thread_id:
        thread_id = (env.get(f"{home_key}_THREAD_ID") or "").strip()
    return {"env_var": home_key, "chat_id": chat_id, "thread_id": thread_id or None}


# --- the record of what was actually delivered ------------------------------


def load_checks(data_dir: str) -> Dict[str, dict]:
    try:
        with open(os.path.join(data_dir, CHECKS_FILE), "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return {}
    seen = payload.get("channels")
    return seen if isinstance(seen, dict) else {}


def save_check(data_dir: str, channel_id: str, record: dict) -> None:
    """Remember one test-send outcome. Best effort — evidence, not state.

    A plain read-modify-write: this file has one writer, and losing it costs a
    click, so the atomic-rename dance a config writer needs would be ceremony.
    """
    existing = load_checks(data_dir)
    existing[channel_id] = record
    try:
        with open(os.path.join(data_dir, CHECKS_FILE), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "channels": existing}, fh, indent=2)
    except OSError:
        # A read-only or full data dir loses the memory of this send. The send
        # already happened and its result is in the response, so this is not
        # worth failing the request over.
        pass


def _still_valid(check: Optional[dict], destination: Optional[dict]) -> bool:
    """Whether a past success still proves anything about the current address.

    It does not if the home channel has since been repointed. A green tick
    earned against a chat nobody reads any more is exactly the stale claim this
    module is here to refuse.
    """
    if not check or not check.get("ok") or not destination:
        return False
    return str(check.get("chat_id") or "") == str(destination["chat_id"])


# --- state ------------------------------------------------------------------


def _describe(entry: dict, destination: Optional[dict], check: Optional[dict],
              channel_id: str) -> dict:
    """One channel row: what is set, where output lands, and what proved it."""
    spec = SETUP[channel_id]
    home_key = HOME_ENV[channel_id]
    by_key = {v.get("key"): v for v in (entry.get("env_vars") or [])
              if isinstance(v, dict)}
    missing = [k for k in spec["required_env"]
               if not (by_key.get(k) or {}).get("is_set")]
    # Hermes's own `configured` is the authority where it offered one: it comes
    # from GatewayConfig._is_platform_connected, which knows more than whether
    # two env vars are non-empty. The env-var reading is the fallback for a row
    # Hermes did not recognise at all.
    credential = (not missing) if entry.get("unknown") else bool(entry.get("configured"))

    # config.yaml's home channel — what /sethome persists and what silences the
    # gateway's nudge. Carried here ONLY so the divergence below can be named.
    yaml_home = entry.get("home_channel")
    yaml_home = yaml_home if isinstance(yaml_home, dict) and yaml_home.get("chat_id") else None

    verified = _still_valid(check, destination)
    stale = bool(check and check.get("ok") and destination and not verified)

    if not credential:
        status = "no_credential"
        headline = f"No credential yet, so nothing can be sent to {spec['name']}."
    elif destination is None:
        status = "no_destination"
        if yaml_home:
            # The trap. Everything visible says connected; scheduled delivery
            # resolves to None and the output is discarded without a word.
            headline = (
                f"Connected, and scheduled work sent here is being silently "
                f"discarded. A home channel is recorded in config.yaml "
                f"({yaml_home.get('name') or yaml_home.get('chat_id')}), but "
                f"cron reads {home_key} from .env and that is not set — so a "
                f"job with deliver: {channel_id} resolves to nothing and drops "
                f"its output with no error anywhere."
            )
        else:
            headline = (
                f"Connected for conversation, but no home channel: {home_key} "
                f"is not set, so anything a scheduled job delivers here is "
                f"discarded without an error."
            )
    elif verified:
        status = "verified"
        headline = (
            f"A message was delivered to {destination['chat_id']} — the same "
            f"address a scheduled job uses."
        )
    else:
        status = "unproven"
        headline = (
            f"Set up to deliver to {destination['chat_id']}, and nothing has "
            f"been delivered yet. A credential that has carried no message is "
            f"not yet proof of anything."
        )

    # A destination in .env that the gateway has not read yet still delivers
    # nothing, and the gateway only re-reads at start. Worth saying out loud,
    # because the test send below WILL succeed in that state: it talks to the
    # platform directly and does not go through the gateway at all.
    return {
        "id": channel_id,
        "name": spec["name"],
        "cost": spec["cost"],
        "docs_url": spec["docs_url"],
        "sethome": spec["sethome"],
        "status": status,
        "headline": headline,
        "credential": credential,
        "enabled": bool(entry.get("enabled")),
        "missing_env": missing,
        "required_env": list(spec["required_env"]),
        "home_env_var": home_key,
        "destination": destination,
        "config_home_channel": yaml_home,
        # The named divergence, so the frontend can shout about this one case
        # without re-deriving it from three other fields.
        "destination_unwired": bool(credential and yaml_home and destination is None),
        "deliverable": bool(credential and destination),
        "can_test": bool(credential and destination),
        "last_test": check,
        "last_test_stale": stale,
        "state": entry.get("state"),
        "error_message": entry.get("error_message"),
    }


async def channel_states(data_dir: str) -> dict:
    """Both day-one channels, their real configuration, and their evidence.

    Credentials come from Hermes's catalog, which reads $HERMES_HOME/.env and
    config.yaml from disk. Destinations are read from that same .env directly,
    because the catalog does not carry the env var cron actually resolves.
    """
    checks = load_checks(data_dir)
    destinations = {cid: cron_destination(data_dir, cid) for cid in CHANNEL_IDS}
    try:
        catalog = await _channels.list_channels(data_dir)
    except HermesUnavailable as exc:
        # Unreachable is not "unconfigured". The difference matters: one is a
        # page admitting it cannot see, the other is a page telling a working
        # install to go and configure itself.
        return {
            "channels": [],
            "reachable": False,
            "error": str(exc),
            "env_path": host_env_path(data_dir),
        }
    by_id = {c.get("id"): c for c in catalog.get("channels", [])}
    rows = [
        _describe(
            by_id.get(cid) or {"id": cid, "unknown": True, "env_vars": []},
            destinations[cid],
            checks.get(cid),
            cid,
        )
        for cid in CHANNEL_IDS
    ]
    return {
        "channels": rows,
        "reachable": True,
        "error": None,
        # The HOST path, not Hermes's own answer: Hermes reports /opt/data/.env
        # from inside its container, which is the right file and a path the
        # operator cannot open.
        "env_path": host_env_path(data_dir),
    }


def summarise(state: dict) -> dict:
    """The one-line checklist verdict over both channels."""
    if not state.get("reachable"):
        return {
            "status": "todo",
            "detail": (
                "Could not read channel configuration, so this cannot say "
                f"whether output has anywhere to go. {state.get('error') or ''}"
            ).strip(),
        }
    rows = state.get("channels") or []

    unwired = [r for r in rows if r["destination_unwired"]]
    if unwired:
        names = ", ".join(r["name"] for r in unwired)
        return {
            "status": "blocked",
            "detail": (
                f"{names} looks connected and is silently discarding scheduled "
                f"output: a home channel is recorded in config.yaml but the "
                f"env var cron reads is not set. Every other indicator on this "
                f"box says this channel is fine."
            ),
        }

    verified = [r for r in rows if r["status"] == "verified"]
    if verified:
        return {
            "status": "ok",
            "detail": (
                "Delivery confirmed on "
                + ", ".join(f"{r['name']} ({r['destination']['chat_id']})"
                            for r in verified)
                + "."
            ),
        }

    unproven = [r for r in rows if r["status"] == "unproven"]
    if unproven:
        return {
            "status": "todo",
            "detail": (
                ", ".join(r["name"] for r in unproven)
                + " can deliver and never has. Send a test message below — it "
                "goes to the same address a scheduled job would use, which is "
                "the only thing that settles it."
            ),
        }

    no_dest = [r for r in rows if r["status"] == "no_destination"]
    if no_dest:
        return {
            "status": "blocked",
            "detail": (
                ", ".join(r["name"] for r in no_dest)
                + " is connected but has no home channel, so anything a "
                "scheduled job delivers there is discarded with no error. "
                "Message the bot from the chat you want and send /sethome."
            ),
        }

    return {
        "status": "todo",
        "detail": (
            "No channel connected. Scheduled work would finish into this "
            "console and wait for someone to remember to open it, which is the "
            "habit Steward exists to remove."
        ),
    }


# --- the send itself --------------------------------------------------------


class TestSendRefused(RuntimeError):
    """The send was not attempted. Carries the operator-facing reason."""


def _result(ok: bool, reason: str, message: str, **extra: Any) -> dict:
    return {
        "ok": ok,
        # A machine-readable cause beside the sentence, so the page can render
        # "the bot is not in that channel" differently from "the token is
        # wrong" without parsing prose.
        "reason": reason,
        "message": message,
        "at": time.time(),
        **extra,
    }


def _telegram_send(token: str, chat_id: str, thread_id: Optional[str]) -> dict:
    body: Dict[str, Any] = {"chat_id": chat_id, "text": TEST_MESSAGE}
    if thread_id:
        body["message_thread_id"] = thread_id
    try:
        with httpx.Client(timeout=SEND_TIMEOUT) as client:
            resp = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage", json=body
            )
    except httpx.RequestError as exc:
        return _result(
            False, "network",
            f"Could not reach api.telegram.org from this host: {exc}",
        )

    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if payload.get("ok"):
        return _result(True, "delivered",
                       f"Telegram accepted the message for chat {chat_id}.")

    # Telegram states the reason in `description` and it is worth forwarding
    # verbatim. The branches below only add what the operator has to do.
    description = str(payload.get("description") or resp.text[:200] or "").strip()
    lowered = description.lower()
    if resp.status_code == 401 or "unauthorized" in lowered:
        return _result(
            False, "bad_token",
            "Telegram rejected the bot token (401 Unauthorized). TELEGRAM_BOT_TOKEN "
            "is wrong, or the bot was deleted in @BotFather.",
        )
    if "chat not found" in lowered:
        return _result(
            False, "unknown_chat",
            f"Telegram does not know chat {chat_id}. TELEGRAM_HOME_CHANNEL points "
            "at a chat that no longer exists, or that a different bot was in. Send "
            "/sethome again from the chat you want.",
        )
    if "blocked" in lowered or "kicked" in lowered or "not a member" in lowered:
        return _result(
            False, "not_a_member",
            f"The bot cannot post in chat {chat_id}: {description}. Add it back to "
            "the group, or unblock it from the chat.",
        )
    if "thread not found" in lowered or "message thread not found" in lowered:
        return _result(
            False, "unknown_thread",
            f"Chat {chat_id} exists but topic {thread_id} does not. Clear "
            "TELEGRAM_CRON_THREAD_ID / TELEGRAM_HOME_CHANNEL_THREAD_ID, or set it "
            "to a topic that still exists.",
        )
    if resp.status_code == 429 or "too many requests" in lowered:
        return _result(False, "rate_limited",
                       f"Telegram is rate-limiting this bot: {description}")
    return _result(
        False, "rejected",
        f"Telegram rejected the message ({resp.status_code}): "
        f"{description or 'no reason given'}",
    )


# Slack answers 200 with ``ok: false`` and a machine-readable ``error``, so the
# status code says nothing at all. Each entry here is a different thing for a
# person to do, which is why they are not collapsed into "failed".
_SLACK_REASONS = {
    "invalid_auth": (
        "bad_token",
        "Slack rejected the bot token (invalid_auth). SLACK_BOT_TOKEN should be the "
        "xoxb- token from the app's OAuth page, not the xapp- app token.",
    ),
    "not_authed": ("bad_token", "Slack received no credential (not_authed)."),
    "token_revoked": (
        "bad_token",
        "This bot token has been revoked. Reinstall the app to the workspace and "
        "copy the new xoxb- token.",
    ),
    "account_inactive": (
        "bad_token", "The Slack account behind this token is deactivated."),
    "not_in_channel": (
        "not_a_member",
        "The bot is not in that channel. In Slack, run /invite @<your bot> in the "
        "channel, then try again.",
    ),
    "channel_not_found": (
        "unknown_chat",
        "Slack does not know that channel id. SLACK_HOME_CHANNEL names a channel "
        "that has been archived, deleted, or belongs to another workspace.",
    ),
    "is_archived": (
        "unknown_chat", "That Slack channel is archived, so nothing can be posted."),
    "missing_scope": (
        "missing_scope",
        "The Slack app is missing the chat:write scope. Add it under OAuth & "
        "Permissions and reinstall the app.",
    ),
    "ratelimited": (
        "rate_limited", "Slack is rate-limiting this app; try again shortly."),
}


def _slack_send(token: str, channel: str, thread_id: Optional[str]) -> dict:
    body: Dict[str, Any] = {"channel": channel, "text": TEST_MESSAGE}
    if thread_id:
        body["thread_ts"] = thread_id
    try:
        with httpx.Client(timeout=SEND_TIMEOUT) as client:
            resp = client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
    except httpx.RequestError as exc:
        return _result(False, "network",
                       f"Could not reach slack.com from this host: {exc}")

    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if payload.get("ok"):
        return _result(True, "delivered",
                       f"Slack accepted the message for {channel}.")
    code = str(payload.get("error") or "").strip()
    known = _SLACK_REASONS.get(code)
    if known:
        return _result(False, known[0], known[1])
    if resp.status_code >= 400 and not code:
        return _result(False, "rejected",
                       f"Slack answered {resp.status_code} with no error code.")
    return _result(False, "rejected",
                   f"Slack rejected the message: {code or 'no reason given'}.")


def _cooldown_remaining(check: Optional[dict], now: float) -> float:
    if not check:
        return 0.0
    try:
        elapsed = now - float(check.get("at") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, COOLDOWN_SECONDS - elapsed)


async def send_test(data_dir: str, channel_id: str) -> dict:
    """Deliver one fixed message down the path a scheduled job would take.

    Raises :class:`TestSendRefused` when nothing is attempted — unknown channel,
    no credential, no destination, or the cooldown. Those are answers about this
    box, not about the platform, and rendering them as a delivery failure would
    blame Telegram for an unset env var.
    """
    if channel_id not in SETUP:
        raise TestSendRefused(f"{channel_id} is not a channel this page sets up.")

    state = await channel_states(data_dir)
    if not state["reachable"]:
        raise TestSendRefused(state["error"] or "Channel configuration is unreadable.")
    row = next((r for r in state["channels"] if r["id"] == channel_id), None)
    if row is None:
        raise TestSendRefused(f"{channel_id} is not a channel this page sets up.")
    if not row["credential"]:
        missing = ", ".join(row["missing_env"]) or "its credentials"
        raise TestSendRefused(
            f"{row['name']} has no credential — {missing} is not set in "
            f"{state['env_path']}, so there is nothing to send with."
        )
    destination = row["destination"]
    if destination is None:
        raise TestSendRefused(
            f"{row['name']} has no home channel, so a scheduled job delivering "
            f"there is discarded. Message the bot from the chat you want and send "
            f"{row['sethome']}, or set {row['home_env_var']} in {state['env_path']}."
        )

    now = time.time()
    remaining = _cooldown_remaining(row.get("last_test"), now)
    if remaining > 0:
        raise TestSendRefused(
            f"A test message was sent {int(COOLDOWN_SECONDS - remaining)}s ago. "
            f"Wait {int(remaining) + 1}s before sending another."
        )

    spec = SETUP[channel_id]
    env = read_env(data_dir, spec["required_env"])
    # The sending credential is the first required var for both channels.
    # Slack's xapp- app token drives Socket Mode, not the Web API, so it is not
    # the one that posts.
    token_key = spec["required_env"][0]
    token = env.get(token_key, "")
    if not token:
        raise TestSendRefused(
            f"{token_key} is set as far as the gateway is concerned but could not "
            f"be read from {state['env_path']} by this container, so no message "
            "can be sent from here."
        )

    chat_id = str(destination["chat_id"])
    thread_id = destination.get("thread_id")
    if channel_id == "telegram":
        result = _telegram_send(token, chat_id, thread_id)
    else:
        result = _slack_send(token, chat_id, thread_id)

    result["channel"] = channel_id
    # Recorded so a later success can be invalidated when the home channel is
    # repointed — see _still_valid.
    result["chat_id"] = chat_id
    result["env_var"] = destination["env_var"]
    save_check(data_dir, channel_id, result)
    return result
