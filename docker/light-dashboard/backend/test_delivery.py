"""Tests for the claim that finished work can reach someone.

What is worth pinning down here is not that httpx can POST — it is the set of
judgements that decide whether a box is allowed to look ready, and every one of
them exists because the obvious reading of the same data is wrong:

* A token is not a destination. Cron resolves a bare ``deliver: telegram``
  through ``TELEGRAM_HOME_CHANNEL`` alone; without it the output is discarded
  with no error, while the catalog still says ``configured``.
* A home channel in config.yaml is not that env var. ``/sethome`` writes both,
  best-effort, and the failure mode of the env half is a warning in a log.
* A green tick earned against a chat that has since been repointed proves
  nothing about the chat output goes to now.
* "Failed" is not an answer. ``not_in_channel`` and ``invalid_auth`` are
  different things for a person to do.

The two send paths are driven against a real local HTTP server rather than a
mocked transport, so the request that would go to Telegram is the request that
is asserted on. No test here contacts a real platform or reads a real token.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import delivery as D


# --- helpers ----------------------------------------------------------------


def catalog_entry(channel_id, *, configured=True, env_set=True, home=None,
                  enabled=True):
    """One row shaped as backend/channels.list_channels hands it over."""
    spec = D.SETUP[channel_id]
    return {
        "id": channel_id,
        "name": spec["name"],
        "configured": configured,
        "enabled": enabled,
        "state": "connected" if configured else "not_configured",
        "home_channel": home,
        "env_vars": [
            {"key": k, "required": True, "is_set": env_set}
            for k in spec["required_env"]
        ],
        "unknown": False,
    }


class FakeCatalog:
    """Stands in for the Hermes dashboard proxy."""

    def __init__(self, entries, env_path="/opt/data/.env", error=None):
        self.entries = entries
        self.env_path = env_path
        self.error = error

    async def list_channels(self, data_dir):
        if self.error:
            raise D.HermesUnavailable(self.error)
        return {"channels": self.entries, "env_path": self.env_path}


class TempHome(unittest.TestCase):
    """A HERMES_HOME with a .env in it, and the catalog patched out."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._real = D._channels

    def tearDown(self):
        D._channels = self._real

    def write_env(self, **pairs):
        with open(os.path.join(self.dir, ".env"), "w", encoding="utf-8") as fh:
            for k, v in pairs.items():
                fh.write(f"{k}={v}\n")

    def use_catalog(self, *entries, **kw):
        D._channels = FakeCatalog(list(entries), **kw)

    def states(self):
        return asyncio.run(D.channel_states(self.dir))

    def row(self, channel_id="telegram"):
        return next(r for r in self.states()["channels"] if r["id"] == channel_id)


# --- reading the destination ------------------------------------------------


