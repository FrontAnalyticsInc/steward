"""How a pipeline reports on its own health, for every pipeline in this project.

Two different claims live here, and the whole point of the module is that they
never merge into one number.

`score` is **measured**. It is the fraction of the run's declared checkpoints
that actually passed, computed from what each stage recorded as it ran. No model
is consulted, the same run always scores the same, and a pipeline cannot flatter
itself.

`self_reports` are **claimed**. A stage that already makes an LLM call may, at no
extra cost, return "how well did I do" alongside its real output — a score and a
sentence or two on what went well and what would have helped (a missing tool, an
ambiguous instruction). This is the only way to surface a problem no rule was
written to detect, and it is exactly as trustworthy as a model grading its own
homework: useful as a lead, never as evidence.

So they are separate fields, and averaging them would destroy the only property
that makes the measured one worth having.

The distinction this feeds is operational: the review queue is about the *output*
of a run (is this draft good enough to send), while a low score here is about the
*pipeline* (it is not doing its job at all) and belongs on the Kanban board as
development work.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# State keys. Deliberately verbose — they share a namespace with pipeline data.
CHECKPOINTS_KEY = "assessment_checkpoints"
SELF_REPORTS_KEY = "assessment_self_reports"


class SelfReport(BaseModel):
    """A model stage's optional opinion of its own turn.

    Every field is optional. A model that cannot judge itself should say nothing
    rather than emit a confident default — an absent score is a fact, and 0.0
    would be a claim.
    """

    score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "How well you did this turn, 0-1. Omit if you cannot judge it. "
            "Do not report high confidence merely because you produced output."
        ),
    )
    went_well: str | None = Field(
        default=None, description="One sentence on what worked. Omit if nothing stands out."
    )
    could_improve: str | None = Field(
        default=None,
        description=(
            "One sentence on what would have made this turn better — a tool you "
            "lacked, an instruction that was ambiguous, input that was missing or "
            "malformed. This is read by a human fixing the pipeline, so name the "
            "concrete obstacle rather than apologising. Omit if nothing hindered you."
        ),
    )


def checkpoint(
    ctx: Any, name: str, ok: bool, detail: str | None = None, state: dict | None = None
) -> dict[str, Any]:
    """Record one stage's outcome, returning a state_delta to yield.

    A state_delta replaces a key rather than merging into it, so the accumulated
    list has to be read back and rewritten whole — the same reason the error list
    is handled this way. Without it only the last stage would be represented and
    the score would be computed over a sample of one.

    `ok` is the stage's own answer to "did I do the job I exist to do", which is
    not the same as "did I raise". A fetch that returns zero messages because the
    inbox is empty is ok; one that returns zero because there is no credential is
    not, and only the stage knows which happened.
    """
    prior = list((ctx.session.state.get(CHECKPOINTS_KEY) or []))
    prior.append({"stage": name, "ok": bool(ok), "detail": detail})
    return {**(state or {}), CHECKPOINTS_KEY: prior}


def record_self_report(ctx: Any, agent: str, report: Any) -> dict[str, Any]:
    """Attach a model stage's self-report, returning a state_delta.

    Accepts the parsed sub-object off a stage's output. Anything unusable is
    dropped rather than stored as empty: a self-report that says nothing is
    indistinguishable from a stage that filed none, and pretending otherwise
    would inflate the count of stages that reflected on themselves.
    """
    if report is None:
        return {}
    data = report if isinstance(report, dict) else getattr(report, "model_dump", lambda: None)()
    if not isinstance(data, dict):
        return {}
    kept = {k: data.get(k) for k in ("score", "went_well", "could_improve") if data.get(k) is not None}
    if not kept:
        return {}
    prior = list((ctx.session.state.get(SELF_REPORTS_KEY) or []))
    prior.append({"agent": agent, **kept})
    return {SELF_REPORTS_KEY: prior}


def build(
    ctx: Any,
    errors: list[str] | None = None,
    extra_checkpoints: list[dict] | None = None,
    extra_self_reports: list[dict] | None = None,
) -> dict[str, Any]:
    """The aggregated self-assessment for the run.

    `score` is None, not 0.0, when no stage declared a checkpoint. A pipeline
    that recorded nothing has not scored badly — it has not been measured, and
    the two must not render the same on a scorecard.

    The `extra_*` arguments carry checkpoints for stages that could not record
    their own — a plain LlmAgent runs no code of ours, so whoever can see its
    output has to speak for it. They are passed in rather than written to state
    because a state_delta belongs to a yielded event, and this is called while
    building one.
    """
    checkpoints = list(ctx.session.state.get(CHECKPOINTS_KEY) or []) + list(extra_checkpoints or [])
    self_reports = list(ctx.session.state.get(SELF_REPORTS_KEY) or []) + list(
        extra_self_reports or []
    )
    errors = list(errors or [])

    passed = [c for c in checkpoints if c.get("ok")]
    failed = [c for c in checkpoints if not c.get("ok")]
    score = round(len(passed) / len(checkpoints), 3) if checkpoints else None

    return {
        # --- measured ---
        "score": score,
        "checkpoints_total": len(checkpoints),
        "checkpoints_passed": len(passed),
        "checkpoints": checkpoints,
        # The short version of what to fix, for a task title.
        "failed_stages": [c["stage"] for c in failed],
        "errors": errors,
        # --- claimed, by the model stages that chose to answer ---
        "self_reports": self_reports,
    }
