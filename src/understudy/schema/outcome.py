"""Declared business outcomes: the app worked and said no.

"No such member" is a legitimate answer the caller needs, not a crash. If outcomes live in the
error path, sooner or later somebody catches one as a failure — so they are members of the
artifact, typed, and returned as a first-class result status.
"""

from __future__ import annotations

from pydantic import Field

from .checkpoint import Checkpoint
from .common import StrictModel
from .io import OutputSpec


class BusinessOutcome(StrictModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    description: str
    # Evaluated once per tick of the step's postcondition poll, BEFORE the postcondition.
    detect: Checkpoint
    # e.g. the application's own validation message, extracted for the caller.
    outputs: list[OutputSpec] = Field(default_factory=list)
