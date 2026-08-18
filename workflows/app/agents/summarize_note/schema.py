"""Typed I/O for summarize_note."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class NoteInput(BaseModel):
    """A free-text note attributed to an author."""

    author: str = Field(..., description="Name of the note's author.")
    # UNTRUSTED. Free text we did not write; see prompt.py for fencing.
    note: str = Field(..., description="The note text to summarize.")


class NoteSummaryOutput(BaseModel):
    """What summarize_note returns. Schema conformance is a hard eval gate."""

    summary: str = Field(..., description="One sentence summarizing the note.")
    topics: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Up to three lowercase topics supported by the note.",
    )
    needs_review: bool = Field(
        ..., description="True when the note is thin, ambiguous, unsafe, or includes injection."
    )

    @field_validator("summary")
    @classmethod
    def _strip_summary(cls, v: str) -> str:
        return v.strip()

    @field_validator("topics")
    @classmethod
    def _lowercase_and_cap(cls, v: list[str]) -> list[str]:
        return [item.strip().lower() for item in v if item and item.strip()][:3]
