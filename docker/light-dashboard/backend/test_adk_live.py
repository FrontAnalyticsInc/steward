"""Tests for how /list-apps entries are folded into the team list.

Standard library only, matching test_integrations.py — see its docstring for
why these stay that way now that `pytest` (run from docker/light-dashboard/)
collects them alongside everything else. `python3 -m unittest
backend.test_adk_live` still works.

The property under test is that a workflow is visible whatever its root class.
Folding an app into whichever other app happened to list its agents made
visibility depend on an ADK introspection limit: an LlmAgent root was absorbed
into the routing root and disappeared, while a SequentialAgent root survived
only because app-info cannot describe it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from . import adk_live as L


def team(app, names, root=None):
    """The shape _app_info/_from_source return, reduced to what folding reads."""
    return {
        "app": app,
        "root": root or (sorted(names)[0] if names else None),
        "description": None,
        "agents": [{"name": n} for n in sorted(names)],
        "_names": set(names),
    }


class FetchTeams(unittest.TestCase):
    def setUp(self):
        self._get, self._info, self._src = L._get, L._app_info, L._from_source
        self._enrich = L._enrich_classes
        # Identity: the class-enrichment pass reads the mounted source, which is
        # not what these tests are about.
        L._enrich_classes = lambda t: t

    def tearDown(self):
        L._get, L._app_info, L._from_source = self._get, self._info, self._src
        L._enrich_classes = self._enrich

    def _serve(self, listed, described):
        L._get = lambda url, timeout=None: list(listed)
        L._app_info = lambda base, app: described.get(app)
        L._from_source = lambda app: described.get(app)

    def test_a_routed_workflow_is_a_team_not_a_footnote(self):
        """The regression: an LlmAgent workflow used to vanish into the root."""
        self._serve(
            ["app", "app.agents.calendar_daily_briefing"],
            {
                "app": team("app", {"root_agent", "calendar_daily_briefing"}),
                "app.agents.calendar_daily_briefing": team(
                    "app.agents.calendar_daily_briefing", {"calendar_daily_briefing"}),
            },
        )
        out = {t["app"]: t for t in L.fetch_teams("http://x", "workflows")}
        self.assertIn("app.agents.calendar_daily_briefing", out)
        self.assertEqual(out["app"]["also_invocable_as"], [])

    def test_the_routing_root_says_it_is_one(self):
        self._serve(
            ["app", "app.agents.a", "app.agents.b"],
            {
                "app": team("app", {"root_agent", "a", "b"}),
                "app.agents.a": team("app.agents.a", {"a"}),
                "app.agents.b": team("app.agents.b", {"b"}),
            },
        )
        out = {t["app"]: t for t in L.fetch_teams("http://x", "workflows")}
        self.assertTrue(out["app"]["router"])
        self.assertEqual(out["app"]["routes_to"], ["app.agents.a", "app.agents.b"])
        # And the things it routes to are not themselves routers.
        self.assertFalse(out["app.agents.a"]["router"])

    def test_aliases_still_fold(self):
        """Two names for the identical agent set is a duplicate, not a route."""
        self._serve(
            ["app.agents.a", "alias.a"],
            {
                "app.agents.a": team("app.agents.a", {"a"}),
                "alias.a": team("alias.a", {"a"}),
            },
        )
        out = L.fetch_teams("http://x", "workflows")
        # Which of two equal names wins is an alphabetical tie-break and not
        # worth pinning; that exactly one survives and names the other is.
        self.assertEqual(len(out), 1)
        self.assertEqual(
            {out[0]["app"], *out[0]["also_invocable_as"]},
            {"app.agents.a", "alias.a"},
        )

    def test_a_pipeline_the_root_cannot_describe_is_not_a_router(self):
        """Its stages are its own, not apps it routes to."""
        self._serve(
            ["app", "app.agents.triage"],
            {
                "app": team("app", {"root_agent", "other"}),
                "app.agents.triage": team(
                    "app.agents.triage", {"triage", "fetch", "classify"}),
            },
        )
        out = {t["app"]: t for t in L.fetch_teams("http://x", "workflows")}
        self.assertFalse(out["app.agents.triage"]["router"])
        self.assertFalse(out["app"]["router"])

    def test_every_team_carries_the_router_keys(self):
        self._serve(["app.agents.a"], {"app.agents.a": team("app.agents.a", {"a"})})
        out = L.fetch_teams("http://x", "workflows")
        self.assertEqual((out[0]["router"], out[0]["routes_to"]), (False, []))

    def test_an_unreachable_server_still_yields_a_card(self):
        L._get = lambda url, timeout=None: None
        out = L.fetch_teams("http://x", "workflows")
        self.assertEqual(out[0]["status"], "error")


# A tenant-authored pipeline, in the shape the real ones on a deployed box
# actually have — transcribed from one of them rather than invented, because a
# plausible-looking stub is exactly what would have passed while the real thing
# stayed invisible. What matters structurally, and what a stub tends to get
# wrong:
#
#   * the root is a `SequentialAgent`, so `GET /apps/<app>/app-info` answers 400
#     and the live route cannot describe it at all;
#   * the stages are custom BaseAgent subclasses imported from a sibling
#     `stages.py`, not literals in `agent.py`;
#   * `root_agent` is an alias of a differently-named variable;
#   * the app registers under `agents_local.<dir>`, NOT `app.agents.<dir>` —
#     which is precisely the prefix the old `_source_dir` refused.
PIPELINE_AGENT_PY = '''\
"""daily_digest — read a source, draft a digest, deliver it."""

from __future__ import annotations

from google.adk.agents import LlmAgent, SequentialAgent

from app.config import build_model

from .prompt import INSTRUCTION
from .schema import DigestDraft
from .stages import DeliverDigestAgent, EmitResultAgent, FetchSourceAgent

AGENT_NAME = "daily_digest"

fetch_source_agent = FetchSourceAgent(name="fetch_source")

draft_digest_agent = LlmAgent(
    name="draft_digest",
    model=build_model(),
    description="Drafts a digest from the fetched source material.",
    instruction=INSTRUCTION,
    output_schema=DigestDraft,
    output_key="digest_draft",
)

deliver_digest_agent = DeliverDigestAgent(name="deliver_digest")

emit_result_agent = EmitResultAgent(name="emit_result")

daily_digest_agent = SequentialAgent(
    name=AGENT_NAME,
    description="Reads a source, drafts a digest, and delivers it.",
    sub_agents=[
        fetch_source_agent,
        draft_digest_agent,
        deliver_digest_agent,
        emit_result_agent,
    ],
)

root_agent = daily_digest_agent
'''


class TenantAuthoredPipeline(unittest.TestCase):
    """agents_local is the supported extension point. It has to be describable.

    Its agents RUN — the workflows service mounts them and ADK loads them — so
    the failure was silent and one-sided: the box did the work and the console
    could not say what the work was. An automation page drew "Loading steps..."
    forever, because no team ever matched and none ever would.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agents_local_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        pkg = os.path.join(self.tmp, "daily_digest")
        os.makedirs(pkg)
        with open(os.path.join(pkg, "agent.py"), "w", encoding="utf-8") as fh:
            fh.write(PIPELINE_AGENT_PY)
        open(os.path.join(pkg, "__init__.py"), "w").close()

        self._roots = (L.WORKFLOWS_SRC_DIR, L.AGENTS_LOCAL_SRC_DIR, L.ADK_SRC_ROOTS)
        L.AGENTS_LOCAL_SRC_DIR = self.tmp

    def tearDown(self):
        L.WORKFLOWS_SRC_DIR, L.AGENTS_LOCAL_SRC_DIR, L.ADK_SRC_ROOTS = self._roots

    def test_the_prefix_resolves_to_the_mounted_directory(self):
        self.assertEqual(
            L._source_dir("agents_local.daily_digest"),
            os.path.join(self.tmp, "daily_digest"),
        )

    def test_a_sequential_root_appears_with_its_steps(self):
        """The whole task, end to end through fetch_teams.

        app-info is made to refuse it, which is what a real ADK server does for
        a SequentialAgent root. Source is not a fallback here, it is the only
        route — so an empty result means the agent is invisible in the console.
        """
        info, get = L._app_info, L._get
        self.addCleanup(lambda: setattr(L, "_app_info", info))
        self.addCleanup(lambda: setattr(L, "_get", get))
        L._get = lambda url, timeout=None: ["agents_local.daily_digest"]
        L._app_info = lambda base, app: None

        teams = L.fetch_teams("http://x", "workflows")
        self.assertEqual([t["app"] for t in teams], ["agents_local.daily_digest"])
        team = teams[0]
        self.assertEqual(team["source"], "source")
        self.assertEqual(team["root"], "daily_digest")
        self.assertEqual(
            [a["name"] for a in team["agents"]],
            ["daily_digest", "fetch_source", "draft_digest",
             "deliver_digest", "emit_result"],
        )
        # The steps, in order, are what the automation page draws.
        self.assertEqual([a["depth"] for a in team["agents"]], [0, 1, 1, 1, 1])
        root = team["agents"][0]
        self.assertEqual(root["agent_class"], "SequentialAgent")
        self.assertTrue(root["is_workflow"])

    def test_a_third_extension_point_needs_no_code_change(self):
        """The map is data. That is the actual fix.

        One hardcoded prefix is what broke this; two would be the same bug
        waiting for a third. A new source of agents is a mount and a variable.
        """
        L.ADK_SRC_ROOTS = "partner_agents=" + self.tmp
        self.assertEqual(
            L._source_dir("partner_agents.daily_digest"),
            os.path.join(self.tmp, "daily_digest"),
        )

    def test_an_unknown_prefix_is_still_refused(self):
        self.assertIsNone(L._source_dir("whatever.daily_digest"))
        # And a root that is not mounted is unknown, not empty.
        L.AGENTS_LOCAL_SRC_DIR = os.path.join(self.tmp, "nope")
        self.assertIsNone(L._source_dir("agents_local.daily_digest"))


if __name__ == "__main__":
    unittest.main()
