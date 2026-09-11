"""The console against a real run: a Chromium session signed on to the target app, the console
served on the test's own loop, driven over HTTP (httpx) and WebSocket (the `websockets` package,
which ships with uvicorn[standard]). The escalator parks a fake run; a human takes over the live
page through the console; the run wakes with the record of what happened."""

from __future__ import annotations

import asyncio
import json
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import websockets

from understudy.console import ConsoleEscalator, InterventionStore, build_app, handle_input, serve
from understudy.evidence import EvidenceDir, RunLogger, new_intervention_id, new_run_id
from understudy.policy import SECRET_MARK, PolicyEngine, Redactor, effective_policy, load_operator_policy
from understudy.runs import Escalator
from understudy.schema import Capability, InterventionContext, InterventionRequest, NavigateAction
from understudy.surface import ActContext, ControlLost
from understudy.surface.web import BrowserSession, WebSurface

from .conftest import alpha_capability
from .test_replay import Stub, _open_share_capability, _opts, _run, _share_count


@dataclass
class FakeRun:
    run_id: str
    session: BrowserSession
    logger: RunLogger
    evidence: EvidenceDir
    kind: str = "replay"
    label: str = "alpha.member.balance@1.0.0"
    escalator: Escalator | None = None


@dataclass
class Rig:
    run: FakeRun
    store: InterventionStore
    surface: WebSurface
    cap: Capability
    base: str  # console URL
    engine: PolicyEngine

    @property
    def content(self):
        return self.run.session.page.frame(name="content")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _request(run: FakeRun, reason="UNRECOVERABLE", **kw) -> InterventionRequest:
    return InterventionRequest(
        id=new_intervention_id(), created_at=datetime.now(timezone.utc), origin="replay", run_id=run.run_id,
        reason=reason, detail="postcondition never held", at_step_id="s4", step_description="Open member search",
        expected="MEMBER INQUIRY", observed="MAIN MENU",
        context=InterventionContext(url="x", title="y"), **kw,
    )


@pytest.fixture
async def rig(target_server, reset_target, operator_env, policy_path, tmp_path: Path):
    cap = alpha_capability(target_server)
    engine = PolicyEngine(effective_policy(load_operator_policy(policy_path), cap.policy_declaration))
    redactor = Redactor()
    redactor.register_secret(operator_env["APP_PASSWORD"])
    session = await BrowserSession.launch(policy_gate=engine, viewport=cap.target.viewport)
    surface = WebSurface(session, policy_gate=engine)
    run_id = new_run_id("replay")
    evidence = EvidenceDir(tmp_path / "evidence", run_id, redactor)
    run = FakeRun(run_id, session, RunLogger(run_id, evidence.log_path, redactor), evidence)
    store = InterventionStore(evidence, run.logger)
    # Sign on the way replay would: the fixture descriptors, secrets substituted from the env.
    steps = {s.id: s for s in cap.steps}
    await surface.act(NavigateAction(type="navigate", url=cap.target.entry_url), ActContext())
    for sid, secret in (("s1", "APP_OPERATOR"), ("s2", "APP_PASSWORD")):
        await surface.act(steps[sid].action.model_copy(update={"value": operator_env[secret]}), ActContext())
    await surface.act(steps["s3"].action, ActContext(expects_navigation=True))
    assert "MAIN MENU" in await surface.visible_text(["content"])

    port = _free_port()
    server, task = await serve(build_app(run, store, engine), port=port)
    yield Rig(run, store, surface, cap, f"http://127.0.0.1:{port}", engine)
    server.should_exit = server.force_exit = True
    await task
    await session.close()


async def _reply(ws) -> dict:
    """The next JSON reply; binary frames (the live picture) are interleaved on the same socket."""
    while True:
        m = await asyncio.wait_for(ws.recv(), 10)
        if isinstance(m, str):
            return json.loads(m)


