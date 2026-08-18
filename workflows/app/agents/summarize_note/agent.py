"""summarize_note — structured one-sentence note summarization."""

from __future__ import annotations

from google.adk.agents import LlmAgent

from app.config import build_model
from app.run_metrics import MODEL_CAPTURE_AFTER, MODEL_CAPTURE_BEFORE

from .prompt import INSTRUCTION
from .schema import NoteSummaryOutput

AGENT_NAME = "summarize_note"

summarize_note_agent = LlmAgent(
    name=AGENT_NAME,
    model=build_model(),
    description=(
        "Summarizes a free-text note into one sentence, up to three topics, "
        "and a needs_review flag."
    ),
    instruction=INSTRUCTION,
    # Records which model this stage called and what the provider reported
    # spending, including the cached and reasoning counts the event stream drops.
    # Without the model name a run cannot be priced at all, which is how
    # estimated_cost_usd came to be a hardcoded zero. See app/run_metrics.py.
    before_model_callback=MODEL_CAPTURE_BEFORE,
    after_model_callback=MODEL_CAPTURE_AFTER,
    # Structured output: ADK returns a dict conforming to this model, so
    # nothing downstream parses prose. Note this also disables tool calling,
    # which is intended here.
    output_schema=NoteSummaryOutput,
    output_key="note_summary",
)

# ADK's nested agent loader resolves an app to `<module>.agent.root_agent`. Without
# this alias the agent appears in /list-apps but every /run against it 404s with
# "No root_agent found".
root_agent = summarize_note_agent