class Destination(TempHome):

    def test_no_env_file_is_no_destination_not_a_crash(self):
        self.assertIsNone(D.cron_destination(self.dir, "telegram"))

    def test_the_home_env_var_is_the_destination(self):
        self.write_env(TELEGRAM_HOME_CHANNEL="-100999")
        dest = D.cron_destination(self.dir, "telegram")
        self.assertEqual(dest["chat_id"], "-100999")
        self.assertEqual(dest["env_var"], "TELEGRAM_HOME_CHANNEL")
        self.assertIsNone(dest["thread_id"])

    def test_an_empty_value_is_not_a_destination(self):
        """`TELEGRAM_HOME_CHANNEL=` reads as set to a shell and resolves to
        nothing in cron. It must not read as a destination here."""
        self.write_env(TELEGRAM_HOME_CHANNEL="")
        self.assertIsNone(D.cron_destination(self.dir, "telegram"))

    def test_quotes_and_export_are_stripped(self):
        with open(os.path.join(self.dir, ".env"), "w", encoding="utf-8") as fh:
            fh.write("# a comment\n\nexport SLACK_HOME_CHANNEL='C0EXAMPLE'\n")
        self.assertEqual(
            D.cron_destination(self.dir, "slack")["chat_id"], "C0EXAMPLE")

    def test_telegram_cron_thread_id_wins_over_the_home_thread(self):
        """Mirrors _get_home_target_thread_id: the cron override is checked
        first, because a delivery into a topic-mode root DM lands in a lobby
        the user cannot reply in."""
        self.write_env(
            TELEGRAM_HOME_CHANNEL="42",
            TELEGRAM_HOME_CHANNEL_THREAD_ID="7",
            TELEGRAM_CRON_THREAD_ID="99",
        )
        self.assertEqual(D.cron_destination(self.dir, "telegram")["thread_id"], "99")

    def test_the_home_thread_is_used_when_there_is_no_cron_override(self):
        self.write_env(TELEGRAM_HOME_CHANNEL="42", TELEGRAM_HOME_CHANNEL_THREAD_ID="7")
        self.assertEqual(D.cron_destination(self.dir, "telegram")["thread_id"], "7")

    def test_only_the_named_keys_are_read(self):
        """The parser must not accumulate the rest of a .env full of secrets."""
        self.write_env(TELEGRAM_HOME_CHANNEL="42", ANTHROPIC_API_KEY="sk-not-mine")
        got = D.read_env(self.dir, ("TELEGRAM_HOME_CHANNEL",))
        self.assertEqual(got, {"TELEGRAM_HOME_CHANNEL": "42"})


# --- what state a box is in -------------------------------------------------