async def _until(pred, timeout=5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never held")


def _at(obs, vp, pred) -> dict[str, float]:
    """Centre of the first control satisfying `pred`, as fractions of the viewport (the wire format)."""
    c = next(c for c in obs.all_controls() if pred(c))
    return {"x": (c.page_box.x + c.page_box.width / 2) / vp.width, "y": (c.page_box.y + c.page_box.height / 2) / vp.height}


async def _claimed(rig: Rig) -> tuple[asyncio.Task, str]:
    """Park a request through the console escalator and claim it: the state in which input is accepted."""
    escalator = ConsoleEscalator(rig.run, rig.store, url=rig.base, timeout_s=60)
    parked = asyncio.create_task(escalator.raise_intervention(_request(rig.run)))
    await _until(lambda: rig.run.session.control.holder == "human")
    iv = rig.run.session.control.intervention_id
    rig.store.claim(iv)
    return parked, iv


def _events(rig: Rig) -> list[dict]:
    return [json.loads(line) for line in rig.run.evidence.log_path.read_text().splitlines()]


async def test_human_takes_over_the_live_page_and_the_run_wakes_with_the_record(rig: Rig):
    run, store, surface = rig.run, rig.store, rig.surface
    escalator = ConsoleEscalator(run, store, url=rig.base, timeout_s=60)
    parked = asyncio.create_task(escalator.raise_intervention(_request(run)))
    await _until(lambda: run.session.control.holder == "human")
    iv = run.session.control.intervention_id
    steps = {s.id: s for s in rig.cap.steps}

    # Raising transfers control: automation's next action raises ControlLost.
    with pytest.raises(ControlLost, match=iv):
        await surface.act(steps["s4"].action, ActContext(expects_navigation=True))

    vp = rig.cap.target.viewport
    obs = await surface.observe()
    link = _at(obs, vp, lambda c: c.role == "link" and c.name == "Member Search")
    async with httpx.AsyncClient(base_url=rig.base) as http, websockets.connect(rig.base.replace("http", "ws") + "/ws/live") as ws:
        first = await asyncio.wait_for(ws.recv(), 10)
        assert isinstance(first, bytes) and first.startswith(b"\xff\xd8")  # a JPEG frame is pushed unprompted

        # Before the claim nobody is handling it: input is refused and the page did not move.
        await ws.send(json.dumps({"t": "click", **link}))
        assert "take control" in (await _reply(ws))["error"]
        assert rig.content.url.endswith("/menu")

        r = await http.post(f"/api/interventions/{iv}/claim")
        assert r.json() == {"id": iv, "status": "claimed"}
        assert (await http.get("/api/run")).json()["control"]["holder"] == "human"

        await ws.send(json.dumps({"t": "click", **link}))
        assert (await _reply(ws)) == {"ok": True, "kind": "click"}
        await rig.content.wait_for_url(re.compile(r"/search$"), timeout=5000)
        assert "MEMBER INQUIRY" in await surface.visible_text(["content"])

        # Typed member number + Retrieve through the same path lands on the record.
        obs = await surface.observe()
        await ws.send(json.dumps({"t": "click", **_at(obs, vp, lambda c: c.attrs.get("name") == "q")}))
        assert (await _reply(ws))["ok"]
        await ws.send(json.dumps({"t": "type", "text": "100234"}))
        assert (await _reply(ws))["ok"]
        await ws.send(json.dumps({"t": "click", **_at(obs, vp, lambda c: c.role == "button" and c.name == "Retrieve")}))
        assert (await _reply(ws))["ok"]
        await rig.content.wait_for_url(re.compile(r"/members/100234$"), timeout=5000)
        assert "MEMBER RECORD" in await surface.visible_text(["content"])

        # A human navigate is policy-checked: off the allowlist -> refused, page did not move.
        await ws.send(json.dumps({"t": "navigate", "url": rig.cap.target.entry_url.replace("/alpha/", "/beta/")}))
        assert "refused" in (await _reply(ws))["error"]
        assert rig.content.url.endswith("/members/100234")

        r = await http.post(f"/api/interventions/{iv}/resolve", json={"decision": "resume", "note": "did s4-s6 by hand"})
        assert r.status_code == 200

    req = await asyncio.wait_for(parked, 10)
    assert req.resolution.decision == "resume" and req.status == "resolved"
    assert run.session.control.holder == "automation" and run.session.control.intervention_id is None
    assert [a.kind for a in req.human_actions] == ["click", "click", "type", "click"]
    assert req.human_actions[0].x == pytest.approx(link["x"] * vp.width, abs=0.01)

    # Every human action is on disk (redacted sink) and in the run log; the refused navigate too.
    on_disk = json.loads((run.evidence.run_dir / "interventions.json").read_text())
    assert [a["kind"] for a in on_disk[0]["human_actions"]] == ["click", "click", "type", "click"]
    assert (run.evidence.run_dir / "human-actions" / f"{iv}.json").exists()
    events = [json.loads(l) for l in run.evidence.log_path.read_text().splitlines()]
    human = [e for e in events if e["type"] == "human.action"]
    assert [e["kind"] for e in human] == ["click", "click", "type", "click", "navigate"]
    assert human[-1]["url"].endswith("/t/beta/") and human[-1]["denied"]
    assert events[-1]["type"] == "human.resolved" and events[-1]["decision"] == "resume"


async def test_a_password_typed_per_keystroke_is_masked_at_the_record(rig: Rig, operator_env, target_server):
    secret = operator_env["APP_PASSWORD"]
    parked, iv = await _claimed(rig)

    async def go(msg: dict) -> dict:
        return await handle_input(rig.run, rig.store, rig.engine, msg)

    # Signing off lands on the top-level sign-on page, which has the password field.
    assert (await go({"t": "navigate", "url": f"{target_server}/t/alpha/signoff"}))["ok"]
    obs = await rig.surface.observe()
    assert (await go({"t": "click", **_at(obs, rig.cap.target.viewport, lambda c: c.attrs.get("name") == "p")}))["ok"]
    for ch in secret:  # exactly what console.html sends: one message per keydown
        assert (await go({"t": "type", "text": ch}))["ok"]
    # The real text reached the page; the record holds the mark, one per keystroke.
    assert await rig.run.session.page.evaluate("() => document.querySelector('input[name=p]').value") == secret
    rig.store.resolve(iv, "resume")
    await parked

    typed = [e["text"] for e in _events(rig) if e["type"] == "human.action" and e["kind"] == "type"]
    assert typed == [SECRET_MARK] * len(secret)
    for name in ("run.jsonl", "interventions.json", f"human-actions/{iv}.json"):
        text = (rig.run.evidence.run_dir / name).read_text()
        assert secret not in text and text.count(SECRET_MARK) >= len(secret), name


async def test_a_cross_origin_websocket_is_refused(rig: Rig):
    url = rig.base.replace("http", "ws") + "/ws/live"
    with pytest.raises(websockets.exceptions.InvalidHandshake):
        await websockets.connect(url, origin="http://evil.example")
    async with websockets.connect(url, origin=rig.base) as ws:  # the console page's own origin
        assert isinstance(await asyncio.wait_for(ws.recv(), 10), bytes)


async def test_a_child_frame_target_resolves_after_a_human_navigate(rig: Rig):
    parked, iv = await _claimed(rig)
    assert (await handle_input(rig.run, rig.store, rig.engine, {"t": "navigate", "url": rig.cap.target.entry_url}))["ok"]
    rig.store.resolve(iv, "resume")
    await parked
    steps = {s.id: s for s in rig.cap.steps}
    assert (await rig.surface.resolve(steps["s4"].action.target)).won == "role_name"  # "Member Search" in /nav


async def _confirms_by_hand(run, req) -> str:
    """The operator performs the irreversible action on the live page and hands back with resume."""
    page = run.session.page
    await page.click("input[value=Confirm]")
    await page.wait_for_selector("text=SUB-ACCOUNT CREATED")
    return "resume"


async def test_resume_after_the_human_confirmed_by_hand_advances_without_rerun(reset_target, operator_env, policy_path, tmp_path):
    before = _share_count(reset_target)
    run, result = await _run(_open_share_capability(reset_target), _opts(tmp_path, policy_path), Stub(_confirms_by_hand))
    assert result.status == "success", result
    assert _share_count(reset_target) == before + 1  # performed exactly once
    events = [json.loads(line) for line in run.evidence.log_path.read_text().splitlines()]
    assert [e["resume_branch"] for e in events if e["type"] == "human.resolved"] == ["postcondition_satisfied"]
    assert not [e for e in events if e["type"] == "target.resolved" and e["step_id"] == "s8"]  # not re-run


async def test_unanswered_intervention_aborts_and_hands_control_back(rig: Rig):
    escalator = ConsoleEscalator(rig.run, rig.store, url=rig.base, timeout_s=0.2)
    req = await escalator.raise_intervention(_request(rig.run))
    assert req.status == "aborted" and req.resolution.decision == "abort"
    assert "did not respond within 0.2s" in req.resolution.note
    assert rig.run.session.control.holder == "automation"


async def test_approve_answers_only_a_risky_action_approval(rig: Rig):
    stuck = rig.store.create(_request(rig.run, "UNRECOVERABLE"))
    risky = rig.store.create(_request(rig.run, "RISKY_ACTION_APPROVAL"))
    async with httpx.AsyncClient(base_url=rig.base) as http:
        r = await http.post(f"/api/interventions/{stuck.id}/resolve", json={"decision": "approve"})
        assert r.status_code == 400 and "approve" in r.json()["detail"]
        assert stuck.resolution is None
        r = await http.post(f"/api/interventions/{risky.id}/resolve", json={"decision": "approve", "note": "ok"})
        assert r.status_code == 200 and risky.resolution.decision == "approve"
        r = await http.post(f"/api/interventions/{risky.id}/resolve", json={"decision": "abort"})
        assert r.status_code == 409  # a decision is made once
        assert (await http.post("/api/interventions/iv_nope/claim")).status_code == 404


async def test_events_replay_history_then_stream_live_and_never_carry_a_secret(rig: Rig, operator_env):
    secret = operator_env["APP_PASSWORD"]
    rig.run.logger.emit("run.start", label="x")
    rig.run.logger.emit("action.performed", value=secret)
    async with httpx.AsyncClient(base_url=rig.base) as http:
        async with http.stream("GET", "/api/events") as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            lines = r.aiter_lines()

            async def data():
                while True:
                    line = await asyncio.wait_for(anext(lines), 10)
                    if line.startswith("data: "):
                        return json.loads(line[6:])

            history = [await data(), await data()]
            assert [e["type"] for e in history] == ["run.start", "action.performed"]
            assert history[1]["value"] == SECRET_MARK
            rig.run.logger.emit("step.start", step_id="s4", note=secret)
            live = await data()
            assert live["type"] == "step.start" and live["seq"] == 3 and secret not in json.dumps(live)


async def test_frame_is_a_jpeg_of_the_live_page(rig: Rig):
    async with httpx.AsyncClient(base_url=rig.base) as http:
        r = await http.get("/api/frame.jpg")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        assert r.headers["cache-control"] == "no-store" and r.content.startswith(b"\xff\xd8")
        info = (await http.get("/api/run")).json()
        assert info["alive"] and info["kind"] == "replay" and info["interventions"] == []
        assert "Understudy console" in (await http.get("/")).text
