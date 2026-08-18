"""Typed result contract for intentional_failure_demo.

This workflow exists only as demo/test infrastructure for the ADK health path.
It deterministically emits a failed result so the workflow monitoring and kanban
repair loop can be screenshotted without depending on a real outage.
"""

from __future__ import annotations

from typing import Any
from typing import Literal

from pydantic import BaseModel, Field


class IntentionalFailureItem(BaseModel):
    """A machine-readable marker for the intentional failure."""

    kind: Literal["intentional_failure_demo"] = "intentional_failure_demo"
    message: str = "Intentional workflow failure for kanban screenshot/testing."


class IntentionalFailureResult(BaseModel):
    """The standard invoke_workflow emit_result payload shape."""

    status: Literal["failed"] = "failed"
    items: list[IntentionalFailureItem] = Field(default_factory=list)
    needs_review: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
    model_usage: list[dict[str, Any]] = Field(default_factory=list)
    self_assessment: dict = Field(default_factory=dict)