class States(TempHome):

    def test_unconfigured_channel_reports_no_credential(self):
        self.use_catalog(catalog_entry("telegram", configured=False, env_set=False),
                         catalog_entry("slack", configured=False, env_set=False))
        row = self.row()
        self.assertEqual(row["status"], "no_credential")
        self.assertFalse(row["deliverable"])
        self.assertFalse(row["can_test"])
        self.assertEqual(row["missing_env"], ["TELEGRAM_BOT_TOKEN"])

    def test_a_token_alone_is_not_deliverable(self):
        """The whole point. Hermes says configured; cron drops the output."""
        self.use_catalog(catalog_entry("telegram"), catalog_entry("slack"))
        row = self.row()
        self.assertEqual(row["status"], "no_destination")
        self.assertFalse(row["deliverable"])
        self.assertFalse(row["can_test"])

    def test_a_switched_off_channel_with_a_good_token_is_not_deliverable(self):
        """Hermes reports enabled and configured independently, and they are:
        _is_platform_connected asks only whether a token exists. A channel that
        is off runs no adapter, so cron has nothing to deliver through."""
        self.write_env(TELEGRAM_HOME_CHANNEL="-100999")
        self.use_catalog(catalog_entry("telegram", enabled=False),
                         catalog_entry("slack", configured=False, env_set=False))
        row = self.row()
        self.assertEqual(row["status"], "disabled")
        self.assertFalse(row["deliverable"])
        self.assertEqual(D.summarise(self.states())["status"], "blocked")

    def test_a_switched_off_channel_can_still_be_tested(self):
        """The send goes straight to the platform, so it proves the token and
        the address even while the adapter is down — worth knowing."""
        self.write_env(TELEGRAM_HOME_CHANNEL="-100999")
        self.use_catalog(catalog_entry("telegram", enabled=False),
                         catalog_entry("slack", configured=False, env_set=False))
        self.assertTrue(self.row()["can_test"])

    def test_a_row_hermes_does_not_recognise_is_not_called_switched_off(self):
        """`unknown` rows carry no enabled flag, and reading its default as
        "off" would be an invention about a platform this build cannot see."""
        self.write_env(TELEGRAM_HOME_CHANNEL="-100999", TELEGRAM_BOT_TOKEN="t")
        self.use_catalog({"id": "telegram", "unknown": True, "env_vars": [
            {"key": "TELEGRAM_BOT_TOKEN", "required": True, "is_set": True}]},
            catalog_entry("slack", configured=False, env_set=False))
        self.assertEqual(self.row()["status"], "unproven")

    def test_a_config_yaml_home_channel_does_not_make_it_deliverable(self):
        """The trap: /sethome wrote config.yaml, its best-effort env write did
        not land, and every indicator on the box goes green while scheduled
        output is discarded."""
        self.use_catalog(
            catalog_entry("telegram", home={"chat_id": "-100999", "name": "Ops"}),
            catalog_entry("slack", configured=False, env_set=False),
        )
        row = self.row()
        self.assertEqual(row["status"], "no_destination")
        self.assertTrue(row["destination_unwired"])
        self.assertFalse(row["deliverable"])
        self.assertIn("TELEGRAM_HOME_CHANNEL", row["headline"])
        self.assertIn("silently discarding", D.summarise(self.states())["detail"])

    def test_token_plus_env_destination_is_deliverable_but_unproven(self):
        self.write_env(TELEGRAM_HOME_CHANNEL="-100999")
        self.use_catalog(catalog_entry("telegram"),
                         catalog_entry("slack", configured=False, env_set=False))
        row = self.row()
        self.assertEqual(row["status"], "unproven")
        self.assertTrue(row["deliverable"])
        self.assertTrue(row["can_test"])

    def test_a_recorded_delivery_to_the_current_chat_is_verified(self):
        self.write_env(TELEGRAM_HOME_CHANNEL="-100999")
        D.save_check(self.dir, "telegram",
                     {"ok": True, "chat_id": "-100999", "at": time.time() - 500})
        self.use_catalog(catalog_entry("telegram"),
                         catalog_entry("slack", configured=False, env_set=False))
        row = self.row()
        self.assertEqual(row["status"], "verified")
        self.assertFalse(row["last_test_stale"])

    def test_a_delivery_to_a_chat_since_repointed_is_not_verified(self):
        """A tick earned against a chat nobody reads any more proves nothing
        about where output goes now."""
        self.write_env(TELEGRAM_HOME_CHANNEL="-100NEW")
        D.save_check(self.dir, "telegram",
                     {"ok": True, "chat_id": "-100OLD", "at": time.time() - 500})
        self.use_catalog(catalog_entry("telegram"),
                         catalog_entry("slack", configured=False, env_set=False))
        row = self.row()
        self.assertEqual(row["status"], "unproven")
        self.assertTrue(row["last_test_stale"])

    def test_a_failed_send_does_not_make_it_verified(self):
        self.write_env(TELEGRAM_HOME_CHANNEL="-100999")
        D.save_check(self.dir, "telegram",
                     {"ok": False, "reason": "not_a_member", "chat_id": "-100999",
                      "at": time.time() - 500})
        self.use_catalog(catalog_entry("telegram"),
                         catalog_entry("slack", configured=False, env_set=False))
        self.assertEqual(self.row()["status"], "unproven")

    def test_an_unreachable_hermes_is_not_reported_as_unconfigured(self):
        self.use_catalog(error="dashboard refused the credential")
        state = self.states()
        self.assertFalse(state["reachable"])
        self.assertEqual(state["channels"], [])
        self.assertIn("dashboard refused", D.summarise(state)["detail"])
        self.assertEqual(D.summarise(state)["status"], "todo")


class Verdict(TempHome):
    """The one line the first-run checklist shows."""

    def test_nothing_connected_is_todo_not_blocked(self):
        """A box with no channel is not lying about anything."""
        self.use_catalog(catalog_entry("telegram", configured=False, env_set=False),
                         catalog_entry("slack", configured=False, env_set=False))
        self.assertEqual(D.summarise(self.states())["status"], "todo")

    def test_connected_with_nowhere_to_deliver_is_blocked(self):
        self.use_catalog(catalog_entry("telegram"),
                         catalog_entry("slack", configured=False, env_set=False))
        self.assertEqual(D.summarise(self.states())["status"], "blocked")

    def test_deliverable_and_unproven_is_todo(self):
        self.write_env(TELEGRAM_HOME_CHANNEL="-100999")
        self.use_catalog(catalog_entry("telegram"),
                         catalog_entry("slack", configured=False, env_set=False))
        verdict = D.summarise(self.states())
        self.assertEqual(verdict["status"], "todo")
        self.assertIn("never has", verdict["detail"])

    def test_one_verified_channel_is_enough(self):
        """Telegram delivering is the goal; an unconfigured Slack beside it is
        not a fault."""
        self.write_env(TELEGRAM_HOME_CHANNEL="-100999")
        D.save_check(self.dir, "telegram",
                     {"ok": True, "chat_id": "-100999", "at": time.time()})
        self.use_catalog(catalog_entry("telegram"),
                         catalog_entry("slack", configured=False, env_set=False))
        verdict = D.summarise(self.states())
        self.assertEqual(verdict["status"], "ok")
        self.assertIn("-100999", verdict["detail"])


