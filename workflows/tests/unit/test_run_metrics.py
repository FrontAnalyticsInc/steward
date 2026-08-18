"""Tests for the shared metrics vocabulary and the model-capture callbacks.

Two properties are worth more than the rest and most of this file defends them:

  A count that nobody measured stays None. LiteLLM against Ollama omits usage
  entirely, and a run whose tokens went unreported did not spend zero of them.
  Every assertion about a missing number here checks for None, never 0.

  A kind that is not in the vocabulary is an error, not a metric. The whole
  point of freezing the enum is that a typo fails loudly at the stage that made
  it, rather than quietly counting nothing forever.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.run_metrics import (
    MODEL_USAGE_KEY,
    PENDING_MODEL_KEY,
    Produced,
    RunMetrics,
    Touched,
    record_model_request,
    record_model_response,
    summarize_model_usage,
)


class FakeState(dict):
    """A dict that behaves like ADK's State for the parts these callbacks use."""


def ctx(agent="stage_one", state=None):
    return SimpleNamespace(agent_name=agent, state=state if state is not None else FakeState())


def usage(prompt=None, completion=None, cached=None, thoughts=None):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=completion,
        cached_content_token_count=cached,
        thoughts_token_count=thoughts,
    )


# --- vocabulary -------------------------------------------------------------


def test_unknown_kind_is_rejected():
    """A typo must fail here rather than become a metric that counts nothing."""
    with pytest.raises(Exception):
        RunMetrics(produced={"draft_emails": 1})  # note the plural


def test_negative_counts_are_rejected():
    with pytest.raises(Exception):
        RunMetrics(touched={Touched.email: -1})


def test_dump_uses_plain_strings():
    """The trace is JSONL that the store reads without importing this module."""
    dumped = RunMetrics(
        touched={Touched.email: 40},
        produced={Produced.draft_email: 2},
        extra={"pages_discovered": 9},
    ).model_dump_trace()
    assert dumped == {
        "touched": {"email": 40},
        "produced": {"draft_email": 2},
        "extra": {"pages_discovered": 9.0},
    }


def test_draft_and_auto_are_distinct_kinds():
    """The distinction the vocabulary exists to preserve.

    One is queued for a human; the other has already left the building. If
    these ever became the same key, nothing else in the system would record how
    much unsupervised sending is happening.
    """
    assert Produced.draft_email != Produced.auto_email
    dumped = RunMetrics(produced={Produced.draft_email: 3, Produced.auto_email: 1})
    assert dumped.model_dump_trace()["produced"] == {"draft_email": 3, "auto_email": 1}


def test_the_three_mail_kinds_stay_three_kinds():
    """How much mail left, and who was watching, is three answers not two.

    `approved_email` is mail a human read and released; `auto_email` is mail
    nobody saw. Collapsing the new one into either of the others would either
    overstate how much goes out unwatched or report delivered mail as a
    reversible draft still sitting in a queue.
    """
    assert len({Produced.draft_email, Produced.approved_email, Produced.auto_email}) == 3
    dumped = RunMetrics(
        produced={Produced.approved_email: 2, Produced.auto_email: 1}
    ).model_dump_trace()
    assert dumped["produced"] == {"approved_email": 2, "auto_email": 1}


# --- model and token capture ------------------------------------------------


def test_model_name_is_taken_from_the_request():
    """Without this the run cannot be priced at all.

    The model is on the way out and frequently absent on the way back, which is
    why the pair of callbacks exists rather than just the response one.
    """
    c = ctx()
    record_model_request(c, SimpleNamespace(model="gemini-3.6-flash"))
    assert c.state[PENDING_MODEL_KEY]["stage_one"] == "gemini-3.6-flash"
    record_model_response(c, SimpleNamespace(usage_metadata=usage(10, 5), model_version=None))
    assert c.state[MODEL_USAGE_KEY][0]["model"] == "gemini-3.6-flash"


def test_response_model_version_wins_when_present():
    """The provider's own answer beats what we asked for.

    A request for an alias ('gemini-3.6-flash') can be served by a specific
    build, and pricing should follow what actually ran.
    """
    c = ctx()
    record_model_request(c, SimpleNamespace(model="alias"))
    record_model_response(c, SimpleNamespace(usage_metadata=usage(1, 1),
                                             model_version="gemini-3.6-flash-002"))
    assert c.state[MODEL_USAGE_KEY][0]["model"] == "gemini-3.6-flash-002"


