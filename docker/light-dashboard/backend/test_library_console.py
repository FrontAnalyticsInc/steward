"""Tests for the console surface over the automation library.

`automation_library.py` and its 34 tests cover what a template is and when it
refuses to become a job. This file covers the three routes that put that in
front of a person, and it exists because the failures worth catching here are
not validation failures — the library already refuses correctly. They are:

  * a refusal that reaches the browser as "400" instead of as the template's
    own question, which leaves the operator guessing which of four answers was
    wanted;
  * a *successful* creation that silently loses one of `render_job`'s kwargs on
    the way to the gateway. `monitor_url` is the whole mechanism of a monitor
    template — drop it and the job looks identical, runs, and re-sends the same
    summary for ever;
  * a job created enabled. Every template ships switched off on purpose, and
    "created, then disabled by a second request" is not the same promise.

The gateway is stubbed rather than reached: these tests are about what this
process sends and how it reads the answer. What the gateway then does with
`enabled_toolsets` and `monitor_url` is `cron.jobs.create_job`'s business, and
test_automation_library.py pins the kwargs against its signature.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from . import main as M

REPO_LIBRARY = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "hermes", "automations", "library")
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeGateway:
    """The gateway's job API, as far as this process can tell.

    Records every call so a test can assert on the body that went out — which
    is the point: the bug this guards against is a field that never left here.
    """

    def __init__(self, *, create=None, patch=None, create_status=200, patch_status=200):
        self.calls = []
        self._create = create or {"job": {"id": "abc123456789", "name": "made", "enabled": False}}
        self._patch = patch or {"job": {"id": "abc123456789", "name": "made", "enabled": True}}
        self.create_status = create_status
        self.patch_status = patch_status

    # The stand-in for `httpx.AsyncClient(...)`, used as an async context
    # manager in main.py.
    def AsyncClient(self, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, json))
        return FakeResponse(self.create_status, self._create)

    async def patch(self, url, headers=None, json=None):
        self.calls.append(("PATCH", url, json))
        return FakeResponse(self.patch_status, self._patch)

    # main.py catches httpx.RequestError; the real class, so the except clause
    # in the code under test still means what it says.
    RequestError = httpx.RequestError


def client_with(gateway, *, library=REPO_LIBRARY, jobs=None):
    """A TestClient with the gateway stubbed and the library pointed somewhere.

    Channels are stubbed too: `delivery.channel_states` reaches the Hermes
    dashboard over HTTP, and a test that quietly depends on whether something
    is listening on 9119 is a test that passes on the wrong machine.
    """
    patches = [
        mock.patch.object(M, "httpx", gateway),
        mock.patch.object(M, "AUTOMATION_LIBRARY_DIR", library),
        mock.patch.object(
            M.delivery, "channel_states",
            mock.AsyncMock(return_value={
                "channels": [
                    {"id": "telegram", "name": "Telegram", "headline": "Ready.", "deliverable": True},
                    {"id": "slack", "name": "Slack", "headline": "No credential yet.", "deliverable": False},
                ],
                "reachable": True,
            }),
        ),
        mock.patch.object(M, "_enriched_cron_jobs", lambda: jobs if jobs is not None else []),
    ]
    for p in patches:
        p.start()
    c = TestClient(M.app)
    c._patches = patches
    return c


def stop(c):
    for p in getattr(c, "_patches", []):
        p.stop()


class LibraryListing(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.client = client_with(self.gateway)
        self.addCleanup(stop, self.client)

    def test_the_shipped_templates_are_visible(self):
        """The blank tab was the whole complaint. Four templates ship; four show."""
        body = self.client.get("/api/automations/library").json()
        ids = sorted(t["id"] for t in body["templates"])
        self.assertEqual(
            ids,
            ["competitor-watch", "industry-brief", "source-scan", "website-topic-map"],
        )
        for t in body["templates"]:
            self.assertEqual(t["problems"], [], f"{t['id']} does not validate")

    def test_each_parameter_carries_the_question_it_was_written_with(self):
        """The form's labels are the template author's, not this layer's.

        A parameter with no `ask` is already a validation error in the format;
        what this pins is that the text survives the trip to the browser
        instead of being replaced by a prettified field name.
        """
        body = self.client.get("/api/automations/library").json()
        watch = next(t for t in body["templates"] if t["id"] == "competitor-watch")
        asks = {p["name"]: p["ask"] for p in watch["parameters"]}
        self.assertEqual(
            asks["competitors"],
            "Which companies should be watched? Name them explicitly.",
        )
        required = [p for p in watch["parameters"] if p["required"]]
        self.assertTrue(required)
        for p in watch["parameters"]:
            self.assertTrue((p["ask"] or "").strip(), f"{p['name']} has no question")

    def test_optional_parameters_arrive_with_their_defaults(self):
        """So the form shows what will be used rather than an empty box that
        silently becomes something else."""
        body = self.client.get("/api/automations/library").json()
        brief = next(t for t in body["templates"] if t["id"] == "industry-brief")
        by_name = {p["name"]: p for p in brief["parameters"]}
        self.assertEqual(by_name["max_items"]["default"], 5)
        self.assertNotIn("default", by_name["industry"])

    def test_delivery_choices_include_the_run_log_and_the_channels(self):
        body = self.client.get("/api/automations/library").json()
        by_id = {c["id"]: c for c in body["delivery_choices"]}
        self.assertIn("local", by_id)
        self.assertTrue(by_id["telegram"]["deliverable"])
        self.assertFalse(by_id["slack"]["deliverable"])

    def test_an_unknown_template_is_a_404_and_a_malformed_id_is_a_400(self):
        """The id is a path component under a directory this process can read,
        so it is matched against the format's own id rule before it is joined
        onto anything."""
        self.assertEqual(
            self.client.post("/api/automations/library/nope/create", json={"values": {}}).status_code,
            404,
        )
        # ".." never gets here — the URL layer collapses it — so the cases
        # that matter are the ones that reach the handler as one segment.
        for bad in ("Competitor_Watch", "-leading", "has.dot", "a" * 80):
            self.assertEqual(
                self.client.post(
                    f"/api/automations/library/{bad}/create", json={"values": {}}
                ).status_code,
                400,
                bad,
            )


class ABrokenTemplateFile(unittest.TestCase):
    def test_one_unreadable_file_costs_its_own_card_and_not_the_tab(self):
        """Templates are meant to be edited on the box.

        `list_templates` raises on the first file that will not parse, which
        would take the whole library — and the tab — down with it. A file with
        a stray character in it is one broken card beside three working ones.
        """
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "broken.yaml"), "w", encoding="utf-8") as fh:
                fh.write("id: broken\n\tthis is not yaml: [\n")
            with open(os.path.join(tmp, "fine.yaml"), "w", encoding="utf-8") as fh:
                fh.write("id: fine\ntitle: Fine\n")
            client = client_with(FakeGateway(), library=tmp)
            self.addCleanup(stop, client)
            body = client.get("/api/automations/library").json()
            by_id = {t["id"]: t for t in body["templates"]}
            self.assertEqual(sorted(by_id), ["broken", "fine"])
            self.assertTrue(by_id["broken"]["problems"])
            # And the one that parsed is still described, problems and all.
            self.assertTrue(by_id["fine"]["problems"])
            self.assertEqual(by_id["fine"]["title"], "Fine")

    def test_a_missing_library_says_where_it_should_be(self):
        client = client_with(FakeGateway(), library="/nope/automations/library")
        self.addCleanup(stop, client)
        res = client.get("/api/automations/library")
        self.assertEqual(res.status_code, 404)
        self.assertIn("seed.sh", res.json()["detail"])


class RefusingAnIncompleteForm(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.client = client_with(self.gateway)
        self.addCleanup(stop, self.client)

    def test_a_missing_required_parameter_names_itself_in_its_own_words(self):
        """The property the format exists for, at the surface.

        Not a greyed-out button: the operator is told which answer is missing
        and, in the template's own sentence, what a good one would be.
        """
        res = self.client.post(
            "/api/automations/library/competitor-watch/create", json={"values": {}}
        )
        self.assertEqual(res.status_code, 422)
        problems = res.json()["detail"]["problems"]
        self.assertTrue(any("competitors" in p for p in problems))
        self.assertTrue(
            any("Name them explicitly" in p for p in problems),
            f"the template's own question is missing from {problems}",
        )

    def test_nothing_reaches_the_gateway_when_the_form_is_incomplete(self):
        """The refusal is structural, so no job exists to have to clean up."""
        self.client.post("/api/automations/library/industry-brief/create", json={"values": {}})
        self.assertEqual(self.gateway.calls, [])

    def test_every_missing_answer_is_reported_at_once(self):
        """industry-brief asks two required questions. Answering one and being
        told about the other, one at a time, is the form nobody finishes."""
        res = self.client.post(
            "/api/automations/library/industry-brief/create", json={"values": {}}
        )
        problems = res.json()["detail"]["problems"]
        self.assertTrue(any(p.startswith("industry:") for p in problems))
        self.assertTrue(any(p.startswith("themes:") for p in problems))

    def test_a_wrong_type_is_refused_with_the_reason(self):
        res = self.client.post(
            "/api/automations/library/source-scan/create",
            json={"values": {
                "source_url": "example.com",
                "source_name": "Example",
                "interest": "pricing changes",
            }},
        )
        self.assertEqual(res.status_code, 422)
        self.assertTrue(
            any("http(s) URL" in p for p in res.json()["detail"]["problems"])
        )


class CreatingTheJob(unittest.TestCase):
    def test_the_gateway_gets_render_jobs_kwargs_and_nothing_invented(self):
        gateway = FakeGateway()
        client = client_with(gateway)
        self.addCleanup(stop, client)
        res = client.post(
            "/api/automations/library/competitor-watch/create",
            json={"values": {"competitors": "Acme, Northwind"}, "deliver": "telegram"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        method, url, body = gateway.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/jobs"))
        self.assertIn("Acme", body["prompt"])
        self.assertEqual(body["schedule"], "0 8 * * 1")
        self.assertEqual(body["deliver"], "telegram")
        self.assertEqual(body["enabled_toolsets"], ["web"])
        self.assertIs(body["enabled"], False)

    def test_a_monitor_template_carries_its_monitor_url(self):
        """The failure that would be invisible.

        source-scan's entire mechanism is the scheduler hashing this URL before
        any model call. A job created without it looks the same in the list,
        runs on schedule, and reports the same page as news every week.
        """
        gateway = FakeGateway()
        client = client_with(gateway)
        self.addCleanup(stop, client)
        res = client.post(
            "/api/automations/library/source-scan/create",
            json={"values": {
                "source_url": "https://example.com/changelog",
                "source_name": "Example changelog",
                "interest": "pricing changes",
            }},
        )
        self.assertEqual(res.status_code, 200, res.text)
        _, _, body = gateway.calls[0]
        self.assertEqual(body["monitor_url"], "https://example.com/changelog")

    def test_a_gateway_that_returns_an_enabled_job_is_switched_off(self):
        """Belt to the gateway's braces: an older build that ignores
        `enabled: false` must not leave a job armed."""
        gateway = FakeGateway(
            create={"job": {"id": "abc123456789", "name": "made", "enabled": True}},
            patch={"job": {"id": "abc123456789", "name": "made", "enabled": False}},
        )
        client = client_with(gateway)
        self.addCleanup(stop, client)
        res = client.post(
            "/api/automations/library/competitor-watch/create",
            json={"values": {"competitors": "Acme"}},
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIs(res.json()["job"]["enabled"], False)
        self.assertEqual(gateway.calls[1][0], "PATCH")
        self.assertEqual(gateway.calls[1][2], {"enabled": False})

    def test_a_job_that_could_not_be_switched_off_is_shouted_about(self):
        gateway = FakeGateway(
            create={"job": {"id": "abc123456789", "name": "made", "enabled": True}},
            patch_status=500,
        )
        client = client_with(gateway)
        self.addCleanup(stop, client)
        res = client.post(
            "/api/automations/library/competitor-watch/create",
            json={"values": {"competitors": "Acme"}},
        )
        self.assertEqual(res.status_code, 500)
        self.assertIn("will run", res.json()["detail"])

    def test_a_kwarg_the_gateway_cannot_carry_is_refused_rather_than_dropped(self):
        """The other half of the silent-loss guard.

        No shipped template uses `monitor_script`, and the gateway's job API
        deliberately will not accept one over HTTP. A box whose library grew
        one must be told, not handed a job with the mechanism missing.
        """
        template = {
            "id": "script-monitor", "version": 1, "title": "Script monitor",
            "summary": "Watches a script's output.", "tier": "prompt-cron",
            "parameters": [
                {"name": "topic", "type": "string", "required": True, "ask": "Which topic?"},
            ],
            "schedule": {"default": "every 1h", "editable": True},
            "delivery": {"default": "local", "editable": True},
            "change_detection": {"mechanism": "monitor", "verify": "Run it twice."},
            "job": {
                "name": "Watch {{topic}}",
                "monitor_script": "watch.sh",
                "prompt": "Report on {{topic}}.",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "script-monitor.yaml"), "w", encoding="utf-8") as fh:
                json.dump(template, fh)  # JSON is YAML
            gateway = FakeGateway()
            client = client_with(gateway, library=tmp)
            self.addCleanup(stop, client)
            res = client.post(
                "/api/automations/library/script-monitor/create",
                json={"values": {"topic": "widgets"}},
            )
            self.assertEqual(res.status_code, 501, res.text)
            self.assertIn("monitor_script", res.json()["detail"])
            self.assertEqual(gateway.calls, [])


class SwitchingAJobOn(unittest.TestCase):
    JOB = {"id": "abc123456789", "name": "Competitor watch", "enabled": False,
           "agent": "default", "agent_is_default": True}

    def test_enabling_is_one_call(self):
        gateway = FakeGateway(patch={"job": {**JOBS_ON}})
        client = client_with(gateway, jobs=[self.JOB])
        self.addCleanup(stop, client)
        res = client.post("/api/cron/jobs/abc123456789/enabled", json={"enabled": True})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIs(res.json()["job"]["enabled"], True)
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(gateway.calls[0][0], "PATCH")
        self.assertEqual(gateway.calls[0][2], {"enabled": True})

    def test_an_unknown_job_is_a_404_and_a_bad_id_never_reaches_the_gateway(self):
        gateway = FakeGateway()
        client = client_with(gateway, jobs=[self.JOB])
        self.addCleanup(stop, client)
        self.assertEqual(
            client.post("/api/cron/jobs/ffffffffffff/enabled", json={"enabled": True}).status_code,
            404,
        )
        self.assertEqual(
            client.post("/api/cron/jobs/NOTHEX/enabled", json={"enabled": True}).status_code,
            400,
        )
        self.assertEqual(gateway.calls, [])

    def test_another_profiles_job_is_refused_rather_than_guessed_at(self):
        """The API server speaks for its own home only. Two profiles can hold
        jobs with different ids and the same name; switching on the wrong one
        would be silent."""
        other = {**self.JOB, "agent": "worker", "agent_is_default": False}
        gateway = FakeGateway()
        client = client_with(gateway, jobs=[other])
        self.addCleanup(stop, client)
        res = client.post("/api/cron/jobs/abc123456789/enabled", json={"enabled": True})
        self.assertEqual(res.status_code, 409)
        self.assertIn("worker", res.json()["detail"])
        self.assertEqual(gateway.calls, [])


JOBS_ON = {"id": "abc123456789", "name": "Competitor watch", "enabled": True}


if __name__ == "__main__":
    unittest.main()
