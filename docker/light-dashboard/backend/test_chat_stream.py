"""Tests for the chat SSE translation layer.

`_translate_event` is the whole reason the chat panel can narrate a turn: it
turns the gateway's upstream vocabulary into the handful of frames the browser
switches on. Everything worth pinning down here is a case where dropping or
mistranslating one frame is invisible in the aggregate — the reply still
arrives, so nothing looks broken — but leaves the panel telling the user
something false while the turn runs:

* a settled tool that carries no `result` renders as an expandable step with
  nothing inside it, right up until the finished turn's transcript is re-read
* a failed tool translated as `completed` renders as a green check, so a turn
  that is visibly going wrong looks like one that is going fine
* a truncated result presented as the whole result is worse than one that says
  it was cut — the user reads the tail as the tool's actual last word
* a turn that dies with no `error` text attached used to translate to nothing
  at all, so the reply simply stopped mid-sentence with no explanation

The reasoning frames matter for the same reason: `_thinking` is filtered off
the structured tool channel by design, so this event is the only way a trace
reaches the panel before the turn ends.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from . import main as M


def _frames(name: str, payload: dict) -> list[dict]:
    """Translate one upstream event and decode the SSE frames it produced."""
    out = []
    for raw in M._translate_event(name, payload):
        assert raw.startswith("data: ") and raw.endswith("\n\n"), raw
        out.append(json.loads(raw[len("data: "):].strip()))
    return out


class TestToolFrames:
    def test_running_becomes_a_started_frame(self):
        frames = _frames(M.TOOL_PROGRESS_EVENT, {
            "tool": "terminal", "toolCallId": "call_1",
            "status": "running", "label": "ls -la",
        })
        assert frames == [{
            "type": "tool", "phase": "started", "tool_name": "terminal",
            "call_id": "call_1", "args": "ls -la",
        }]

    def test_completed_carries_the_tool_output(self):
        """Without `detail` an opened step shows '(no output recorded)'."""
        frames = _frames(M.TOOL_PROGRESS_EVENT, {
            "tool": "terminal", "toolCallId": "call_1",
            "status": "completed", "label": "all good",
            "result": '{"exit_code": 0, "output": "all good"}',
            "resultTruncated": False,
        })
        assert len(frames) == 1
        assert frames[0]["phase"] == "settled"
        assert frames[0]["status"] == "ok"
        assert frames[0]["call_id"] == "call_1"
        assert frames[0]["summary"] == "all good"
        assert "all good" in frames[0]["detail"]

    def test_failed_settles_as_an_error(self):
        """A failed tool must not settle as a green check."""
        frames = _frames(M.TOOL_PROGRESS_EVENT, {
            "tool": "terminal", "toolCallId": "call_1",
            "status": "failed", "label": "exit 1",
            "result": '{"exit_code": 1, "error": "boom"}',
        })
        assert frames[0]["status"] == "error"
        assert frames[0]["summary"] == "exit 1"
        assert "boom" in frames[0]["detail"]

    def test_truncated_result_says_so(self):
        """The tail of a cut result must not read as the tool's last word."""
        frames = _frames(M.TOOL_PROGRESS_EVENT, {
            "tool": "read_file", "toolCallId": "call_1",
            "status": "completed", "result": "x" * 4000,
            "resultTruncated": True,
        })
        assert "truncated" in frames[0]["detail"]
        assert frames[0]["detail"].startswith("x" * 100)

    def test_untruncated_result_is_left_alone(self):
        frames = _frames(M.TOOL_PROGRESS_EVENT, {
            "tool": "read_file", "toolCallId": "call_1",
            "status": "completed", "result": "short", "resultTruncated": False,
        })
        assert frames[0]["detail"] == "short"

    def test_missing_result_is_tolerated(self):
        """An older gateway sends no `result`; the chip must still settle."""
        frames = _frames(M.TOOL_PROGRESS_EVENT, {
            "tool": "terminal", "toolCallId": "call_1", "status": "completed",
        })
        assert frames[0]["status"] == "ok"
        assert frames[0]["detail"] == ""

    def test_unknown_status_produces_nothing(self):
        assert _frames(M.TOOL_PROGRESS_EVENT, {"status": "queued"}) == []


