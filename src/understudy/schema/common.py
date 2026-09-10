"""Shared enums and the strict base model every schema object inherits from."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Every artifact object forbids unknown keys: a typo in a hand-curated artifact must fail
    loudly at load time, never silently no-op at replay time."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# Control roles as perceived on a surface. Deliberately small: these are the roles a teller
# console (web or desktop) actually has, not the full ARIA vocabulary.
Role = Literal[
    "textbox", "button", "link", "checkbox", "radio", "combobox", "listbox", "cell", "heading"
]

# safe < sensitive < irreversible
Risk = Literal["safe", "sensitive", "irreversible"]
RISK_RANK: dict[str, int] = {"safe": 0, "sensitive": 1, "irreversible": 2}

# Drives redaction, screenshot masking, and whether a value is returned to the caller at all.
Sensitivity = Literal["public", "pii", "secret"]

SurfaceKind = Literal["web", "legacy_web", "desktop"]

# What a target descriptor was captured FOR. Positional strategies are legal for `act`
# (clicking the wrong row is visible and recoverable) and forbidden for `read` (returning the
# wrong balance is silent).
Intent = Literal["act", "read"]

ActionType = Literal["navigate", "click", "type", "select", "press", "wait", "assert", "extract"]
ALL_ACTION_TYPES: tuple[str, ...] = (
    "navigate", "click", "type", "select", "press", "wait", "assert", "extract"
)


class Viewport(StrictModel):
    width: int = 1280
    height: int = 800
