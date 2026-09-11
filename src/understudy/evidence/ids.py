"""Identifiers that become directory names and file keys."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Literal

RunKind = Literal["discover", "replay"]


def new_run_id(kind: RunKind) -> str:
    """`replay-20260910-161000-a1b2c3`: sorts by time, prefixed by what produced it."""
    if kind not in ("discover", "replay"):
        raise ValueError(f"unknown run kind {kind!r}")
    return f"{kind}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def new_intervention_id() -> str:
    return "iv_" + secrets.token_hex(5)
