"""What the Context tab lists, and — more importantly — what it does not.

The tab walks the whole Hermes home for markdown, which makes it a faithful
picture of the directory and a misleading picture of the agent. Only SOUL.md
from HERMES_HOME reaches the system prompt; everything else here is a file that
happens to live nearby. Two directories have already had to be excluded after
they buried the identity file, so the exclusions are pinned rather than left to
whoever next adds a markdown-shaped store under the home.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from . import main


class ContextFileListingTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "SOUL.md").write_text("identity", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _names(self):
        return {row["rel_path"] for row in main.list_context_files_for(str(self.home))}

    def _write(self, relative: str, text: str = "x"):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_the_identity_file_is_listed_first(self):
        self._write("USER.md")
        rows = main.list_context_files_for(str(self.home))
        self.assertTrue(rows[0]["is_soul"])

    def test_an_ordinary_markdown_file_is_listed(self):
        self._write("USER.md")
        self.assertIn("USER.md", self._names())

    def test_the_memory_store_is_not_listed(self):
        """One file per contact, written by the workflows service and growing
        without bound. It has its own tab; listed here it buries SOUL.md, and it
        reads as though the agent were being fed every contact record."""
        self._write("wiki/alice@example.com.md")
        self._write("wiki/bob@example.com.md")
        self.assertEqual(self._names(), {"SOUL.md"})

    def test_cron_run_logs_are_not_listed(self):
        self._write("cron/output/run-1.md")
        self.assertEqual(self._names(), {"SOUL.md"})

    def test_a_named_profile_is_not_flattened_into_the_default_agent(self):
        self._write("profiles/dev/SOUL.md")
        self.assertEqual(self._names(), {"SOUL.md"})

    def test_a_missing_home_is_empty_rather_than_an_error(self):
        self.assertEqual(main.list_context_files_for(str(self.home / "gone")), [])


if __name__ == "__main__":
    unittest.main()
