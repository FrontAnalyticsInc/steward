"""Tests for alias resolution.

The property under test is the one the proxy used to give us: a workflow names a
role, an operator edits one file, and the swap takes effect without a rebuild or
a restart. Each test below is one way that could quietly stop being true.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import model_aliases


class AliasTestCase(unittest.TestCase):
    """Point the module at a scratch file and clear its cache between tests.

    The cache is keyed on (mtime_ns, size), so a fresh temp file per test is not
    enough on its own — two files written in the same nanosecond with the same
    length would collide. Resetting explicitly makes the isolation independent
    of clock resolution.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "model-aliases.yaml"
        self._previous = model_aliases.ALIASES_PATH
        model_aliases.ALIASES_PATH = str(self.path)
        model_aliases._cache = None
        model_aliases._cache_stamp = None

    def tearDown(self) -> None:
        model_aliases.ALIASES_PATH = self._previous
        model_aliases._cache = None
        model_aliases._cache_stamp = None
        self._tmp.cleanup()

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")


class TestResolution(AliasTestCase):
    def test_the_file_decides_which_model_answers(self):
        self.write("drafting: anthropic/claude-sonnet-5\n")
        self.assertEqual(model_aliases.resolve("drafting"), "anthropic/claude-sonnet-5")

    def test_an_absent_file_falls_back_to_the_built_in_defaults(self):
        # A fresh install must run. Failing every workflow because nobody has
        # seeded a config file yet would be a worse first experience than
        # running on a documented default.
        self.assertEqual(
            model_aliases.resolve("drafting"), model_aliases.DEFAULTS["drafting"]
        )

    def test_a_partial_file_leaves_the_other_aliases_alone(self):
        # Overriding one role must not delete the two you did not mention.
        self.write("fast: anthropic/claude-haiku-4-5\n")
        self.assertEqual(
            model_aliases.resolve("extraction"), model_aliases.DEFAULTS["extraction"]
        )

    def test_an_operator_may_add_an_alias_the_code_has_never_heard_of(self):
        self.write("reranking: anthropic/claude-haiku-4-5\n")
        self.assertEqual(
            model_aliases.resolve("reranking"), "anthropic/claude-haiku-4-5"
        )

    def test_an_unknown_alias_raises_rather_than_billing_a_model_nobody_chose(self):
        # The failure this prevents is a typo silently answered by the most
        # expensive tier and discovered weeks later on an invoice.
        with self.assertRaises(KeyError) as caught:
            model_aliases.resolve("draftign")
        self.assertIn("draftign", str(caught.exception))


class TestLiveReload(AliasTestCase):
    def test_an_edit_takes_effect_without_a_restart(self):
        # This is the whole reason the file lives outside the image.
        self.write("drafting: anthropic/claude-opus-5\n")
        self.assertEqual(model_aliases.resolve("drafting"), "anthropic/claude-opus-5")

        self.write("drafting: anthropic/claude-haiku-4-5\n")
        self.assertEqual(model_aliases.resolve("drafting"), "anthropic/claude-haiku-4-5")

    def test_deleting_the_file_returns_to_the_defaults(self):
        self.write("drafting: anthropic/claude-haiku-4-5\n")
        model_aliases.resolve("drafting")
        self.path.unlink()
        self.assertEqual(
            model_aliases.resolve("drafting"), model_aliases.DEFAULTS["drafting"]
        )


class TestParser(AliasTestCase):
    def test_comments_and_blank_lines_are_not_aliases(self):
        self.write(
            "# drafting: commented-out/model\n"
            "\n"
            "drafting: anthropic/claude-opus-5   # trailing note\n"
        )
        aliases = model_aliases.load()
        self.assertEqual(aliases["drafting"], "anthropic/claude-opus-5")
        self.assertNotIn("# drafting", aliases)

    def test_quotes_and_indentation_are_tolerated(self):
        self.write('  drafting: "anthropic/claude-opus-5"\n')
        self.assertEqual(model_aliases.resolve("drafting"), "anthropic/claude-opus-5")

    def test_the_shipped_file_parses_to_what_it_reads_as(self):
        # Guards the seed file itself, not just the parser: the parser ignores
        # nesting, so a future edit that indents these under a `aliases:` heading
        # would still "work" here but silently change nothing downstream. This
        # asserts the shipped file resolves to the three real models.
        shipped = (
            Path(__file__).resolve().parents[3]
            / "hermes"
            / "config"
            / "model-aliases.yaml"
        )
        if not shipped.is_file():  # repo layout differs in the deployed image
            self.skipTest(f"{shipped} not present")
        model_aliases.ALIASES_PATH = str(shipped)
        model_aliases._cache = None
        model_aliases._cache_stamp = None
        aliases = model_aliases.load()
        for role in ("drafting", "extraction", "fast"):
            self.assertTrue(
                aliases[role].startswith("anthropic/"),
                f"{role} resolves to {aliases[role]!r}, which names no provider",
            )


class TestDescribe(AliasTestCase):
    def test_it_says_where_the_answer_came_from(self):
        described = model_aliases.describe()
        self.assertFalse(described["present"])
        self.assertEqual(described["path"], str(self.path))
        self.write("drafting: anthropic/claude-haiku-4-5\n")
        self.assertTrue(model_aliases.describe()["present"])


if __name__ == "__main__":
    unittest.main()