# --- the send ---------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.requests.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(body or b"{}"),
        })
        status, payload = self.server.reply
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class Platform:
    """A real HTTP server standing in for api.telegram.org / slack.com."""

    def __init__(self, status, payload):
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.requests = []
        self.httpd.reply = (status, payload)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def requests(self):
        return self.httpd.requests

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Sending(TempHome):

    def send(self, channel="telegram"):
        return asyncio.run(D.send_test(self.dir, channel))

    def test_a_missing_destination_refuses_rather_than_failing_a_send(self):
        """Blaming Telegram for an unset env var would be the wrong sentence."""
        self.use_catalog(catalog_entry("telegram"), catalog_entry("slack"))
        with self.assertRaises(D.TestSendRefused) as caught:
            self.send()
        self.assertIn("TELEGRAM_HOME_CHANNEL", str(caught.exception))

    def test_a_missing_credential_refuses(self):
        self.write_env(TELEGRAM_HOME_CHANNEL="-100999")
        self.use_catalog(catalog_entry("telegram", configured=False, env_set=False),
                         catalog_entry("slack"))
        with self.assertRaises(D.TestSendRefused) as caught:
            self.send()
        self.assertIn("TELEGRAM_BOT_TOKEN", str(caught.exception))

    def test_an_unknown_channel_refuses(self):
        with self.assertRaises(D.TestSendRefused):
            asyncio.run(D.send_test(self.dir, "discord"))

    def test_a_recent_test_is_refused_by_the_cooldown(self):
        self.write_env(TELEGRAM_HOME_CHANNEL="-100999", TELEGRAM_BOT_TOKEN="t")
        D.save_check(self.dir, "telegram",
                     {"ok": True, "chat_id": "-100999", "at": time.time()})
        self.use_catalog(catalog_entry("telegram"), catalog_entry("slack"))
        with self.assertRaises(D.TestSendRefused) as caught:
            self.send()
        self.assertIn("Wait", str(caught.exception))


