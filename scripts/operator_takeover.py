"""A scripted operator, for the one evidence run a person has to be present for.

`alpha.share.open` parks on its irreversible Confirm. Instead of approving it, this operator
does what the brief's §3.6 actually asks for: claims the intervention, takes the live session,
performs the Confirm *by hand* through the console's WebSocket, and hands control back with
`resume`. Replay then re-evaluates the postcondition, finds it already satisfied, and advances
without re-running the step — the share is created exactly once.

The operator is scripted; every seam it touches is the real one — the same control lease, the
same HTTP + WebSocket console routes, the same human-action record that a person clicking in a
browser would go through. Coordinates come from the run's own observation because a script has
no eyes; the console page gets them from a mouse event on the live frame.

    python -m understudy app &            # the target application on :4599
    python scripts/operator_takeover.py   # writes one evidence/runs/replay-… directory
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import httpx
import websockets

from understudy.cli import HUMAN_TIMEOUT_S, with_console
from understudy.discover import load_env_file
from understudy.discover.client import ENV_FILE
from understudy.replay import ReplayOptions, ReplayRun
from understudy.schema import Capability, exit_code

CAPABILITY = Path("capabilities/alpha.share.open.v1.0.0.json")
INPUTS = {"member_number": 100987, "deposit": 50}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _park_notice(http: httpx.AsyncClient) -> str:
    """The id of the intervention the run is parked on. Polled, because the console is a
    bystander: it learns the run stopped the same way an operator's browser would."""
    while True:
        try:  # the operator opens the console before the run gets there, and waits
            control = (await http.get("/api/run")).json()["control"]
        except httpx.HTTPError:
            control = {}
        if control.get("holder") == "human" and control.get("intervention_id"):
            return control["intervention_id"]
        await asyncio.sleep(0.2)


async def operate(run: ReplayRun, base: str) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=30) as http:
        iv = await _park_notice(http)
        open_ = next(i for i in (await http.get("/api/run")).json()["interventions"] if i["id"] == iv)
        print(f"  operator: {iv} — {open_['reason']} at {open_['at_step_id']}", file=sys.stderr)

        (await http.post(f"/api/interventions/{iv}/claim")).raise_for_status()

        obs = await run.surface.observe()
        vp = run.session.page.viewport_size
        confirm = next(c for c in obs.all_controls() if c.role == "button" and c.name == "Confirm")
        click = {
            "t": "click",
            "x": (confirm.page_box.x + confirm.page_box.width / 2) / vp["width"],
            "y": (confirm.page_box.y + confirm.page_box.height / 2) / vp["height"],
        }

        async with websockets.connect(base.replace("http", "ws") + "/ws/live", origin=base) as ws:
            await ws.send(json.dumps(click))
            while True:  # JPEG frames of the live page are interleaved on the same socket
                message = await asyncio.wait_for(ws.recv(), 10)
                if isinstance(message, str):
                    print(f"  operator: clicked Confirm by hand -> {message}", file=sys.stderr)
                    break

        await run.session.page.wait_for_selector("text=SUB-ACCOUNT CREATED", timeout=10_000)
        r = await http.post(
            f"/api/interventions/{iv}/resolve",
            json={"decision": "resume", "note": "opened the share by hand on the live session"},
        )
        r.raise_for_status()
        print("  operator: handed control back with resume", file=sys.stderr)


async def main() -> int:
    load_env_file(ENV_FILE)  # APP_OPERATOR / APP_PASSWORD, exactly as the CLI does
    cap = Capability.model_validate_json(CAPABILITY.read_text(encoding="utf-8"))
    run = ReplayRun(cap, ReplayOptions(inputs=INPUTS, escalation_timeout_s=HUMAN_TIMEOUT_S + 30))
    await run.prepare()
    port = _free_port()
    operator = asyncio.create_task(operate(run, f"http://127.0.0.1:{port}"))
    try:
        result = await with_console(run, run.engine, port, run.execute)
    finally:
        operator.cancel()
        await run.close()
    await asyncio.gather(operator, return_exceptions=True)

    print(result.model_dump_json(indent=2))
    print(f"{result.status}: evidence {result.evidence_dir}", file=sys.stderr)
    return exit_code(result)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
