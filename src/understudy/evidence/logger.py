"""The structured run log: one JSON line per event, append-only, post-redaction.

Every event is redacted ONCE, written, and then the same post-redaction dict is handed to every
subscriber — so a live viewer structurally cannot show what the file masked.

Event types are an open string; the executor defines them. The canonical set:

    run.start · step.start · policy.decision · target.resolved · action.performed ·
    checkpoint.evaluated · outcome.matched · recovery.applied · retry · escalation.raised ·
    human.action · human.resolved · drift.proposed · recorder.overruled · step.end · run.end
"""

from __future__ import annotations

import json
import sys
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from pydantic_core import to_jsonable_python


class Redacts(Protocol):
    """What the evidence sinks need from the policy layer. Returns a copy with secret and PII
    values masked. Sinks only ever hand it JSON-native values (see `jsonable`)."""

    def redact(self, obj: Any) -> Any: ...


def jsonable(obj: Any) -> Any:
    """Normalize to JSON-native BEFORE redacting. A nested model, dataclass or datetime that
    the redactor cannot descend into would otherwise be stringified by json.dumps AFTER
    redaction, carrying a secret to disk unmasked."""
    # base64 for bytes: the utf8 default raises on any PNG, and a log call must not end a run.
    return to_jsonable_python(obj, fallback=str, bytes_mode="base64")


Subscriber = Callable[[dict[str, Any]], None]

_HEADER = ("ts", "run_id", "seq", "type")
_ECHOED = {"step.end", "outcome.matched", "recovery.applied", "escalation.raised", "run.end"}


class RunLogger:
    def __init__(self, run_id: str, path: Path, redactor: Redacts, echo: bool = False) -> None:
        self.run_id = run_id
        self.path = path
        self._redactor = redactor
        self._echo = echo
        self._seq = 0
        self._subscribers: list[Subscriber] = []

    def emit(self, type: str, /, **payload: Any) -> dict[str, Any]:
        # A payload `seq=` or `ts=` would silently overwrite the header; `type=` is also the
        # Action discriminator, so `**action.model_dump()` must fail loudly, not by TypeError.
        if reserved := payload.keys() & _HEADER:
            raise ValueError(f"reserved event keys: {sorted(reserved)}")
        self._seq += 1
        event: dict[str, Any] = self._redactor.redact(jsonable(
            {"ts": datetime.now(timezone.utc).isoformat(), "run_id": self.run_id,
             "seq": self._seq, "type": type, **payload}
        ))
        # Open/append/close per event: a crash mid-run must not lose buffered lines. The directory
        # is made here rather than in __init__ so that a run which dies before its first event —
        # a bad --console-port, a Ctrl-C during startup — leaves no empty directory behind.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        if self._echo and type in _ECHOED:
            # stderr: `invoke` returns the JSON result on stdout for an agent to parse.
            print(_summary(event), file=sys.stderr)
        for fn in list(self._subscribers):
            try:
                fn(event)
            except Exception as e:  # noqa: BLE001 — a viewer must not take the run down
                print(f"evidence: subscriber {fn!r} raised {e!r}; dropped", file=sys.stderr)
                self._subscribers.remove(fn)
        return event

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        self._subscribers.append(fn)

        def unsubscribe() -> None:
            # Tolerate a subscriber that was already dropped for throwing.
            if fn in self._subscribers:
                self._subscribers.remove(fn)

        return unsubscribe

    def tail(self, n: int) -> list[dict[str, Any]]:
        """Last n events, re-read from disk (intervention context)."""
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as f:
            for line in deque(f, maxlen=n):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a line torn by a mid-write kill; only ever the last one
        return events


def _summary(event: dict[str, Any]) -> str:
    # Payload keys are executor-defined; show the scalar ones and skip nested structures.
    bits = [f"{k}={v}" for k, v in event.items()
            if k not in _HEADER and isinstance(v, (str, int, float, bool))]
    return f"[{event['seq']:>3}] {event['type']:<18} {' '.join(bits)}".rstrip()
