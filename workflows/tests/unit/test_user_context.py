"""Unit tests for the operator profile injected into drafting agents.

The properties asserted here are the ones the drafting agents depend on: the
profile actually reaches the system instruction, a missing profile degrades to a
thinner prompt rather than a failed run, an edit takes effect without a restart,
and — the one that would be expensive to discover in production — that injecting
it does not disturb ADK's `{state}` templating, which every pipeline stage uses
to hand its data to the model.
"""

from __future__ import annotations

import pytest

from app import user_context

PROFILE = (
    "User is Ada Byron, President at Example Analytics. Email: ada@example.com.\n"
    "§\n"
    "User's active goals: 1) win consulting contracts; 2) publish thought "
    "leadership.\n"
)


@pytest.fixture(autouse=True)
def memories(tmp_path, monkeypatch):
    """Point the module at a scratch memory directory and clear its cache."""
    target = tmp_path / "memories"
    target.mkdir()
    monkeypatch.setenv("HERMES_MEMORIES_DIR", str(target))
    monkeypatch.delenv("HERMES_MEMORY_FILES", raising=False)
    user_context._cache.clear()
    return target


def test_facts_split_on_the_section_marker(memories):
    (memories / "USER.md").write_text(PROFILE, encoding="utf-8")
    block = user_context.user_context_block()
    assert "- User is Ada Byron, President at Example Analytics." in block
    assert "- User's active goals: 1) win consulting contracts" in block


def test_unseparated_file_is_one_fact_not_zero(memories):
    """Hand-edited profiles predate the separator; they still have to land."""
    (memories / "USER.md").write_text("User is Ada Byron.", encoding="utf-8")
    assert "- User is Ada Byron." in user_context.user_context_block()


def test_profile_is_fenced_and_marked_as_reference(memories):
    """It is written by an agent from user speech, so it is data, not orders."""
    (memories / "USER.md").write_text(PROFILE, encoding="utf-8")
    block = user_context.user_context_block()
    assert "<operator_profile>" in block and "</operator_profile>" in block
    assert "not instruction" in block


def test_missing_profile_is_empty_not_an_error(memories):
    """No mount, no profile — a thinner draft, never a failed run."""
    assert user_context.user_context_block() == ""


def test_missing_directory_is_empty_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_MEMORIES_DIR", str(tmp_path / "nope"))
    assert user_context.memories_dir() is None
    assert user_context.user_context_block() == ""


def test_edit_takes_effect_without_a_restart(memories):
    """These containers run for weeks. A cached profile would go stale."""
    path = memories / "USER.md"
    path.write_text("User is Ada Byron.", encoding="utf-8")
    assert "Ada Byron" in user_context.user_context_block()

    # Same size would still be a different mtime; make both differ so the test
    # does not depend on filesystem timestamp resolution.
    path.write_text("User is Grace Hopper, Rear Admiral.", encoding="utf-8")
    block = user_context.user_context_block()
    assert "Grace Hopper" in block and "Ada Byron" not in block


def test_extra_files_are_opt_in(memories):
    """MEMORY.md holds operational notes; it stays out unless asked for."""
    (memories / "USER.md").write_text("User is Ada Byron.", encoding="utf-8")
    (memories / "MEMORY.md").write_text("Cron jobs must use no_agent.", encoding="utf-8")
    assert "Cron jobs" not in user_context.user_context_block()


def test_extra_files_can_be_added_by_env(memories, monkeypatch):
    (memories / "USER.md").write_text("User is Ada Byron.", encoding="utf-8")
    (memories / "MEMORY.md").write_text("Prefers short subject lines.", encoding="utf-8")
    monkeypatch.setenv("HERMES_MEMORY_FILES", "USER.md,MEMORY.md")
    block = user_context.user_context_block()
    assert "Ada Byron" in block and "short subject lines" in block


def test_runaway_profile_is_truncated(memories, monkeypatch):
    monkeypatch.setattr(user_context, "MAX_CHARS", 200)
    (memories / "USER.md").write_text("x" * 5000, encoding="utf-8")
    block = user_context.user_context_block()
    assert len(block) < 400
    assert block.endswith("</operator_profile>")


def test_callback_appends_to_the_system_instruction(memories):
    """The seam the agents actually use."""
    from google.adk.models.llm_request import LlmRequest

    (memories / "USER.md").write_text(PROFILE, encoding="utf-8")
    request = LlmRequest()
    request.append_instructions(["You draft a single reply."])
    user_context.apply_user_context(None, request)

    system = request.config.system_instruction
    assert "You draft a single reply." in system
    assert "Ada Byron" in system
    # Order matters: the rules are established before the profile is offered.
    assert system.index("You draft a single reply.") < system.index("Ada Byron")


def test_callback_is_a_noop_without_a_profile(memories):
    from google.adk.models.llm_request import LlmRequest

    request = LlmRequest()
    request.append_instructions(["You draft a single reply."])
    user_context.apply_user_context(None, request)
    assert request.config.system_instruction == "You draft a single reply."


def test_profile_carries_no_braces_into_the_instruction(memories):
    """The regression that would break every pipeline.

    ADK renders `{...}` in a string instruction as session state and raises
    KeyError when it cannot resolve the name. The profile is appended by a
    before_model_callback — after templating — so a brace in USER.md is inert
    here. This asserts the rendering adds none of its own, which is what would
    turn a state-templated pipeline prompt into a KeyError at request time.
    """
    (memories / "USER.md").write_text(PROFILE, encoding="utf-8")
    block = user_context.user_context_block()
    assert "{" not in block and "}" not in block


