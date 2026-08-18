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


if __name__ == "__main__":
    unittest.main()
