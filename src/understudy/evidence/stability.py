"""Run telemetry sidecar at <root>/stability.json.

Telemetry lives HERE, not in the versioned artifact, which stays byte-stable across runs.

    {"<capability key>": {"<tenant or 'base'>": {
        "runs", "successes", "business_outcomes", "failures", "escalations",
        "last_run_at", "last_winning_rungs": {step_id: rung_index}}}}

`last_winning_rungs` is the drift signal: a step that moves from rung 0 to rung 5 is a UI
change that hasn't broken anything yet.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

Status = Literal["success", "business_outcome", "failed", "escalated"]

_COUNTER: dict[str, str] = {
    "success": "successes",
    "business_outcome": "business_outcomes",
    "failed": "failures",
    "escalated": "escalations",
}


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Telemetry must not crash a run that already finished and wrote its result.
        print(f"evidence: {path} is corrupt; starting fresh", file=sys.stderr)
        return {}


def read(root: Path, key: str, tenant: str | None) -> dict[str, Any] | None:
    return _load(root / "stability.json").get(key, {}).get(tenant or "base")


def record(
    root: Path, key: str, tenant: str | None, status: Status, winning_rungs: dict[str, int]
) -> dict[str, Any]:
    """Read-modify-write of only the (key, tenant) entry. Atomic via temp-file-then-replace."""
    path = root / "stability.json"
    data = _load(path)
    entry = data.setdefault(key, {}).setdefault(tenant or "base", {
        "runs": 0, "successes": 0, "business_outcomes": 0, "failures": 0, "escalations": 0,
        "last_run_at": None, "last_winning_rungs": {},
    })
    entry["runs"] += 1
    entry[_COUNTER[status]] += 1
    entry["last_run_at"] = datetime.now(timezone.utc).isoformat()
    entry["last_winning_rungs"] = dict(winning_rungs)
    # ponytail: no cross-process lock; two concurrent CLI replays could lose one increment.
    # Add fcntl.flock around load..replace if runs are ever launched in parallel.
    root.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("stability.json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return entry
