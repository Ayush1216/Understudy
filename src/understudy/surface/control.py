"""Who is driving the live session. A two-state lease enforced by one line at the top of
`Surface.act()`: while a human holds it, automation's next action raises ControlLost.

There is exactly one of these per browser session, and the operator console is handed the
run's own instance — no console route ever launches a browser. That one fact is what the
entire live-handoff claim rests on.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Literal

from .protocol import ControlLost

Holder = Literal["automation", "human"]


class SessionControl:
    def __init__(self) -> None:
        self.holder: Holder = "automation"
        self.intervention_id: str | None = None
        self.since: datetime = datetime.now(timezone.utc)
        self._automation_holds = asyncio.Event()
        self._automation_holds.set()

    def assert_held_by(self, who: Holder) -> None:
        if self.holder != who:
            raise ControlLost(
                f"session is held by {self.holder}"
                + (f" (intervention {self.intervention_id})" if self.intervention_id else ""),
                expected=f"{who} to hold the session",
                observed=f"{self.holder} holds it",
            )

    def transfer_to(self, who: Holder, intervention_id: str | None = None) -> None:
        self.holder = who
        self.intervention_id = intervention_id if who == "human" else None
        self.since = datetime.now(timezone.utc)
        if who == "automation":
            self._automation_holds.set()
        else:
            self._automation_holds.clear()

    async def wait_for_automation(self, timeout_s: float | None = None) -> bool:
        """Park until control returns to automation. False on timeout."""
        try:
            await asyncio.wait_for(self._automation_holds.wait(), timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    def snapshot(self) -> dict[str, str | None]:
        return {"holder": self.holder, "intervention_id": self.intervention_id, "since": self.since.isoformat()}
