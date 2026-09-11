"""Where an intervention lives while a human handles it, and the escalator a run parks on.

The store is a sink: every mutation is written to the run's evidence directory through the
redacting writer, so `interventions.json` is what the console shows and what the reviewer reads.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any

from understudy.evidence import EvidenceDir, RunLogger
from understudy.runs import RunHandle
from understudy.schema import Decision, HumanAction, InterventionRequest, Resolution


class InterventionStore:
    def __init__(self, evidence: EvidenceDir, logger: RunLogger) -> None:
        self._evidence = evidence
        self._logger = logger
        self._items: dict[str, InterventionRequest] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._file = evidence.run_dir / "interventions.json"

    def create(self, req: InterventionRequest) -> InterventionRequest:
        self._items[req.id] = req
        self._done[req.id] = asyncio.Event()
        self._persist()
        return req

    def get(self, id: str) -> InterventionRequest:
        return self._items[id]  # KeyError: the route maps it to 404

    def snapshot(self) -> list[dict[str, Any]]:
        """Every intervention as written to disk — post-redaction, like RunLogger.tail."""
        return json.loads(self._file.read_text(encoding="utf-8")) if self._file.exists() else []

    def claim(self, id: str) -> InterventionRequest:
        req = self.get(id)
        if req.status == "open":
            req.status = "claimed"
            self._persist()
        return req

    def append_human_action(self, id: str, action: HumanAction) -> None:
        req = self.get(id)
        if req.status != "claimed":
            raise ValueError(f"intervention {id} is {req.status}; take control first")
        req.human_actions.append(action)
        self._persist()
        self._logger.emit("human.action", intervention_id=id, **action.model_dump(mode="json"))

    def resolve(self, id: str, decision: Decision, note: str = "") -> InterventionRequest:
        req = self.get(id)
        if req.resolution is not None:
            raise ValueError(f"intervention {id} is already {req.status}")
        req.resolution = Resolution(at=datetime.now(timezone.utc), decision=decision, note=note)
        req.status = "aborted" if decision == "abort" else "resolved"
        self._persist()
        self._evidence.write_json(
            f"human-actions/{id}.json",
            {"intervention_id": id, "human_actions": req.human_actions, "resolution": req.resolution},
        )
        self._logger.emit("human.resolved", intervention_id=id, decision=decision, note=note,
                          human_actions=len(req.human_actions))
        self._done[id].set()
        return req

    async def wait(self, id: str, timeout_s: float) -> InterventionRequest:
        try:
            await asyncio.wait_for(self._done[id].wait(), timeout_s)
        except TimeoutError:
            # Never left open: an unanswered request is an abort, on the record.
            self.resolve(id, "abort", f"operator did not respond within {timeout_s:g}s")
        return self._items[id]

    def _persist(self) -> None:
        self._evidence.write_json(self._file.name, list(self._items.values()))


class ConsoleEscalator:
    """runs.Escalator backed by the console. Control moves to the human at raise time, so the
    operator page can act the moment it opens; `claim` is the operator acknowledging, and no
    input is accepted before it (an unclaimed intervention is not being handled by anyone)."""

    def __init__(self, run: RunHandle, store: InterventionStore, *, url: str, timeout_s: float) -> None:
        self._run = run
        self._store = store
        self._url = url.rstrip("/")
        self._timeout_s = timeout_s

    async def raise_intervention(self, request: InterventionRequest) -> InterventionRequest:
        req = self._store.create(request)
        control = self._run.session.control
        control.transfer_to("human", req.id)
        print(f"[understudy] intervention {req.id} ({req.reason}): {self._url}/?intervention={req.id}",
              file=sys.stderr)
        try:
            return await self._store.wait(req.id, self._timeout_s)
        finally:
            control.transfer_to("automation")
