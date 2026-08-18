"""The review queue is written by one user and read by another.

`hermes-workflows` runs as root; the dashboard that renders pending items runs
as uid 1000. `tempfile.mkstemp` creates 0600 and `os.replace` preserves it, so
every item this container wrote was root-owned and mode 600 — present on disk,
counted by the pipeline as queued for approval, and unreadable by the process
that shows it to a human.

A review gate whose items cannot be read is worse than no gate: the pipeline
reports the work as awaiting review, and nothing ever awaits it.
"""

from __future__ import annotations

import json
import os
import stat

from app import approvals


class TestPendingItemIsReadable:
    def test_a_written_item_is_world_readable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(approvals, "PENDING_DIR", tmp_path)
        item = approvals.email_draft(body="b", reason="r")
        path = approvals.write_pending(item)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode & stat.S_IRGRP, f"group cannot read: {oct(mode)}"
        assert mode & stat.S_IROTH, f"others cannot read: {oct(mode)}"

    def test_the_item_is_still_valid_json_after_the_chmod(self, tmp_path, monkeypatch):
        monkeypatch.setattr(approvals, "PENDING_DIR", tmp_path)
        item = approvals.email_draft(body="hello", reason="why")
        path = approvals.write_pending(item)
        loaded = json.loads(path.read_text())
        assert loaded["body"] == "hello"
        assert loaded["channel"] == "email"

    def test_no_temp_files_are_left_behind(self, tmp_path, monkeypatch):
        monkeypatch.setattr(approvals, "PENDING_DIR", tmp_path)
        approvals.write_pending(approvals.email_draft(body="b", reason="r"))
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp-")] == []

    def test_no_queue_means_no_write_and_no_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(approvals, "PENDING_DIR", tmp_path / "missing")
        assert approvals.write_pending(approvals.email_draft(body="b", reason="r")) is None
