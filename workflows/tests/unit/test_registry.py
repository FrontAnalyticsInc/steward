"""Discovery replaces the hand-maintained WORKFLOW_AGENTS list.

What matters here is that the set of registered agents is exactly what it was
under the old list, and that the ways discovery can go wrong are loud rather
than quiet. An agent that silently fails to register is the failure mode the
old list already produced, and it is expensive: the workflow imports, its
runner script works, and it is simply missing from the playground.
"""

from __future__ import annotations

import pathlib
import types

import pytest

from app import registry as R

# Exactly the contents of the hand-written WORKFLOW_AGENTS list that discovery
# replaced. Pinned deliberately: this is the assertion that the change did not
# quietly drop or add a workflow.
#
# Note what this set does NOT contain, because it is the one behaviour change.
# Discovery registers what is on disk, whereas the old list registered what
# someone had remembered to add to it. A directory that exists but was never
# in the list — including an untracked one, since the container bind-mounts the
# working tree rather than the committed tree — now becomes a live workflow.
# HERMES_DISABLED_AGENTS is the way to say no.
BUILTIN_AGENTS = {
    "intentional_failure_demo",
    "summarize_note",
}


def _names(modules) -> set[str]:
    return {path.name for _, path in modules}


def _make_agent_dir(root: pathlib.Path, name: str) -> pathlib.Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("")
    return d


class TestBuiltinDiscovery:
    def test_finds_every_shipped_agent(self, monkeypatch):
        """No committed agent goes missing.

        Subset, not equality. The container bind-mounts the working tree, so a
        directory someone has on disk but has not committed IS a live workflow
        on that machine — legitimately, and invisibly to every other clone.
        Equality would fail the suite for whoever has scratch work checked out,
        which teaches people to delete the assertion rather than read it.
        Extras are governed by HERMES_DISABLED_AGENTS, pinned in
        test_an_agent_dir_not_in_the_old_list_would_now_register; what THIS
        test guards is the opposite direction, a shipped agent silently
        dropping out of discovery.
        """
        monkeypatch.delenv(R.AGENTS_PATH_ENV, raising=False)
        monkeypatch.delenv(R.DISABLED_ENV, raising=False)
        assert BUILTIN_AGENTS <= _names(R.find_agent_modules())

    def test_builtins_import_under_the_app_package(self, monkeypatch):
        monkeypatch.delenv(R.AGENTS_PATH_ENV, raising=False)
        monkeypatch.delenv(R.DISABLED_ENV, raising=False)
        modules = dict((path.name, mod) for mod, path in R.find_agent_modules())
        assert modules["summarize_note"] == "app.agents.summarize_note"

    def test_order_is_deterministic(self, monkeypatch):
        """The root agent's prompt is built from this order; an unstable one
        would change routing between runs for no reason."""
        monkeypatch.delenv(R.AGENTS_PATH_ENV, raising=False)
        monkeypatch.delenv(R.DISABLED_ENV, raising=False)
        first = [m for m, _ in R.find_agent_modules()]
        assert first == sorted(first)
        assert first == [m for m, _ in R.find_agent_modules()]

    def test_an_agent_dir_not_in_the_old_list_would_now_register(self, tmp_path, monkeypatch):
        """The one behaviour change, pinned so it stays intentional.

        Under the old list a directory could sit in app/agents/ and never run,
        because registration was a separate act of remembering. Under discovery
        presence IS registration.
        """
        _make_agent_dir(tmp_path, "never_was_in_the_list")
        monkeypatch.setenv(R.AGENTS_PATH_ENV, str(tmp_path))
        monkeypatch.delenv(R.DISABLED_ENV, raising=False)
        assert "never_was_in_the_list" in _names(R.find_agent_modules())

        monkeypatch.setenv(R.DISABLED_ENV, "never_was_in_the_list")
        assert "never_was_in_the_list" not in _names(R.find_agent_modules())

    def test_skips_pycache_and_private_dirs(self, monkeypatch):
        monkeypatch.delenv(R.AGENTS_PATH_ENV, raising=False)
        monkeypatch.delenv(R.DISABLED_ENV, raising=False)
        names = _names(R.find_agent_modules())
        assert not any(n.startswith(("_", ".")) for n in names)