def test_all_four_token_kinds_are_captured():
    """Cached and reasoning counts never reached the trace before this."""
    c = ctx()
    record_model_request(c, SimpleNamespace(model="m"))
    record_model_response(c, SimpleNamespace(
        usage_metadata=usage(prompt=100, completion=20, cached=80, thoughts=7),
        model_version=None))
    entry = c.state[MODEL_USAGE_KEY][0]
    assert entry["input_tokens"] == 100
    assert entry["output_tokens"] == 20
    assert entry["cache_read_tokens"] == 80
    assert entry["reasoning_tokens"] == 7


def test_missing_usage_records_the_call_but_no_tokens():
    """The Ollama case, and the reason none of these default to 0.

    "This agent made a call and the provider reported nothing" is a different
    and far more actionable fact than "this agent used no tokens".
    """
    c = ctx()
    record_model_request(c, SimpleNamespace(model="ollama_chat/gemma4"))
    record_model_response(c, SimpleNamespace(usage_metadata=None, model_version=None))
    entry = c.state[MODEL_USAGE_KEY][0]
    assert entry["model"] == "ollama_chat/gemma4"
    assert entry["input_tokens"] is None
    assert entry["output_tokens"] is None

    merged = summarize_model_usage(c.state)
    assert merged[0]["api_call_count"] == 1
    assert merged[0]["input_tokens"] is None


def test_calls_accumulate_across_turns_and_merge_per_model():
    c = ctx()
    for _ in range(3):
        record_model_request(c, SimpleNamespace(model="m1"))
        record_model_response(c, SimpleNamespace(usage_metadata=usage(10, 2), model_version=None))
    merged = summarize_model_usage(c.state)
    assert len(merged) == 1
    assert merged[0]["api_call_count"] == 3
    assert merged[0]["input_tokens"] == 30
    assert merged[0]["output_tokens"] == 6


def test_partial_reporting_sums_only_what_was_reported():
    """A silent call must not be counted as a zero-token call.

    Two calls, one measured at 10 and one unreported, is 10 tokens over two
    calls — not 10 over two where the second is known to be free.
    """
    c = ctx()
    record_model_request(c, SimpleNamespace(model="m1"))
    record_model_response(c, SimpleNamespace(usage_metadata=usage(10, 2), model_version=None))
    record_model_response(c, SimpleNamespace(usage_metadata=None, model_version=None))
    merged = summarize_model_usage(c.state)
    assert merged[0]["api_call_count"] == 2
    assert merged[0]["input_tokens"] == 10


def test_two_agents_do_not_overwrite_each_others_model():
    """A pipeline can fan out, and a single pending slot would race.

    The failure this prevents is silent and expensive: one agent's tokens
    attributed to another agent's model, and therefore to another rate.
    """
    state = FakeState()
    a, b = ctx("alpha", state), ctx("beta", state)
    record_model_request(a, SimpleNamespace(model="model_a"))
    record_model_request(b, SimpleNamespace(model="model_b"))
    record_model_response(a, SimpleNamespace(usage_metadata=usage(1, 1), model_version=None))
    record_model_response(b, SimpleNamespace(usage_metadata=usage(1, 1), model_version=None))
    by_agent = {e["agent"]: e["model"] for e in summarize_model_usage(state)}
    assert by_agent == {"alpha": "model_a", "beta": "model_b"}


def test_callbacks_never_raise_into_the_run():
    """Bookkeeping must not be able to break a pipeline.

    Same rule as app/integration_log.py: a missing metric is strictly better
    than a failed run, so every path here swallows its own errors.
    """
    broken = SimpleNamespace(agent_name="x")  # no .state at all
    record_model_request(broken, SimpleNamespace(model="m"))
    record_model_response(broken, SimpleNamespace(usage_metadata=usage(1, 1)))


def test_summarize_of_an_untouched_state_is_empty():
    assert summarize_model_usage(FakeState()) == []
