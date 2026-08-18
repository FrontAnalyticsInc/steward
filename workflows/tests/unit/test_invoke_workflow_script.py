"""Regression tests for the gateway-side ADK invoker script."""

from __future__ import annotations

import http.client
import importlib.util
import urllib.request
from pathlib import Path


# The repo copy, not the deployed one. /opt/data/scripts exists only inside the
# gateway container, so pinning that path made this test pass on one machine and
# fail everywhere else, CI included. hermes/scripts is what gets deployed there,
# so testing it is testing the thing that ships.
#
# parents[3] is the REPOSITORY ROOT — .../hermes/scripts, not
# .../workflows/hermes/scripts. This briefly pointed at parents[2], with a copy
# of the script placed under workflows/ to satisfy it. That makes the test pass
# against a duplicate which nothing deploys and which is free to drift from the
# real one, defeating the sentence above. Run pytest from a full checkout.
INVOKER_PATH = Path(__file__).resolve().parents[3] / "hermes" / "scripts" / "invoke_workflow.py"


def load_invoker():
    spec = importlib.util.spec_from_file_location("invoke_workflow_script", INVOKER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_remote_disconnected_is_retryable_transient(monkeypatch):
    """A reload-disconnect should enter the bounded retry path, not crash raw."""

    invoker = load_invoker()

    def closed_connection(_request, timeout=None):
        raise http.client.RemoteDisconnected(
            "Remote end closed connection without response"
        )

    monkeypatch.setattr(urllib.request, "urlopen", closed_connection)

    try:
        invoker._request("POST", "/run", {"x": 1}, timeout_s=1)
    except invoker.TransientError as exc:
        assert "remote disconnected on POST /run" in str(exc)
    else:  # pragma: no cover - clearer failure than pytest.raises with dynamic type
        raise AssertionError("RemoteDisconnected must be wrapped as TransientError")
