from __future__ import annotations

from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.genai import types

from app import self_assessment
from app.run_metrics import Produced, RunMetrics, summarize_model_usage

from .schema import IntentionalFailureItem, IntentionalFailureResult

FAILURE_MESSAGE = (
    "INTENTIONAL_FAILURE_DEMO: deterministic failed status for ADK workflow "
    "health/kanban screenshot testing. This is not a production outage."
)


class IntentionalFailureDemoAgent(BaseAgent):
    """A controlled failure used to exercise workflow health filing."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        checkpoint = {
            "stage": "intentional_failure_demo",
            "ok": False,
            "detail": FAILURE_MESSAGE,
        }
        assessment = self_assessment.build(
            ctx,
            errors=[FAILURE_MESSAGE],
            extra_checkpoints=[checkpoint],
        )
        result = IntentionalFailureResult(
            items=[IntentionalFailureItem(message=FAILURE_MESSAGE)],
            errors=[FAILURE_MESSAGE],
            metrics=RunMetrics(
                produced={Produced.document: 0},
                extra={"intentional_failure_demo": 1},
            ).model_dump_trace(),
            model_usage=summarize_model_usage(ctx.session.state),
            self_assessment=assessment,
        ).model_dump()
        yield Event(
            author=self.name,
            content=types.Content(
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="emit_result", response=result
                        )
                    )
                ]
            ),
        )