class TestDisabling:
    def test_disabled_agent_is_not_registered(self, monkeypatch):
        monkeypatch.delenv(R.AGENTS_PATH_ENV, raising=False)
        monkeypatch.setenv(R.DISABLED_ENV, "intentional_failure_demo")
        names = _names(R.find_agent_modules())
        assert "intentional_failure_demo" not in names
        assert "summarize_note" in names

    def test_disable_list_tolerates_spacing_and_blanks(self, monkeypatch):
        monkeypatch.delenv(R.AGENTS_PATH_ENV, raising=False)
        monkeypatch.setenv(R.DISABLED_ENV, " intentional_failure_demo , ,summarize_note ")
        names = _names(R.find_agent_modules())
        assert "intentional_failure_demo" not in names and "summarize_note" not in names


class TestOverlay:
    def test_overlay_agents_are_discovered(self, tmp_path, monkeypatch):
        _make_agent_dir(tmp_path, "tenant_workflow")
        monkeypatch.setenv(R.AGENTS_PATH_ENV, str(tmp_path))
        monkeypatch.delenv(R.DISABLED_ENV, raising=False)
        assert "tenant_workflow" in _names(R.find_agent_modules())

    def test_overlay_module_is_top_level_not_under_app(self, tmp_path, monkeypatch):
        _make_agent_dir(tmp_path, "tenant_workflow")
        monkeypatch.setenv(R.AGENTS_PATH_ENV, str(tmp_path))
        monkeypatch.delenv(R.DISABLED_ENV, raising=False)
        modules = dict((p.name, m) for m, p in R.find_agent_modules())
        assert modules["tenant_workflow"] == "tenant_workflow"

    def test_overlay_overrides_a_shipped_agent_of_the_same_name(self, tmp_path, monkeypatch):
        """How a tenant replaces a shipped workflow without forking it."""
        _make_agent_dir(tmp_path, "summarize_note")
        monkeypatch.setenv(R.AGENTS_PATH_ENV, str(tmp_path))
        monkeypatch.delenv(R.DISABLED_ENV, raising=False)
        modules = dict((p.name, p) for _, p in R.find_agent_modules())
        assert modules["summarize_note"].parent == tmp_path

    def test_multiple_overlay_dirs(self, tmp_path, monkeypatch):
        a, b = tmp_path / "a", tmp_path / "b"
        _make_agent_dir(a, "from_a")
        _make_agent_dir(b, "from_b")
        import os

        monkeypatch.setenv(R.AGENTS_PATH_ENV, os.pathsep.join([str(a), str(b)]))
        monkeypatch.delenv(R.DISABLED_ENV, raising=False)
        names = _names(R.find_agent_modules())
        assert {"from_a", "from_b"} <= names

    def test_absent_overlay_is_not_an_error(self, tmp_path, monkeypatch):
        """A configured mount that this deployment does not have is normal."""
        monkeypatch.setenv(R.AGENTS_PATH_ENV, str(tmp_path / "nope"))
        monkeypatch.delenv(R.DISABLED_ENV, raising=False)
        assert _names(R.find_agent_modules()) == BUILTIN_AGENTS

    def test_directory_without_init_is_not_an_agent(self, tmp_path, monkeypatch):
        (tmp_path / "just_a_folder").mkdir()
        monkeypatch.setenv(R.AGENTS_PATH_ENV, str(tmp_path))
        monkeypatch.delenv(R.DISABLED_ENV, raising=False)
        assert "just_a_folder" not in _names(R.find_agent_modules())