class TestReasoningFrames:
    def test_reasoning_becomes_a_thinking_frame(self):
        frames = _frames(M.REASONING_EVENT, {"delta": "**Plan**\nfirst step"})
        assert frames == [{"type": "thinking", "text": "**Plan**\nfirst step"}]

    def test_empty_reasoning_is_dropped(self):
        assert _frames(M.REASONING_EVENT, {"delta": ""}) == []
        assert _frames(M.REASONING_EVENT, {}) == []


class TestCompletionChunks:
    def test_text_delta(self):
        frames = _frames("message", {
            "choices": [{"delta": {"content": "hello"}, "finish_reason": None}],
        })
        assert frames == [{"type": "delta", "text": "hello"}]

    def test_done_marker_is_dropped(self):
        assert _frames("message", {"raw": "[DONE]"}) == []

    def test_normal_finish_produces_nothing(self):
        assert _frames("message", {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
        }) == []

    def test_explicit_error_is_reported(self):
        frames = _frames("message", {
            "choices": [{"delta": {}, "finish_reason": "error"}],
            "error": {"message": "provider refused", "type": "agent_error"},
        })
        assert frames == [{"type": "error", "message": "provider refused"}]

    def test_failure_without_error_text_still_reports(self):
        """The mid-stream crash path emits a bare `finish_reason: error`.

        Translated to nothing, the reply just stopped with no explanation.
        """
        frames = _frames("message", {
            "choices": [{"delta": {}, "finish_reason": "error"}],
        })
        assert len(frames) == 1
        assert frames[0]["type"] == "error"
        assert frames[0]["message"]

    def test_truncation_says_it_was_truncated(self):
        frames = _frames("message", {
            "choices": [{"delta": {}, "finish_reason": "length"}],
        })
        assert "cut off" in frames[0]["message"]

    def test_hermes_block_supplies_the_message(self):
        frames = _frames("message", {
            "choices": [{"delta": {}, "finish_reason": "error"}],
            "hermes": {"error": "tool budget exhausted", "failed": True},
        })
        assert frames[0]["message"] == "tool budget exhausted"


class TestApprovalFrames:
    """A blocked tool has to become something the user can actually answer.

    The panel never sees the gateway's session key — it answers with an opaque
    token instead. That key also scopes the session's memory and its standing
    approvals, so handing it to page JavaScript would let anything running in
    the page resolve approvals for turns the user is not looking at.
    """

    def setup_method(self):
        M._APPROVAL_TOKENS.clear()

    def test_approval_becomes_an_answerable_frame(self):
        frames = _frames(M.APPROVAL_EVENT, {
            "sessionKey": "sess_1",
            "command": "rm -rf /tmp/x",
            "description": "terminal command",
            "choices": ["once", "session", "always", "deny"],
            "timeoutSeconds": 60,
        })
        assert len(frames) == 1
        f = frames[0]
        assert f["type"] == "approval"
        assert f["command"] == "rm -rf /tmp/x"
        assert f["description"] == "terminal command"
        assert f["choices"] == ["once", "session", "always", "deny"]
        assert f["timeout_seconds"] == 60
        # The token resolves back to the key, and the key never left the server.
        assert M._approval_session_key(f["token"]) == "sess_1"
        assert "sess_1" not in json.dumps(f)

    def test_each_request_gets_its_own_token(self):
        a = _frames(M.APPROVAL_EVENT, {"sessionKey": "sess_1"})[0]["token"]
        b = _frames(M.APPROVAL_EVENT, {"sessionKey": "sess_1"})[0]["token"]
        assert a != b

    def test_missing_session_key_reports_rather_than_offering_buttons(self):
        """Buttons that cannot resolve anything are worse than saying so."""
        frames = _frames(M.APPROVAL_EVENT, {"command": "rm -rf /"})
        assert frames[0]["type"] == "error"
        assert "approval" in frames[0]["message"].lower()

    def test_choices_fall_back_when_absent(self):
        f = _frames(M.APPROVAL_EVENT, {"sessionKey": "sess_1"})[0]
        assert f["choices"] == ["once", "deny"]

    def test_expired_token_stops_resolving(self):
        token = _frames(M.APPROVAL_EVENT, {"sessionKey": "sess_1"})[0]["token"]
        M._APPROVAL_TOKENS[token]["created"] -= M._APPROVAL_TOKEN_TTL_SECONDS + 1
        assert M._approval_session_key(token) is None

    def test_unknown_token_resolves_to_nothing(self):
        assert M._approval_session_key("not-a-real-token") is None

    def test_token_store_is_bounded(self):
        """A long-running dashboard must not accumulate tokens forever."""
        for _ in range(M._APPROVAL_TOKEN_LIMIT + 25):
            _frames(M.APPROVAL_EVENT, {"sessionKey": "sess_1"})
        assert len(M._APPROVAL_TOKENS) <= M._APPROVAL_TOKEN_LIMIT


