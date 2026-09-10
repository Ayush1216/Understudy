"""How a control is identified on a surface.

The model never authors a locator. It picks a control by a `ref` it was shown in an
observation; perception code — which can actually see the element — emits the descriptor.
A descriptor is an ORDERED ladder of redundant strategies, each with a confidence and a
provenance flag. Resolution walks the ladder and accepts the first strategy that yields
exactly one visible match. Which rung won is logged: a step that drops from rung 0 to rung 5
is a UI change that hasn't broken anything yet.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import Field, field_validator, model_validator

from .common import Intent, Role, StrictModel, Viewport

StrategyKind = Literal[
    "role_name",       # ARIA role + accessible name
    "inferred_label",  # role + label text inferred from layout (adjacent cell etc.)
    "placeholder",
    "table_cell",      # row anchor + column header
    "text",            # exact visible text of a link/button
    "attribute",       # name / id / value / href ...
    "nth_of_role",     # positional
    "css",             # positional
    "coordinate",      # positional, viewport-pinned
]

POSITIONAL_KINDS: frozenset[str] = frozenset({"nth_of_role", "css", "coordinate"})


class _StrategyBase(StrictModel):
    confidence: float = Field(ge=0.0, le=1.0)
    # `captured` = read off the live element at discovery time.
    # `derived`  = synthesized from structure (position). A run whose winning rung is
    # derived is a run to re-record.
    origin: Literal["captured", "derived"]
    # How many elements this strategy matched when it was captured. 1 is what we want;
    # >1 means it needed scoping; None means it was hand-authored.
    matches_at_capture: int | None = None


class RoleNameStrategy(_StrategyBase):
    kind: Literal["role_name"]
    role: Role
    name: str
    exact: bool = True


class InferredLabelStrategy(_StrategyBase):
    """The legacy case. `<td class="lbl">Member No.:</td><td><input name="q"></td>` gives the
    input no accessible name; its only human-readable identity is the neighbouring cell."""

    kind: Literal["inferred_label"]
    role: Role
    label: str


class PlaceholderStrategy(_StrategyBase):
    kind: Literal["placeholder"]
    text: str


class TableCellStrategy(_StrategyBase):
    """Locate a cell by (row anchor, column header) instead of by position. `row_anchor` is
    matched against the text of `anchor_column` (default: the first column). `inner_role`
    targets a control INSIDE the cell rather than the cell itself."""

    kind: Literal["table_cell"]
    row_anchor: str
    column: str
    anchor_column: str | None = None
    inner_role: Role | None = None


class TextStrategy(_StrategyBase):
    kind: Literal["text"]
    text: str
    role: Role | None = None
    exact: bool = True


class AttributeStrategy(_StrategyBase):
    kind: Literal["attribute"]
    attr: Literal["name", "id", "value", "href", "type", "title", "alt"]
    value: str
    role: Role | None = None


class NthOfRoleStrategy(_StrategyBase):
    kind: Literal["nth_of_role"]
    role: Role
    index: int = Field(ge=0)


class CssStrategy(_StrategyBase):
    kind: Literal["css"]
    selector: str


class CoordinateStrategy(_StrategyBase):
    """Last resort. Refused at replay if the live viewport differs from the captured one: a
    coordinate that quietly works at the wrong size is worse than a clean failure."""

    kind: Literal["coordinate"]
    x: float
    y: float
    viewport: Viewport


Strategy = Annotated[
    Union[
        RoleNameStrategy,
        InferredLabelStrategy,
        PlaceholderStrategy,
        TableCellStrategy,
        TextStrategy,
        AttributeStrategy,
        NthOfRoleStrategy,
        CssStrategy,
        CoordinateStrategy,
    ],
    Field(discriminator="kind"),
]


class Scope(StrictModel):
    """Applied only when a strategy yields more than one visible match."""

    within_row_matching: str | None = None  # restrict to a <tr> whose text contains this
    nth: int | None = Field(default=None, ge=0)  # then pick index N of what remains


_POSITIONAL_FRAME = re.compile(r"^frame-\d+$")


class TargetDescriptor(StrictModel):
    role: Role
    # Human-readable, for logs and review ONLY. Never consulted by the resolver.
    label: str = ""
    # The brief asks for "how each target element/control is identified (with your reasoning
    # about robustness)". The recorder fills this from what perception actually saw.
    rationale: str = ""
    # Semantic frame names from the top frame down, e.g. ["content"]. [] = main frame.
    frame_path: list[str] = Field(default_factory=list)
    intent: Intent = "act"
    scope: Scope | None = None
    strategies: list[Strategy] = Field(min_length=1)

    @field_validator("frame_path")
    @classmethod
    def _no_positional_frames(cls, v: list[str]) -> list[str]:
        # Frame attach order depends on network timing. A recording that says "frame-1"
        # silently retargets the nav frame on a slow load.
        for seg in v:
            if _POSITIONAL_FRAME.match(seg):
                raise ValueError(
                    f"positional frame path segment {seg!r}: attach order is not identity; "
                    "use the frame's name"
                )
        return v

    @model_validator(mode="after")
    def _reads_need_a_semantic_rung(self) -> "TargetDescriptor":
        if self.intent == "read" and all(s.kind in POSITIONAL_KINDS for s in self.strategies):
            raise ValueError(
                "a read target has only positional strategies; position is a guess when reading "
                "business data — re-record with a semantic anchor (label, table cell, role+name)"
            )
        return self

    def strategy_kinds(self) -> list[str]:
        return [s.kind for s in self.strategies]
