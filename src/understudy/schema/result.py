"""What a caller gets back. Four arms, discriminated on `status`.

A declared business outcome is a legitimate answer (exit 0). A failure carries `expected` and
`observed` as prose produced by the same evaluator that made the decision, so a human triaging
a broken automation does not have to open a screenshot to know what went wrong.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import Field

from .common import StrictModel

# Only values something actually produces. No dead enum members.
ErrorClass = Literal[
    "INPUT_INVALID",        # the artifact, the inputs, or the secrets are wrong; no browser opened
    "POLICY_BLOCKED",       # refused by policy, or by a human
    "TARGET_NOT_FOUND",     # no strategy resolved; a drift candidate
    "AMBIGUOUS_TARGET",     # more than one visible match after scoping
    "PRECONDITION_FAILED",  # wrong page state going into a step
    "CHECKPOINT_FAILED",    # asserted state never materialized (postcondition, assert, success,
                            # or a required output was empty)
    "TIMEOUT",              # run wall clock exceeded (human wait excluded)
    "SURFACE_ERROR",        # the application broke: 5xx, or its own error page
    "CONTROL_LOST",         # a human holds the session
    "INTERNAL",
]


class RunError(StrictModel):
    kind: ErrorClass
    step_id: str | None = None
    step_description: str | None = None
    expected: str
    observed: str
    message: str
    recovery_attempts: list[str] = Field(default_factory=list)


class CapabilityRef(StrictModel):
    id: str
    name: str
    version: str


class _ResultBase(StrictModel):
    run_id: str
    capability: CapabilityRef
    tenant: str | None = None
    duration_ms: int = 0
    evidence_dir: str


class SuccessResult(_ResultBase):
    status: Literal["success"]
    outputs: dict[str, Any]
    steps_executed: int


class BusinessOutcomeResult(_ResultBase):
    status: Literal["business_outcome"]
    code: str
    message: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    at_step_id: str | None = None


class EscalatedResult(_ResultBase):
    status: Literal["escalated"]
    intervention_id: str
    reason: str
    at_step_id: str | None = None


class FailedResult(_ResultBase):
    status: Literal["failed"]
    error: RunError


RunResult = Annotated[
    Union[SuccessResult, BusinessOutcomeResult, EscalatedResult, FailedResult],
    Field(discriminator="status"),
]


def exit_code(result: SuccessResult | BusinessOutcomeResult | EscalatedResult | FailedResult) -> int:
    """0 = success or a legitimate business outcome; 1 = failure; 2 = a human was needed."""
    return {"success": 0, "business_outcome": 0, "failed": 1, "escalated": 2}[result.status]
