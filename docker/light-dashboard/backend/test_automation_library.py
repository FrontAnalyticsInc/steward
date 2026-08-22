"""Tests for the automation library — the templates and the door out of them.

Standard library plus PyYAML, matching test_automations.py.

Two things are under test and only one of them is code.

The first is `automation_library.py`: that an unconfigured template cannot
become a job, that the `job:` block cannot quietly grow into a second job
format, and that a template claiming change detection has actually wired one
up.

The second is the four shipped YAML files themselves. `ShippedLibrary` reads
the real `hermes/automations/library/` out of the repo, so a template edited
into an invalid state fails CI here rather than on a client's box — which is
the whole reason the format has a validator instead of a style guide.
"""

from __future__ import annotations

import copy
import os
import unittest

import yaml

from . import automation_library as L

REPO_LIBRARY = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "hermes", "automations", "library")
)

# The create_job parameters a rendered job is allowed to use.
#
# Pinned by hand, from the signature of `cron.jobs.create_job` in the gateway
# (hermes-agent). Hermes is not importable here — it lives in the gateway image
# — so this list is the seam. If the scheduler's signature changes, this is the
# line to change, and a rendered job that grows a key not in it is the failure
# this test exists to catch: an invented job format that only fails at fire
# time, on a client's schedule.
CREATE_JOB_KWARGS = frozenset(
    {
        "prompt", "schedule", "name", "repeat", "deliver", "origin", "skill",
        "skills", "model", "provider", "base_url", "script", "context_from",
        "enabled_toolsets", "workdir", "no_agent", "attach_to_session",
        "monitor_script", "monitor_url",
    }
)


def minimal(**overrides):
    """A well-formed template, as the base for one-thing-wrong variants."""
    template = {
        "id": "example",
        "version": 1,
        "title": "Example",
        "summary": "An example.",
        "tier": "prompt-cron",
        "requires_capabilities": ["web_search"],
        "parameters": [
            {"name": "topic", "type": "string", "required": True, "ask": "Which topic?"},
        ],
        "schedule": {"default": "0 8 * * 1", "editable": True},
        "delivery": {"default": "origin", "editable": True},
        "change_detection": {
            "mechanism": "prompt",
            "memory": "agent",
            "verify": "Read week two.",
        },
        "job": {"name": "Example", "prompt": "Watch {{topic}}. If nothing is new say [SILENT]."},
    }
    template.update(overrides)
    return template


class ShippedLibrary(unittest.TestCase):
    """The four templates that ship, read from the repo."""

    def setUp(self):
        self.templates = L.list_templates(REPO_LIBRARY)

    def test_the_four_are_present(self):
        self.assertEqual(
            sorted(t["id"] for _, t in self.templates),
            ["competitor-watch", "industry-brief", "source-scan", "website-topic-map"],
        )

    def test_every_shipped_template_is_valid(self):
        for path, template in self.templates:
            with self.subTest(template=os.path.basename(path)):
                self.assertEqual(L.validate_template(template, filename=path), [])

    def test_none_of_them_can_run_unconfigured(self):
        """The point of the whole format, asserted against what actually ships.

        Not "is disabled" — cannot be rendered at all. Every required parameter
        is named in the refusal, because an operator filling a form should be
        told about all of them at once rather than one per attempt.
        """
        for path, template in self.templates:
            with self.subTest(template=os.path.basename(path)):
                required = [p["name"] for p in template["parameters"] if p["required"]]
                self.assertTrue(required, "a template with no required parameters could run as-is")
                with self.assertRaises(L.TemplateError) as caught:
                    L.render_job(template, {})
                named = " ".join(caught.exception.problems)
                for name in required:
                    self.assertIn(name, named)

    def test_at_least_one_uses_scheduler_level_change_detection(self):
        """`prompt` mechanisms trust the model to remember. One must not.

        docs/cookbook/competitor-watch.md is explicit that a repeated summary
        is worse than no automation, so the library has to contain at least one
        template whose change detection does not depend on recall at all.
        """
        monitors = [
            t for _, t in self.templates
            if t["change_detection"]["mechanism"] == "monitor"
        ]
        self.assertTrue(monitors)
        for t in monitors:
            self.assertTrue(t["job"].get("monitor_url") or t["job"].get("monitor_script"))

    def test_every_template_declares_how_to_verify_its_change_detection(self):
        for _, t in self.templates:
            with self.subTest(template=t["id"]):
                self.assertTrue(str(t["change_detection"].get("verify", "")).strip())

    def test_none_of_them_names_a_company(self):
        """Client-agnostic is the reason this is a library and not four jobs.

        Example values may name a fictional company; the prompt may not.
        """
        for _, t in self.templates:
            with self.subTest(template=t["id"]):
                self.assertNotIn("Acme", t["job"]["prompt"])


