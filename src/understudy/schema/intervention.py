"""Human-in-the-loop: the request that pauses a run, and the record of what the human did."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import Field

from .common import StrictModel
from .result import CapabilityRef
from .step import Action

# Only values something actually produces.
#   replay:    RISKY_ACTION_APPROVAL, UNRECOVERABLE
#   discovery: AGENT_REQUESTED, NO_PROGRESS, MAX_STEPS
InterventionReason = Literal[
    "RISKY_ACTION_APPROVAL", "UNRECOVERABLE", "AGENT_REQUESTED", "NO_PROGRESS", "MAX_STEPS"
]

InterventionStatus = Literal["open", "claimed", "resolved", "aborted"]
Decision = Literal["resume", "approve", "abort"]


class HumanClick(StrictModel):
    kind: Literal["click"]
    at: datetime
    x: float
    y: float


class HumanType(StrictModel):
    kind: Literal["type"]
    at: datetime
    text: str


class HumanKey(StrictModel):
    kind: Literal["key"]
    at: datetime
    key: str


class HumanNavigate(StrictModel):
    kind: Literal["navigate"]
    at: datetime
    url: str


HumanAction = Annotated[
    Union[HumanClick, HumanType, HumanKey, HumanNavigate], Field(discriminator="kind")
]


class Resolution(StrictModel):
    at: datetime
    decision: Decision
    note: str = ""


class InterventionContext(StrictModel):
    url: str
    title: str
    screenshot_path: str | None = None
    dom_path: str | None = None
    recent_events: list[dict[str, Any]] = Field(default_factory=list)


class InterventionRequest(StrictModel):
    id: str
    created_at: datetime
    status: InterventionStatus = "open"
    origin: Literal["discovery", "replay"]
    run_id: str
    capability: CapabilityRef | None = None
    goal: str | None = None
    reason: InterventionReason
    detail: str
    at_step_id: str | None = None
    step_description: str | None = None
    expected: str | None = None
    observed: str | None = None
    # What the automation wanted to do next (for an approval, the risky action itself).
    proposed_action: Action | None = None
    context: InterventionContext
    human_actions: list[HumanAction] = Field(default_factory=list)
    resolution: Resolution | None = None
