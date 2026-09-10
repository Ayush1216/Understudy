"""Actions, steps, and recovery rules."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field

from .checkpoint import Checkpoint
from .common import Risk, StrictModel
from .target import TargetDescriptor


class NavigateAction(StrictModel):
    type: Literal["navigate"]
    url: str  # may contain {{inputs.x}} / {{secrets.X}}


class ClickAction(StrictModel):
    type: Literal["click"]
    target: TargetDescriptor


class TypeAction(StrictModel):
    type: Literal["type"]
    target: TargetDescriptor
    value: str
    clear_first: bool = True


class SelectAction(StrictModel):
    type: Literal["select"]
    target: TargetDescriptor
    value: str


class PressAction(StrictModel):
    type: Literal["press"]
    key: str
    target: TargetDescriptor | None = None


class WaitAction(StrictModel):
    type: Literal["wait"]
    checkpoint: Checkpoint


class AssertAction(StrictModel):
    type: Literal["assert"]
    checkpoint: Checkpoint


class ExtractAction(StrictModel):
    """Read the named outputs NOW, from the current screen. Values accumulate in run state;
    a value that only exists on an intermediate screen can be captured before the page moves."""

    type: Literal["extract"]
    outputs: list[str] = Field(min_length=1)


Action = Annotated[
    Union[
        NavigateAction,
        ClickAction,
        TypeAction,
        SelectAction,
        PressAction,
        WaitAction,
        AssertAction,
        ExtractAction,
    ],
    Field(discriminator="type"),
]


class Retry(StrictModel):
    """Budget for TRANSIENT action failures (a timeout resolving or dispatching). Distinct from a
    recovery rule's `max_attempts`, which is its own dial."""

    max: int = Field(default=0, ge=0)
    backoff_ms: int = Field(default=500, ge=0)


class Step(StrictModel):
    id: str = Field(min_length=1)
    description: str
    action: Action
    # Classified at record time and written here where a reviewer sees it.
    risk: Risk = "safe"
    # Recorded at discovery by probing for a frame navigation after the action. Drives the
    # settle strategy at replay: arm a frame-scoped wait BEFORE dispatch, or don't wait at all.
    # Guessing this is wrong half the time.
    expects_navigation: bool = False
    preconditions: list[Checkpoint] = Field(default_factory=list)
    postcondition: Checkpoint | None = None
    # Budget for resolving the target and dispatching the action. Checkpoint timing is on the
    # checkpoint (`wait_ms`), not here.
    timeout_ms: int = Field(default=15000, ge=0)
    retries: Retry = Field(default_factory=Retry)
    on_failure: Literal["fail", "escalate"] = "fail"


class RecoveryRule(StrictModel):
    """A known obstruction with a declared repair: a maintenance interstitial to dismiss, a
    session timeout to re-authenticate through. Invisible in the result — a recovered run is
    simply a success; only the event log shows `recovery.applied`."""

    id: str = Field(min_length=1)
    description: str
    when: Checkpoint  # evaluated once per tick; wait_ms must be 0
    do: list[Action] = Field(min_length=1)
    max_attempts: int = Field(default=1, ge=1)  # per run; its OWN budget
    then_retry_step: bool = True
