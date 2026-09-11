"""Seams between a running automation (replay or discovery) and the operator console.

A run owns exactly one BrowserSession. When it cannot proceed safely it hands an
InterventionRequest to its Escalator and parks. The escalator — the console — transfers the
session's control lease to the human, exposes the live page, records what the human does,
and returns the request with its resolution filled once control is handed back. A run with no
escalator attached reports `escalated` immediately: it cannot wait for someone who isn't there.
"""

from __future__ import annotations

from typing import Literal, Protocol

from understudy.evidence import EvidenceDir, RunLogger
from understudy.schema import InterventionRequest
from understudy.surface.web import BrowserSession


class Escalator(Protocol):
    async def raise_intervention(self, request: InterventionRequest) -> InterventionRequest:
        """Park until a human resolves the request (or a timeout aborts it). While parked the
        human holds the session's control lease; it is back with automation before this
        returns. The returned request carries `resolution` and every `human_actions` entry."""
        ...


class RunHandle(Protocol):
    """What the console needs from a live run to show it and to take it over."""

    run_id: str
    kind: Literal["discover", "replay"]
    label: str  # capability key, or the discovery goal
    session: BrowserSession
    logger: RunLogger
    evidence: EvidenceDir
    escalator: Escalator | None
