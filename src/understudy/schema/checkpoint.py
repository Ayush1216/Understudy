"""A small predicate language for asserting page state.

Deliberately not Turing-complete, so an artifact stays reviewable by a human and safe to
store. One evaluator serves all six sites: step preconditions, step postcondition, wait/assert
bodies, recovery triggers, outcome detectors, and the success checkpoint.

Timing has exactly one field with exactly one meaning: `wait_ms` on the ROOT checkpoint is how
long the evaluator may poll for it to become true; 0 means evaluate once. Nested checkpoints
inside all/any/not are evaluated once per tick of the root's poll; their `wait_ms` must be 0
(enforced by lint).
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field

from .common import StrictModel
from .target import TargetDescriptor

TextMatch = Literal["contains", "exact", "regex"]


class _CheckpointBase(StrictModel):
    wait_ms: int = Field(default=0, ge=0)
    description: str = ""


class TextPresent(_CheckpointBase):
    kind: Literal["text_present"]
    text: str
    match: TextMatch = "contains"
    # None = any frame. Otherwise the semantic frame path to look in.
    frame_path: list[str] | None = None


class TextAbsent(_CheckpointBase):
    kind: Literal["text_absent"]
    text: str
    match: TextMatch = "contains"
    frame_path: list[str] | None = None


class ControlPresent(_CheckpointBase):
    kind: Literal["control_present"]
    target: TargetDescriptor


class ControlAbsent(_CheckpointBase):
    kind: Literal["control_absent"]
    target: TargetDescriptor


class UrlMatches(_CheckpointBase):
    kind: Literal["url_matches"]
    pattern: str  # regex, searched against the deepest navigated frame's URL


class TitleMatches(_CheckpointBase):
    kind: Literal["title_matches"]
    pattern: str


class HttpStatus(_CheckpointBase):
    """Transport status of the most recent document load. Lets an artifact say "the app
    returned 503" without pattern-matching one vendor's error prose."""

    kind: Literal["http_status"]
    min: int = Field(ge=100, le=599)
    max: int = Field(ge=100, le=599)


class All(_CheckpointBase):
    kind: Literal["all"]
    of: list["Checkpoint"] = Field(min_length=1)


class AnyOf(_CheckpointBase):
    kind: Literal["any"]
    of: list["Checkpoint"] = Field(min_length=1)


class Not(_CheckpointBase):
    kind: Literal["not"]
    of: "Checkpoint"


Checkpoint = Annotated[
    Union[
        TextPresent,
        TextAbsent,
        ControlPresent,
        ControlAbsent,
        UrlMatches,
        TitleMatches,
        HttpStatus,
        All,
        AnyOf,
        Not,
    ],
    Field(discriminator="kind"),
]

All.model_rebuild()
AnyOf.model_rebuild()
Not.model_rebuild()


def iter_checkpoints(cp: Checkpoint):
    """Depth-first walk over a checkpoint tree, root first."""
    yield cp
    if isinstance(cp, (All, AnyOf)):
        for child in cp.of:
            yield from iter_checkpoints(child)
    elif isinstance(cp, Not):
        yield from iter_checkpoints(cp.of)