class TelegramSend(unittest.TestCase):
    """Driven directly, against a real socket."""

    def _call(self, server, token, chat, thread):
        import httpx
        original = httpx.Client
        base = server.url

        class Redirected(original):
            def post(self, url, **kw):
                return original.post(self, base + "/x", **kw)

        httpx.Client = Redirected
        try:
            return D._telegram_send(token, chat, thread)
        finally:
            httpx.Client = original

    def _run(self, status, payload, chat="-100999", thread=None):
        server = Platform(status, payload)
        try:
            result = self._call(server, "tok", chat, thread)
            return result, list(server.requests)
        finally:
            server.close()

    def test_a_success_reads_as_delivered(self):
        result, _ = self._run(200, {"ok": True, "result": {"message_id": 5}})
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "delivered")
        self.assertIn("-100999", result["message"])

    def test_the_thread_id_reaches_the_request(self):
        _, requests = self._run(200, {"ok": True}, thread="7")
        self.assertEqual(requests[0]["body"]["message_thread_id"], "7")
        self.assertEqual(requests[0]["body"]["chat_id"], "-100999")

    def test_the_body_is_fixed_and_not_caller_supplied(self):
        _, requests = self._run(200, {"ok": True})
        self.assertEqual(requests[0]["body"]["text"], D.TEST_MESSAGE)

    def test_a_bad_token_is_named_as_a_bad_token(self):
        result, _ = self._run(401, {"ok": False, "description": "Unauthorized"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "bad_token")
        self.assertIn("TELEGRAM_BOT_TOKEN", result["message"])

    def test_a_missing_chat_is_named_and_says_what_to_do(self):
        result, _ = self._run(400, {"ok": False, "description": "Bad Request: chat not found"})
        self.assertEqual(result["reason"], "unknown_chat")
        self.assertIn("/sethome", result["message"])

    def test_being_kicked_out_is_not_the_same_as_a_bad_token(self):
        result, _ = self._run(
            403, {"ok": False, "description": "Forbidden: bot was kicked from the group chat"})
        self.assertEqual(result["reason"], "not_a_member")

    def test_an_unrecognised_rejection_still_forwards_telegrams_own_words(self):
        result, _ = self._run(400, {"ok": False, "description": "Bad Request: message is too long"})
        self.assertEqual(result["reason"], "rejected")
        self.assertIn("message is too long", result["message"])

    def test_an_unreachable_platform_is_network_not_rejected(self):
        import httpx
        original = httpx.Client

        class Dead(original):
            def post(self, url, **kw):
                raise httpx.ConnectError("no route to host")

        httpx.Client = Dead
        try:
            result = D._telegram_send("tok", "-100999", None)
        finally:
            httpx.Client = original
        self.assertEqual(result["reason"], "network")
        self.assertIn("api.telegram.org", result["message"])


class SlackSend(unittest.TestCase):

    def _run(self, status, payload, channel="C0EXAMPLE", thread=None):
        server = Platform(status, payload)
        import httpx
        original = httpx.Client
        base = server.url

        class Redirected(original):
            def post(self, url, **kw):
                return original.post(self, base + "/x", **kw)

        httpx.Client = Redirected
        try:
            result = D._slack_send("xoxb-tok", channel, thread)
            return result, list(server.requests)
        finally:
            httpx.Client = original
            server.close()

    def test_a_success_carries_the_bearer_token_and_the_fixed_body(self):
        result, requests = self._run(200, {"ok": True, "ts": "1.2"})
        self.assertTrue(result["ok"])
        self.assertEqual(requests[0]["auth"], "Bearer xoxb-tok")
        self.assertEqual(requests[0]["body"]["text"], D.TEST_MESSAGE)

    def test_slack_says_200_while_failing_so_the_body_decides(self):
        """The status code says nothing; ok:false is the answer."""
        result, _ = self._run(200, {"ok": False, "error": "invalid_auth"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "bad_token")
        self.assertIn("xoxb-", result["message"])

    def test_not_in_channel_says_to_invite_the_bot(self):
        result, _ = self._run(200, {"ok": False, "error": "not_in_channel"})
        self.assertEqual(result["reason"], "not_a_member")
        self.assertIn("/invite", result["message"])

    def test_a_missing_scope_is_its_own_reason(self):
        result, _ = self._run(200, {"ok": False, "error": "missing_scope"})
        self.assertEqual(result["reason"], "missing_scope")
        self.assertIn("chat:write", result["message"])

    def test_an_unknown_error_code_is_forwarded_rather_than_swallowed(self):
        result, _ = self._run(200, {"ok": False, "error": "org_login_required"})
        self.assertEqual(result["reason"], "rejected")
        self.assertIn("org_login_required", result["message"])


class HostPath(TempHome):
    """The path printed in an instruction has to exist on the operator's machine."""

    def test_the_host_mount_source_is_preferred(self):
        os.environ["STEWARD_DATA_DIR_HOST"] = "/srv/steward/data"
        try:
            self.assertEqual(
                D.host_env_path("/opt/data"), "/srv/steward/data/.env")
        finally:
            del os.environ["STEWARD_DATA_DIR_HOST"]

    def test_without_it_the_container_path_is_used_rather_than_a_guess(self):
        os.environ.pop("STEWARD_DATA_DIR_HOST", None)
        self.assertEqual(D.host_env_path("/opt/data"), "/opt/data/.env")

    def test_the_state_reports_the_host_path_not_hermes_container_path(self):
        os.environ["STEWARD_DATA_DIR_HOST"] = "/srv/steward/data"
        try:
            self.use_catalog(catalog_entry("telegram"), catalog_entry("slack"),
                             env_path="/opt/data/.env")
            self.assertEqual(self.states()["env_path"], "/srv/steward/data/.env")
        finally:
            del os.environ["STEWARD_DATA_DIR_HOST"]


class Record(TempHome):
    """The evidence file."""

    def test_a_missing_record_file_is_empty_not_an_error(self):
        self.assertEqual(D.load_checks(self.dir), {})

    def test_a_corrupt_record_file_costs_the_evidence_not_the_page(self):
        with open(os.path.join(self.dir, D.CHECKS_FILE), "w") as fh:
            fh.write("{not json")
        self.assertEqual(D.load_checks(self.dir), {})

    def test_saving_one_channel_leaves_the_other_alone(self):
        D.save_check(self.dir, "telegram", {"ok": True, "chat_id": "1"})
        D.save_check(self.dir, "slack", {"ok": False, "reason": "not_a_member"})
        saved = D.load_checks(self.dir)
        self.assertEqual(saved["telegram"]["chat_id"], "1")
        self.assertEqual(saved["slack"]["reason"], "not_a_member")

    def test_an_unwritable_data_dir_does_not_raise(self):
        """The send already happened; losing the memory of it is not worth
        failing the request over."""
        D.save_check("/proc/nonexistent-for-this-test", "telegram", {"ok": True})


class ChecklistItem(TempHome):
    """What /api/setup/state puts in front of an operator.

    Driven through _setup_checklist rather than the route, because the route
    also reaches for service health and a model config, and neither is part of
    the claim under test.
    """

    def item(self, state):
        from . import main as M

        items = M._setup_checklist(state)["items"]
        return next(i for i in items if i["id"] == "delivery")

    def test_the_item_is_absent_when_no_delivery_state_was_resolved(self):
        """Older callers, and the path where the whole lookup threw. An absent
        row is honest; a green one would not be."""
        from . import main as M

        ids = [i["id"] for i in M._setup_checklist()["items"]]
        self.assertNotIn("delivery", ids)

    def test_a_box_discarding_scheduled_output_does_not_read_as_set_up(self):
        self.use_catalog(
            catalog_entry("telegram", home={"chat_id": "-100999", "name": "Ops"}),
            catalog_entry("slack", configured=False, env_set=False),
        )
        item = self.item(self.states())
        self.assertEqual(item["status"], "blocked")
        # `blocked` is what keeps the first-run page in front of someone, via
        # the rollup _setup_checklist computes over every item.
        self.assertIn("silently discarding", item["detail"])

    def test_a_box_with_no_channel_at_all_still_counts_as_set_up(self):
        """It has nowhere to speak, which is a decision. It is not a lie, and
        gating the console on a Telegram token would punish the operator for a
        choice that may not be theirs to make at install time."""
        self.use_catalog(catalog_entry("telegram", configured=False, env_set=False),
                         catalog_entry("slack", configured=False, env_set=False))
        item = self.item(self.states())
        self.assertEqual(item["status"], "todo")
        # Not blocked: this row must not be what holds the page open. The
        # Anthropic key item is free to block on its own account.
        self.assertNotEqual(item["status"], "blocked")

    def test_a_verified_channel_reads_as_done(self):
        self.write_env(TELEGRAM_HOME_CHANNEL="-100999")
        D.save_check(self.dir, "telegram",
                     {"ok": True, "chat_id": "-100999", "at": time.time()})
        self.use_catalog(catalog_entry("telegram"),
                         catalog_entry("slack", configured=False, env_set=False))
        item = self.item(self.states())
        self.assertEqual(item["status"], "ok")
        self.assertIsNone(item["why"])


if __name__ == "__main__":
    unittest.main()
