"""Typed inputs and outputs — the capability's calling contract.

`sensitivity` is behaviour, not documentation: it drives redaction, screenshot masking, and
whether a value is returned to the caller at all. `inputs` → `model_json_schema()` is the
agent-facing tool contract, with zero duplication.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import Sensitivity, StrictModel
from .target import TargetDescriptor

NAME_PATTERN = r"^[a-z][a-z0-9_]*$"

ParamType = Literal["string", "number", "boolean", "enum"]
Scalar = str | int | float | bool


class ParamSpec(StrictModel):
    name: str = Field(pattern=NAME_PATTERN)
    type: ParamType
    description: str
    required: bool = True
    default: Scalar | None = None
    enum_values: list[str] | None = None
    pattern: str | None = None  # regex applied to str(value)
    sensitivity: Sensitivity = "public"
    # A value that would work. The recorder parameterizes it out of every recorded literal, and
    # lint refuses a success checkpoint that embeds it ("green once, useless after").
    example: Scalar | None = None

    @model_validator(mode="after")
    def _enum_needs_values(self) -> "ParamSpec":
        if self.type == "enum" and not self.enum_values:
            raise ValueError(f"input {self.name!r}: type=enum requires enum_values")
        return self


OutputType = Literal["string", "number", "boolean", "currency"]
Transform = Literal["none", "trim", "digits_only", "currency_to_number", "upper", "lower"]


class OutputSource(StrictModel):
    kind: Literal["text_of", "attribute_of", "url_capture"]
    target: TargetDescriptor | None = None
    attribute: str | None = None
    # Regex with a named group equal to the output name, run against the current URL.
    url_pattern: str | None = None
    transform: Transform = "trim"


class OutputSpec(StrictModel):
    name: str = Field(pattern=NAME_PATTERN)
    type: OutputType
    description: str
    required: bool = True
    # `secret`: extracted to prove the step worked, then DROPPED from the caller result.
    sensitivity: Sensitivity = "public"
    source: OutputSource

    @model_validator(mode="after")
    def _source_is_complete(self) -> "OutputSpec":
        s = self.source
        if s.kind in ("text_of", "attribute_of"):
            if s.target is None:
                raise ValueError(f"output {self.name!r}: {s.kind} requires source.target")
            if s.target.intent != "read":
                raise ValueError(
                    f"output {self.name!r}: source.target must have intent='read' "
                    "(positional strategies are forbidden when reading business data)"
                )
            if s.kind == "attribute_of" and not s.attribute:
                raise ValueError(f"output {self.name!r}: attribute_of requires source.attribute")
        elif s.kind == "url_capture" and not s.url_pattern:
            raise ValueError(f"output {self.name!r}: url_capture requires source.url_pattern")
        return self
