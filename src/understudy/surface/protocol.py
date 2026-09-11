"""The seam between "how we perceive and act on a surface" and "the recorded flow".

This module imports no Playwright, on purpose: it is what a desktop implementation would
implement. `observe` maps to the platform accessibility API, `resolve` runs the same descriptor
ladder against UIAutomation/AX, `act` synthesizes input. The artifact, the checkpoints, the
outcomes, the recoveries and the result contract do not change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from understudy.schema import (
    Action,
    Aliases,
    ErrorClass,
    Intent,
    Observation,
    Risk,
    StrictModel,
    SurfaceKind,
    TargetDescriptor,
)

Mode = Literal["discovery", "replay"]


class ActContext(StrictModel):
    risk: Risk = "safe"
    mode: Mode = "replay"
    # Budget for resolving the target and dispatching the action (not for checkpoints).
    timeout_ms: int = 15000
    approval_granted: bool = False
    # Recorded on the step at discovery. When True the surface arms a frame-scoped navigation
    # wait BEFORE dispatching and re-acquires frames by name after.
    expects_navigation: bool = False
    step_id: str | None = None


@dataclass
class Attempt:
    kind: str
    match_count: int
    note: str = ""


@dataclass
class Resolved:
    handle: Any  # surface-specific element handle; never persisted
    won: str  # StrategyKind that resolved
    rung_index: int  # 0 = the preferred strategy held; >0 is the drift signal
    frame_path: list[str]
    attempts: list[Attempt] = field(default_factory=list)


@dataclass
class ActResult:
    resolved: Resolved | None
    navigated: bool
    url_after: str
    document_status: int | None = None


class DocStatus(StrictModel):
    """Transport status of the most recent document load. Lets replay classify any 5xx as a
    surface error generically instead of recognising one vendor's error-page wording."""

    url: str
    status: int


class PolicyDecision(Protocol):
    decision: Literal["allow", "deny", "require_approval"]
    reason: str


class PolicyGate(Protocol):
    """What the surface needs from the policy engine. The gate sits inside `act()`, before
    target resolution and before any I/O — not in a prompt, where a model could argue with it."""

    def check(
        self,
        *,
        action_type: str,
        url: str | None,
        risk: Risk,
        mode: Mode,
        approval_granted: bool = False,
    ) -> PolicyDecision: ...


# ---- errors ---------------------------------------------------------------------------------


class SurfaceError(Exception):
    """Base for errors the replay executor classifies. `error_class` is the extension point:
    any layer can raise a subclass and be classified without the executor importing it."""

    error_class: ErrorClass = "INTERNAL"

    def __init__(self, message: str, *, expected: str = "", observed: str = ""):
        super().__init__(message)
        self.message = message
        self.expected = expected
        self.observed = observed


class TargetNotResolved(SurfaceError):
    error_class = "TARGET_NOT_FOUND"

    def __init__(self, target: TargetDescriptor, attempts: list[Attempt]):
        tried = ", ".join(f"{a.kind}={a.match_count}{(' ' + a.note) if a.note else ''}" for a in attempts)
        super().__init__(
            f"no strategy resolved target {target.label or target.role!r}",
            expected=f"exactly one visible {target.role} matching {target.label or target.strategies[0].kind!r} in frame {'/'.join(target.frame_path) or 'main'}",
            observed=f"tried [{tried}]",
        )
        self.target = target
        self.attempts = attempts


class AmbiguousTarget(SurfaceError):
    error_class = "AMBIGUOUS_TARGET"

    def __init__(self, target: TargetDescriptor, attempts: list[Attempt]):
        super().__init__(
            f"more than one visible match for target {target.label or target.role!r} after scoping",
            expected="exactly one visible match",
            observed="; ".join(f"{a.kind}: {a.match_count} matches" for a in attempts),
        )
        self.target = target
        self.attempts = attempts


class ControlLost(SurfaceError):
    error_class = "CONTROL_LOST"


class PolicyBlocked(SurfaceError):
    error_class = "POLICY_BLOCKED"

    def __init__(self, decision: str, reason: str, *, action_type: str, url: str | None):
        super().__init__(
            f"policy {decision}: {reason}",
            expected=f"policy to allow {action_type} on {url or '<no url>'}",
            observed=f"{decision}: {reason}",
        )
        self.decision = decision
        self.reason = reason


class SurfaceGone(SurfaceError):
    """The browser or page closed underneath the run. Never fed back to a model."""

    error_class = "INTERNAL"


# ---- the seam -------------------------------------------------------------------------------


@runtime_checkable
class Surface(Protocol):
    kind: SurfaceKind

    async def observe(self, *, include_screenshot: bool = False) -> Observation: ...

    async def resolve(self, target: TargetDescriptor) -> Resolved:
        """Walk the descriptor's strategies in order; accept the first that yields exactly one
        visible match (after scoping when >1). Raises TargetNotResolved / AmbiguousTarget.
        Positional strategies are skipped when target.intent == 'read'."""
        ...

    async def act(self, action: Action, ctx: ActContext) -> ActResult:
        """Control lease check, then policy check, then resolve, then dispatch. `wait`, `assert`
        and `extract` actions are not the surface's business and raise ValueError."""
        ...

    async def read_text(self, target: TargetDescriptor) -> str | None: ...

    async def read_attribute(self, target: TargetDescriptor, attr: str) -> str | None: ...

    async def url(self) -> str: ...

    async def title(self) -> str: ...

    async def last_document_status(self) -> DocStatus | None: ...

    async def screenshot(self, *, mask_sensitive: bool = True) -> bytes: ...

    async def dom_snapshot(self) -> str: ...

    async def visible_text(self, frame_path: list[str] | None = None) -> str:
        """Full visible text of one frame (None = every frame, joined). Checkpoints evaluate
        against this, never against the model-facing digest, which is size-capped."""
        ...

    async def capture_descriptor(
        self, ref: str, observation: Observation, *, intent: Intent
    ) -> TargetDescriptor:
        """Turn a one-turn ref into a durable descriptor by re-finding the live element and
        building every candidate strategy from what perception saw — keeping only those that
        actually select that element right now."""
        ...

    def register_sensitive_target(self, target: TargetDescriptor) -> None:
        """Mask this control in screenshots."""
        ...

    def set_tenant_vocabulary(self, aliases: Aliases, frame_map: dict[str, str]) -> None:
        """Tenant deltas consumed inside resolution so a base artifact replays on a reskinned
        tenant without a re-recording."""
        ...
