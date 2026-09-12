"""The operator console: one FastAPI app mounted on the run's own event loop.

It never launches a browser. It is handed the run's BrowserSession and drives the same Page the
automation is parked on — the page that has the problem, never a fresh one.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from playwright.async_api import Error, Page
from pydantic import BaseModel

from understudy.policy import SECRET_MARK
from understudy.runs import RunHandle
from understudy.schema import (
    Decision,
    HumanAction,
    HumanClick,
    HumanKey,
    HumanNavigate,
    HumanType,
    InterventionRequest,
)

from .interventions import InterventionStore

STATIC = Path(__file__).parent / "static" / "console.html"


class NavigationGate(Protocol):
    def check_navigation(self, url: str) -> Any: ...  # .decision / .reason


class _Resolve(BaseModel):
    decision: Decision
    note: str = ""


def _claimed(run: RunHandle, store: InterventionStore) -> InterventionRequest:
    """The intervention a human may act under right now, or ValueError."""
    control = run.session.control
    if control.holder != "human" or control.intervention_id is None:
        raise ValueError("automation holds the session; nothing to take over")
    req = store.get(control.intervention_id)
    if req.status != "claimed":
        raise ValueError(f"intervention {req.id} is {req.status}; take control first")
    return req


def _to_action(msg: dict[str, Any], viewport: dict[str, int]) -> HumanAction:
    """Wire message -> recorded action. Fractions become page CSS px (the viewport is fixed for
    the session)."""
    now = datetime.now(timezone.utc)
    match msg.get("t"):
        case "click":
            return HumanClick(kind="click", at=now, x=float(msg["x"]) * viewport["width"],
                              y=float(msg["y"]) * viewport["height"])
        case "type":
            return HumanType(kind="type", at=now, text=str(msg["text"]))
        case "key":
            return HumanKey(kind="key", at=now, key=str(msg["key"]))
        case "navigate":
            return HumanNavigate(kind="navigate", at=now, url=str(msg["url"]))
    raise ValueError(f"unknown input {msg.get('t')!r}")


async def _dispatch(page: Page, action: HumanAction) -> None:
    if action.kind == "click":
        await page.mouse.click(action.x, action.y)
    elif action.kind == "type":
        await page.keyboard.type(action.text)
    elif action.kind == "key":
        await page.keyboard.press(action.key)
    elif action.kind == "navigate":
        await page.goto(action.url, wait_until="load")
    if action.kind in ("click", "key"):
        # A frameset's load state never moves, so a child-frame navigation started by this
        # input has no signal to await; a short settle lets the next frame show the result.
        await asyncio.sleep(0.3)


async def _in_password_field(page: Page) -> bool:
    # page.frames drops detached frames (main_frame.child_frames does not); a frame that is
    # mid-navigation throws on evaluate and simply is not the one holding focus.
    for f in page.frames:
        try:
            if await f.evaluate("() => !!document.activeElement && document.activeElement.type === 'password'"):
                return True
        except Error:
            continue
    return False


async def handle_input(run: RunHandle, store: InterventionStore, engine: NavigationGate, msg: dict[str, Any]) -> dict[str, Any]:
    page = run.session.page
    try:
        req = _claimed(run, store)
        if msg.get("t") == "wheel":
            # A scroll changes nothing and the schema has no record for it: gated, not stored.
            await page.mouse.wheel(0, float(msg["dy"]))
            return {"ok": True, "kind": "wheel"}
        action = _to_action(msg, page.viewport_size or {"width": 0, "height": 0})
    except (ValueError, KeyError, TypeError) as e:
        return {"error": str(e) or repr(e)}
    except Error as e:
        return {"error": e.message.splitlines()[0]}
    if action.kind == "navigate":
        d = engine.check_navigation(action.url)
        if d.decision != "allow":
            run.logger.emit("human.action", intervention_id=req.id, kind="navigate", url=action.url, denied=d.reason)
            return {"error": f"navigation refused: {d.reason}"}
    # On the record BEFORE it is dispatched: an action that throws still happened. The page
    # sends one `type` per keydown, and a one-character literal is below the redactor's minimum,
    # so a password typed by hand is masked here at the record; the real text still reaches
    # the page.
    masked = action.kind == "type" and await _in_password_field(page)
    store.append_human_action(req.id, action.model_copy(update={"text": SECRET_MARK}) if masked else action)
    try:
        await _dispatch(page, action)
    except Error as e:
        return {"error": e.message.splitlines()[0]}
    return {"ok": True, "kind": action.kind}


async def _push_frames(ws: WebSocket, page: Page, lock: asyncio.Lock) -> None:
    while not page.is_closed():
        await asyncio.sleep(0.25)
        if lock.locked():
            continue  # the previous send is still in flight: drop this frame rather than queue it
        try:
            data = await page.screenshot(type="jpeg", quality=60, timeout=2000)
        except Error:
            continue  # mid-navigation; the next tick has a document
        try:
            async with lock:
                await ws.send_bytes(data)
        except Exception:  # noqa: BLE001 — the socket went away; the receive loop ends the session
            return


def build_app(run: RunHandle, store: InterventionStore, engine: NavigationGate) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    page = run.session.page

    def _req(id: str) -> InterventionRequest:
        try:
            return store.get(id)
        except KeyError:
            raise HTTPException(404, f"no intervention {id}")

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(STATIC.read_text(encoding="utf-8"))

    @app.get("/api/run")
    async def run_info() -> dict[str, Any]:
        return {
            "run_id": run.run_id, "kind": run.kind, "label": run.label,
            "control": run.session.control.snapshot(),
            "interventions": store.snapshot(), "alive": not page.is_closed(),
        }

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        async def gen() -> AsyncIterator[str]:
            q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            # Subscribe first, then replay: an event landing in between is duplicated (the page
            # dedupes by seq) rather than lost. Subscribers get the post-redaction dict.
            unsubscribe = run.logger.subscribe(q.put_nowait)
            try:
                for e in run.logger.tail(500):
                    yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                while True:
                    try:
                        e = await asyncio.wait_for(q.get(), 15)
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
            finally:
                unsubscribe()
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.get("/api/frame.jpg")
    async def frame() -> Response:
        if page.is_closed():
            return Response(status_code=204)
        try:
            data = await page.screenshot(type="jpeg", quality=60, timeout=2000)
        except Error:
            return Response(status_code=204)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.websocket("/ws/live")
    async def live(ws: WebSocket) -> None:
        # Browsers exempt WebSocket handshakes from the same-origin policy, so any page the
        # operator has open could otherwise connect to loopback and drive the claimed session.
        # Non-browser clients send no Origin and are the loopback cut documented on serve().
        if (origin := ws.headers.get("origin")) and origin != f"http://{ws.headers.get('host')}":
            await ws.close(code=1008)
            return
        await ws.accept()
        lock = asyncio.Lock()  # frames and input replies share one socket
        pusher = asyncio.create_task(_push_frames(ws, page, lock))
        try:
            while True:
                msg = await ws.receive_json()
                reply = await handle_input(run, store, engine, msg if isinstance(msg, dict) else {})
                async with lock:
                    await ws.send_json(reply)
        except WebSocketDisconnect:
            pass
        finally:
            pusher.cancel()

    @app.post("/api/interventions/{id}/claim")
    async def claim(id: str) -> dict[str, str]:
        _req(id)
        return {"id": id, "status": store.claim(id).status}

    @app.post("/api/interventions/{id}/resolve")
    async def resolve(id: str, body: _Resolve) -> dict[str, str]:
        req = _req(id)
        if body.decision == "approve" and req.reason != "RISKY_ACTION_APPROVAL":
            # Approve answers exactly one question: may this irreversible action proceed.
            # Approving a stuck step would just re-run it and burn the escalation budget.
            raise HTTPException(400, f"approve is not an answer to {req.reason}; resume or abort")
        try:
            store.resolve(id, body.decision, body.note)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"id": id, "status": req.status}

    @app.get("/api/interventions/{id}/screenshot")
    async def screenshot(id: str) -> FileResponse:
        sp = _req(id).context.screenshot_path
        p = Path(sp) if sp else None
        if p is not None and not p.is_absolute():
            p = run.evidence.root.parent / p  # EvidenceDir.relative() convention
        if p is None or not p.is_file():
            raise HTTPException(404, "no screenshot for this intervention")
        return FileResponse(p)

    return app


async def serve(app: FastAPI, *, host: str = "127.0.0.1", port: int) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    """Start uvicorn on the current loop; the task is returned so the run can cancel it.

    No auth, loopback only: an accepted, documented cut for a single-operator demo. Binding to
    127.0.0.1 keeps other hosts out, and nothing more: any local process can drive the parked
    session, so it must never bind beyond loopback. It does not keep out the operator's own
    browser — a cross-site page can open a WebSocket to loopback — which is why /ws/live checks
    the Origin header."""
    # uvicorn logs a bind failure and retries rather than raising, which leaves the caller
    # spinning on `server.started` forever. Claim the port ourselves first and fail with the fix.
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError as e:
            # SystemExit, not OSError: the CLI already turns a message-carrying SystemExit into a
            # usage error, and `except OSError` in main() would also swallow TimeoutError.
            raise SystemExit(f"operator console cannot bind {host}:{port} ({e.strerror}); "
                             f"pass --console-port with a free port") from e
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():
        await asyncio.sleep(0.02)
    if task.done():
        task.result()  # a bind failure exits the task; surface it instead of spinning
    return server, task