class RenderedJobs(unittest.TestCase):
    def test_a_filled_template_renders_create_job_kwargs(self):
        template = L.load_template(os.path.join(REPO_LIBRARY, "source-scan.yaml"))
        kwargs = L.render_job(
            template,
            {
                "source_url": "https://example.com/changelog",
                "source_name": "A changelog",
                "interest": "pricing",
            },
        )
        self.assertLessEqual(set(kwargs), CREATE_JOB_KWARGS)
        self.assertEqual(kwargs["schedule"], "0 9 * * 1")
        self.assertEqual(kwargs["deliver"], "origin")
        self.assertEqual(kwargs["monitor_url"], "https://example.com/changelog")
        self.assertNotIn("{{", kwargs["prompt"])
        # Nothing that would make it a running job comes out of here.
        self.assertNotIn("enabled", kwargs)
        self.assertNotIn("id", kwargs)

    def test_lists_and_booleans_render_as_prose(self):
        template = L.load_template(os.path.join(REPO_LIBRARY, "competitor-watch.yaml"))
        kwargs = L.render_job(template, {"competitors": ["Acme", "Northwind"], "include_news": True})
        self.assertIn("Acme, Northwind", kwargs["prompt"])
        self.assertIn("coverage as well: yes", kwargs["prompt"])

    def test_optional_parameters_fall_back_to_their_defaults(self):
        template = L.load_template(os.path.join(REPO_LIBRARY, "competitor-watch.yaml"))
        kwargs = L.render_job(template, {"competitors": ["Acme"]})
        self.assertIn("pricing, changelog", kwargs["prompt"])
        self.assertIn("as well: no", kwargs["prompt"])

    def test_a_wrong_type_is_refused(self):
        template = L.load_template(os.path.join(REPO_LIBRARY, "source-scan.yaml"))
        with self.assertRaises(L.TemplateError) as caught:
            L.render_job(
                template,
                {"source_url": "example.com", "source_name": "x", "interest": "y"},
            )
        self.assertIn("http(s) URL", " ".join(caught.exception.problems))

    def test_an_empty_string_does_not_satisfy_a_required_parameter(self):
        template = L.load_template(os.path.join(REPO_LIBRARY, "industry-brief.yaml"))
        with self.assertRaises(L.TemplateError):
            L.render_job(template, {"industry": "   ", "themes": ["pricing"]})

    def test_an_undeclared_value_is_refused(self):
        """A typo in a parameter name must not be silently ignored."""
        template = L.load_template(os.path.join(REPO_LIBRARY, "source-scan.yaml"))
        with self.assertRaises(L.TemplateError) as caught:
            L.render_job(
                template,
                {
                    "source_url": "https://example.com",
                    "source_name": "x",
                    "interest": "y",
                    "sorce_name": "typo",
                },
            )
        self.assertIn("sorce_name", " ".join(caught.exception.problems))

    def test_the_schedule_can_be_overridden_but_not_invented(self):
        template = L.load_template(os.path.join(REPO_LIBRARY, "industry-brief.yaml"))
        kwargs = L.render_job(
            template, {"industry": "logistics", "themes": ["pricing"]}, schedule="every 6h"
        )
        self.assertEqual(kwargs["schedule"], "every 6h")
        with self.assertRaises(L.TemplateError):
            L.render_job(
                template, {"industry": "logistics", "themes": ["pricing"]}, schedule="on Tuesdays"
            )


