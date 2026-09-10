"""The capability artifact: a typed, versioned, reviewable description of a UI flow that an AI
agent can invoke by name and that replays with no model in the loop."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .checkpoint import Checkpoint
from .common import ALL_ACTION_TYPES, RISK_RANK, ActionType, Risk, StrictModel, SurfaceKind, Viewport
from .io import OutputSpec, ParamSpec
from .outcome import BusinessOutcome
from .step import RecoveryRule, Step

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class TargetSpec(StrictModel):
    surface: SurfaceKind
    app: str
    app_version: str | None = None
    tenant: str | None = None
    entry_url: str  # may contain {{inputs.x}}
    viewport: Viewport = Field(default_factory=Viewport)


class PolicyDeclaration(StrictModel):
    """A REQUEST, not the grant. The effective policy is the intersection of this and the
    operator-side config: an artifact can narrow what the operator allows, never widen it. An
    artifact that says `require_approval_for: []` still gets the operator's approval gate."""

    origins: list[str] = Field(min_length=1)
    path_patterns: list[str] = Field(default_factory=lambda: ["/**"])
    action_types: list[ActionType] = Field(default_factory=lambda: list(ALL_ACTION_TYPES))
    max_risk: Risk = "safe"
    require_approval_for: list[Risk] = Field(default_factory=lambda: ["irreversible"])


class Provenance(StrictModel):
    recorded_at: datetime
    goal: str
    model: str
    discovery_run_id: str
    surface_signature: dict[str, str] = Field(default_factory=dict)
    llm_step_count: int = 0
    # True when a human edited the recording. The raw recording is kept beside it.
    curated: bool = False


class Aliases(StrictModel):
    """Vocabulary a tenant uses for the same controls. Consumed inside the resolver, so the
    base artifact's strategies match on either tenant without a re-recording."""

    labels: dict[str, list[str]] = Field(default_factory=dict)   # "Member No." -> ["Account Number"]
    texts: dict[str, list[str]] = Field(default_factory=dict)    # "Retrieve"   -> ["Search"]
    columns: dict[str, list[str]] = Field(default_factory=dict)  # "Share ID"   -> ["Account ID"]


class CapabilityOverride(StrictModel):
    """Per-tenant delta over a base artifact. Objects deep-merge; lists replace wholesale, so an
    overridden `strategies` list is an explicit full replacement. The merged capability must
    re-pass the same schema and lint gates the base one did."""

    note: str = ""
    entry_url: str | None = None
    # Semantic frame path remap, keyed by "/".join(path). {"content": ""} = main frame.
    frame_map: dict[str, str] = Field(default_factory=dict)
    aliases: Aliases = Field(default_factory=Aliases)
    # Partial Step by step id, deep-merged onto the base step.
    steps: dict[str, dict[str, Any]] = Field(default_factory=dict)
    outcomes: list[BusinessOutcome] | None = None
    recoveries: list[RecoveryRule] | None = None


class Capability(StrictModel):
    schema_version: Literal["1"] = "1"
    id: str = Field(min_length=1)
    name: str = Field(pattern=r"^[a-z][a-z0-9_.]*$")
    version: str
    title: str
    description: str
    # `draft` is refused by the agent-facing invoke path; a human may replay it explicitly.
    approval: Literal["draft", "approved", "deprecated"] = "draft"
    target: TargetSpec
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    # Environment variable NAMES. Values are read at substitution time and never stored.
    secrets_required: list[str] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    outcomes: list[BusinessOutcome] = Field(default_factory=list)
    recoveries: list[RecoveryRule] = Field(default_factory=list)
    success_checkpoint: Checkpoint
    policy_declaration: PolicyDeclaration
    provenance: Provenance
    tenant_overrides: dict[str, CapabilityOverride] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not SEMVER.match(v):
            raise ValueError(f"version {v!r} is not MAJOR.MINOR.PATCH")
        return v

    @model_validator(mode="after")
    def _unique_names(self) -> "Capability":
        for what, items in (("inputs", self.inputs), ("outputs", self.outputs)):
            names = [i.name for i in items]
            if len(names) != len(set(names)):
                raise ValueError(f"duplicate {what} names: {names}")
        codes = [o.code for o in self.outcomes]
        if len(codes) != len(set(codes)):
            raise ValueError(f"duplicate outcome codes: {codes}")
        return self

    @property
    def key(self) -> str:
        """Catalog identity. Two versions of one capability coexist."""
        return f"{self.name}@{self.version}"

    def input_by_name(self, name: str) -> ParamSpec | None:
        return next((p for p in self.inputs if p.name == name), None)

    def output_by_name(self, name: str) -> OutputSpec | None:
        return next((o for o in self.outputs if o.name == name), None)

    def max_step_risk(self) -> Risk:
        return max((s.risk for s in self.steps), key=lambda r: RISK_RANK[r], default="safe")