class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Stands in for httpx.AsyncClient, recording the one call it receives."""

    def __init__(self, response, calls):
        self._response = response
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self._calls.append({"url": url, **kwargs})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class TestApprovalDecisionEndpoint:
    """What the panel does with the token, and what it must refuse to do.

    Every case here is one where the honest answer differs from the
    convenient one: a lapsed approval has already been refused upstream, so
    reporting success would tell the user their click did something it did
    not.
    """

    def setup_method(self):
        from fastapi.testclient import TestClient

        M._APPROVAL_TOKENS.clear()
        self.client = TestClient(M.app, raise_server_exceptions=False)
        self.calls = []

    def _install(self, response):
        calls = self.calls
        return patch.object(
            M.httpx, "AsyncClient", lambda *a, **k: _FakeClient(response, calls)
        )

    def _token(self, session_key="sess_1"):
        return _frames(M.APPROVAL_EVENT, {"sessionKey": session_key})[0]["token"]

    def test_decision_reaches_the_gateway_with_the_hidden_key(self):
        token = self._token("sess_secret")
        with self._install(_FakeResponse(200)):
            res = self.client.post(f"/api/chat/approvals/{token}", json={"choice": "once"})
        assert res.status_code == 200
        assert res.json()["choice"] == "once"
        assert self.calls[0]["url"].endswith("/v1/approvals/sess_secret/decision")
        assert self.calls[0]["json"] == {"choice": "once"}

    def test_deny_reason_is_forwarded(self):
        token = self._token()
        with self._install(_FakeResponse(200)):
            res = self.client.post(
                f"/api/chat/approvals/{token}",
                json={"choice": "deny", "reason": "wrong path"},
            )
        assert res.status_code == 200
        assert self.calls[0]["json"]["reason"] == "wrong path"

    def test_token_is_single_use(self):
        """A second click must not re-answer a question already answered."""
        token = self._token()
        with self._install(_FakeResponse(200)):
            assert self.client.post(
                f"/api/chat/approvals/{token}", json={"choice": "once"}
            ).status_code == 200
            assert self.client.post(
                f"/api/chat/approvals/{token}", json={"choice": "once"}
            ).status_code == 404

    def test_unknown_token_is_refused(self):
        with self._install(_FakeResponse(200)):
            res = self.client.post("/api/chat/approvals/nope", json={"choice": "once"})
        assert res.status_code == 404
        assert not self.calls, "an unknown token must not reach the gateway"

    def test_lapsed_approval_says_so_and_retires_the_token(self):
        token = self._token()
        with self._install(_FakeResponse(409)):
            res = self.client.post(f"/api/chat/approvals/{token}", json={"choice": "once"})
        assert res.status_code == 409
        assert "refusal" in res.json()["detail"]
        assert M._approval_session_key(token) is None

    def test_invalid_choice_is_rejected_before_the_gateway(self):
        token = self._token()
        with self._install(_FakeResponse(200)):
            res = self.client.post(f"/api/chat/approvals/{token}", json={"choice": "maybe"})
        assert res.status_code == 422
        assert not self.calls
        # The approval is still answerable — a typo must not burn it.
        assert M._approval_session_key(token) == "sess_1"

    def test_unreachable_gateway_is_a_502(self):
        token = self._token()
        with self._install(M.httpx.ConnectError("refused")):
            res = self.client.post(f"/api/chat/approvals/{token}", json={"choice": "once"})
        assert res.status_code == 502