class TemplateValidation(unittest.TestCase):
    def one_problem(self, template, fragment):
        problems = L.validate_template(template)
        self.assertTrue(problems, "expected a problem, got none")
        self.assertIn(fragment, " ".join(problems))

    def test_the_baseline_is_valid(self):
        self.assertEqual(L.validate_template(minimal()), [])

    def test_a_required_parameter_may_not_have_a_default(self):
        t = minimal()
        t["parameters"][0]["default"] = "widgets"
        self.one_problem(t, "must not have a default")

    def test_an_optional_parameter_must_have_one(self):
        t = minimal()
        t["parameters"][0]["required"] = False
        self.one_problem(t, "need a default")

    def test_a_parameter_needs_a_question(self):
        t = minimal()
        del t["parameters"][0]["ask"]
        self.one_problem(t, "needs an `ask`")

    def test_a_template_may_not_carry_job_state(self):
        for key, value in (("enabled", False), ("id", "abc"), ("next_run_at", "2026-01-01")):
            with self.subTest(key=key):
                t = minimal()
                t["job"][key] = value
                self.one_problem(t, "a template is not a job")

    def test_a_template_may_not_ship_a_script(self):
        t = minimal()
        t["job"]["script"] = "collect.py"
        self.one_problem(t, "a template is not a job")

    def test_schedule_and_delivery_live_outside_the_job_block(self):
        t = minimal()
        t["job"]["deliver"] = "telegram"
        self.one_problem(t, "a template is not a job")

    def test_an_unknown_job_key_is_refused(self):
        t = minimal()
        t["job"]["temperature"] = 0.4
        self.one_problem(t, "not an argument of cron.jobs.create_job")

    def test_a_placeholder_must_name_a_parameter(self):
        t = minimal()
        t["job"]["prompt"] = "Watch {{subject}}. [SILENT]"
        self.one_problem(t, "is not a declared parameter")

    def test_a_parameter_must_be_used(self):
        t = minimal()
        t["parameters"].append(
            {"name": "unused", "type": "string", "required": False, "default": "x", "ask": "?"}
        )
        self.one_problem(t, "never used")

    def test_there_is_no_template_engine(self):
        """The syntax someone reaches for when the format looks like Jinja."""
        t = minimal()
        t["job"]["prompt"] = "Watch {{topic}}. {% if topic %}x{% endif %} {{#topic}} [SILENT]"
        self.one_problem(t, "malformed placeholder")

    def test_prompt_change_detection_needs_the_silent_contract(self):
        t = minimal()
        t["job"]["prompt"] = "Watch {{topic}}. Only tell me what is new."
        self.one_problem(t, "[SILENT] contract")

    def test_monitor_change_detection_needs_something_to_monitor(self):
        t = minimal()
        t["change_detection"] = {"mechanism": "monitor", "memory": "monitor_state", "verify": "twice"}
        self.one_problem(t, "neither monitor_url nor monitor_script")

    def test_change_detection_must_say_how_to_check_it(self):
        t = minimal()
        del t["change_detection"]["verify"]
        self.one_problem(t, "verify is required")

    def test_no_change_detection_must_be_justified(self):
        t = minimal()
        t["change_detection"] = {"mechanism": "none"}
        self.one_problem(t, "say why")

    def test_an_unparseable_schedule_default_is_caught_here(self):
        t = minimal()
        t["schedule"]["default"] = "weekly"
        self.one_problem(t, "not a schedule")

    def test_the_id_must_match_the_filename(self):
        problems = L.validate_template(minimal(), filename="/x/something-else.yaml")
        self.assertIn("does not match filename", " ".join(problems))


class Schedules(unittest.TestCase):
    """Only the four forms cron.jobs.parse_schedule accepts."""

    def test_accepted(self):
        for s in ("0 8 * * 1", "*/15 * * * *", "every 30m", "every 2h", "2h", "2026-02-03T14:00"):
            self.assertTrue(L.schedule_looks_valid(s), s)

    def test_refused(self):
        for s in ("", "weekly", "every monday", "0 8 * *", None, 8):
            self.assertFalse(L.schedule_looks_valid(s), s)


class CommandLine(unittest.TestCase):
    def test_check_passes_on_the_shipped_library(self):
        self.assertEqual(L.main(["automation_library.py", "--check", REPO_LIBRARY]), 0)

    def test_check_fails_on_a_broken_one(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            broken = minimal()
            broken["job"]["enabled"] = True
            with open(os.path.join(tmp, "example.yaml"), "w", encoding="utf-8") as fh:
                yaml.safe_dump(broken, fh)
            self.assertEqual(L.main(["automation_library.py", "--check", tmp]), 1)


if __name__ == "__main__":
    unittest.main()