class TestLoading:
    """The import half. Built-ins are disabled so these exercise the loader
    itself without dragging in ADK, a model config, or a credential."""

    def test_loads_an_overlay_agent(self, tmp_path, monkeypatch):
        d = _make_agent_dir(tmp_path, "tenant_alpha")
        (d / "__init__.py").write_text("root_agent = 'ALPHA'\n")
        monkeypatch.setenv(R.AGENTS_PATH_ENV, str(tmp_path))
        monkeypatch.setenv(R.DISABLED_ENV, ",".join(BUILTIN_AGENTS))
        assert R.load_agents() == ["ALPHA"]

    def test_a_broken_overlay_agent_is_skipped_and_recorded(self, tmp_path, monkeypatch):
        """Skipped, but never silently — which was the whole point originally.

        Under the old hand-written list a broken agent was simply absent from
        the playground with no error to follow. It is still absent; the
        difference is that `load_errors()` says so and names it, and the rest
        of the box keeps working while its author fixes one file.
        """
        d = _make_agent_dir(tmp_path, "tenant_broken")
        (d / "__init__.py").write_text("raise ValueError('boom')\n")
        monkeypatch.setenv(R.AGENTS_PATH_ENV, str(tmp_path))
        monkeypatch.setenv(R.DISABLED_ENV, ",".join(BUILTIN_AGENTS))

        assert R.load_agents() == []
        assert "tenant_broken" in R.load_errors()
        assert "boom" in R.load_errors()["tenant_broken"]

    def test_one_broken_overlay_does_not_take_the_others_down(
        self, tmp_path, monkeypatch
    ):
        """The reason this is not simply `raise`.

        An operator authoring on a live box gets one file wrong. Refusing to
        start would take every other workflow, the playground and the review
        queue with it.
        """
        broken = _make_agent_dir(tmp_path, "tenant_broken")
        broken.joinpath("__init__.py").write_text("raise ValueError('boom')\n")
        ok = _make_agent_dir(tmp_path, "tenant_fine")
        ok.joinpath("__init__.py").write_text("root_agent = 'FINE'\n")
        monkeypatch.setenv(R.AGENTS_PATH_ENV, str(tmp_path))
        monkeypatch.setenv(R.DISABLED_ENV, ",".join(BUILTIN_AGENTS))

        assert R.load_agents() == ["FINE"]
        assert list(R.load_errors()) == ["tenant_broken"]

    def test_an_overlay_exporting_nothing_is_skipped_and_recorded(
        self, tmp_path, monkeypatch
    ):
        _make_agent_dir(tmp_path, "tenant_empty")
        monkeypatch.setenv(R.AGENTS_PATH_ENV, str(tmp_path))
        monkeypatch.setenv(R.DISABLED_ENV, ",".join(BUILTIN_AGENTS))

        assert R.load_agents() == []
        assert "exports no" in R.load_errors()["tenant_empty"]

    def test_a_clean_load_reports_no_errors(self, tmp_path, monkeypatch):
        """Stale errors would be worse than none: the console would show a
        failure the operator had already fixed."""
        d = _make_agent_dir(tmp_path, "tenant_broken")
        (d / "__init__.py").write_text("raise ValueError('boom')\n")
        monkeypatch.setenv(R.AGENTS_PATH_ENV, str(tmp_path))
        monkeypatch.setenv(R.DISABLED_ENV, ",".join(BUILTIN_AGENTS))
        R.load_agents()
        assert R.load_errors()

        (d / "__init__.py").write_text("root_agent = 'FIXED'\n")
        # The failed import left nothing in sys.modules to invalidate, but the
        # parent directory is already on sys.path from the first call — which
        # is exactly the state a restart would NOT be in, so assert against the
        # harder case rather than the convenient one.
        assert R.load_agents() == ["FIXED"]
        assert R.load_errors() == {}

    def test_a_broken_builtin_still_raises(self, tmp_path, monkeypatch):
        """The asymmetry, asserted directly.

        A shipped agent that will not load means the release is wrong, and
        every deployment has the same one. Starting anyway would ship a box
        that is quietly missing a workflow we told the customer they had.

        Driven through find_agent_modules rather than by writing a broken
        package into app/agents: the branch under test keys off the module
        name, so naming one is enough, and a test that creates real files
        inside the shipped agent directory leaves debris there when it fails.
        """
        monkeypatch.setattr(
            R,
            "find_agent_modules",
            lambda: [("app.agents.no_such_builtin", tmp_path)],
        )
        with pytest.raises(R.AgentLoadError, match="no_such_builtin"):
            R.load_agents()

    def test_load_order_matches_discovery_order(self, tmp_path, monkeypatch):
        for name in ("tenant_c", "tenant_a", "tenant_b"):
            d = _make_agent_dir(tmp_path, name)
            (d / "__init__.py").write_text(f"root_agent = {name!r}\n")
        monkeypatch.setenv(R.AGENTS_PATH_ENV, str(tmp_path))
        monkeypatch.setenv(R.DISABLED_ENV, ",".join(BUILTIN_AGENTS))
        assert R.load_agents() == ["tenant_a", "tenant_b", "tenant_c"]


class TestAgentExportResolution:
    """Which object a module registers. Wrong answers here are silent."""

    def test_root_agent_wins(self):
        mod = types.SimpleNamespace(root_agent="ROOT", other_agent="OTHER")
        assert R._agent_of(mod, "m") == "ROOT"

    def test_single_suffixed_export_is_used(self):
        mod = types.SimpleNamespace(summarize_note_agent="DRAFT")
        assert R._agent_of(mod, "m") == "DRAFT"

    def test_no_export_is_an_error_not_a_skip(self):
        with pytest.raises(R.AgentLoadError, match="exports no"):
            R._agent_of(types.SimpleNamespace(), "m")

    def test_ambiguous_exports_refuse_to_guess(self):
        mod = types.SimpleNamespace(a_agent=1, b_agent=2)
        with pytest.raises(R.AgentLoadError, match="several agents"):
            R._agent_of(mod, "m")
