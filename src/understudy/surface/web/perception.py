"""One observation of every frame on the page, plus the ref registry that maps each one-turn
ref back to a live element."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import ElementHandle, Error, Frame, Page

from understudy.schema import Box, FramePerception, Observation, PerceivedControl, PerceivedTable, TablePosition

from ..protocol import SurfaceError, SurfaceGone
from .session import BrowserSession

PERCEIVE_JS = (Path(__file__).parent.parent / "perceive.js").read_text(encoding="utf-8")
DEREF_JS = (
    "([i, g]) => { if (!window.__us || window.__us.gen !== g) throw Error('stale-generation');"
    " const el = window.__us.nodes[i]; if (!el || !el.isConnected) throw Error('stale-node'); return el; }"
)


@dataclass(frozen=True)
class RefEntry:
    frame_path: tuple[str, ...]
    idx: int
    kind: str  # control | cell | table
    gen: int


RefRegistry = dict[str, RefEntry]


# ---- frames ---------------------------------------------------------------------------------


async def frame_name(frame: Frame) -> str:
    """Playwright fills `name` on the first navigation commit; the <frame> element carries it
    from the moment it is attached. Reading the attribute is what makes the path semantic even
    when we look before the child has navigated."""
    if frame.parent_frame is None:
        return ""
    if frame.name:
        return frame.name
    try:
        el = await frame.frame_element()
        return await el.evaluate("e => e.getAttribute('name') || e.id || ''")
    except Error:
        return ""


def _slug(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", url.rsplit("/", 1)[-1].lower()).strip("-") or "blank"


async def frame_tree(page: Page) -> list[tuple[list[str], Frame, str | None]]:
    """(path, frame, warning) for every attached frame, top first, DOM order."""
    out: list[tuple[list[str], Frame, str | None]] = [([], page.main_frame, None)]

    async def walk(frame: Frame, path: list[str]) -> None:
        for child in frame.child_frames:
            if child.is_detached():
                continue
            name = await frame_name(child)
            warning = None
            if not name:
                name = "url:" + _slug(child.url)
                warning = f"unnamed frame {child.url} perceived as {name!r}"
            out.append((path + [name], child, warning))
            await walk(child, path + [name])

    await walk(page.main_frame, [])
    return out


async def find_frame(page: Page, path: list[str]) -> Frame | None:
    frame = page.main_frame
    for seg in path:
        for child in frame.child_frames:
            if await frame_name(child) == seg:
                frame = child
                break
        else:
            return None
    return frame


async def await_frame(page: Page, path: list[str], timeout_ms: int) -> Frame:
    """Re-acquire a frame by name after a navigation: a frameset reload destroys and recreates
    its children, and there is a moment when the new one is not there yet."""
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while True:
        frame = await find_frame(page, path)
        if frame is not None:
            return frame
        if asyncio.get_running_loop().time() > deadline:
            raise SurfaceError(
                f"frame /{'/'.join(path)} did not reappear within {timeout_ms}ms",
                expected=f"frame /{'/'.join(path)} attached",
                observed=f"frames: {[f.name for f in page.frames]}",
            )
        await asyncio.sleep(0.05)


def _depth(frame: Frame) -> int:
    d = 0
    while frame.parent_frame is not None:
        frame = frame.parent_frame
        d += 1
    return d


def current_frame(session: BrowserSession) -> Frame:
    """The deepest navigated non-about frame, most recently navigated on a tie. On a frameset
    the main frame's URL never changes and means nothing."""
    best = session.page.main_frame
    key = (-1, -1)
    for f in session.page.frames:
        if f.is_detached() or f.url.startswith("about:"):
            continue
        k = (_depth(f), session.nav_seq.get(f, -1))
        if k > key:
            key, best = k, f
    return best


async def frame_offset(frame: Frame) -> tuple[float, float]:
    """Offset of the frame's own element, for page_box. Playwright's bounding_box() is already
    main-frame relative, so the leaf element is the whole offset; walking up the parent chain
    would count every outer frame twice."""
    if frame.parent_frame is None:
        return 0.0, 0.0
    try:
        box = await (await frame.frame_element()).bounding_box()
    except Error:
        box = None
    return (box["x"], box["y"]) if box else (0.0, 0.0)


# ---- the pass -------------------------------------------------------------------------------


async def _evaluate(frame: Frame, cfg: dict[str, Any], path: list[str], warnings: list[str]) -> dict[str, Any] | None:
    for attempt in (1, 2):
        try:
            await frame.wait_for_load_state("domcontentloaded", timeout=5000)
            return await frame.evaluate(PERCEIVE_JS, cfg)
        except Error as e:
            # "Execution context was destroyed": the frame navigated under us. Once is timing;
            # twice is a frame we cannot read this turn. A frame detached meanwhile is not
            # part of the page any more, so that is not worth a warning. A closed page is not
            # a warning either: a zero-frame observation of it must never reach a model.
            msg = e.message.splitlines()[0]
            if "closed" in msg:
                raise SurfaceGone(msg) from e
            if frame.is_detached():
                return None
            if attempt == 2:
                warnings.append(f"frame /{'/'.join(path)} not perceived: {msg}")
                return None
            await asyncio.sleep(0.1)
    return None


async def perceive(session: BrowserSession, *, cfg: dict[str, Any], gen: int) -> tuple[Observation, RefRegistry]:
    page = session.page
    registry: RefRegistry = {}
    warnings: list[str] = []
    frames: list[FramePerception] = []
    counter = 0

    for path, frame, warning in await frame_tree(page):
        if warning:
            warnings.append(warning)
        data = await _evaluate(frame, {**cfg, "gen": gen}, path, warnings)
        if data is None:
            continue
        dx, dy = await frame_offset(frame)
        idx_to_ref: dict[int, str] = {}

        def mint(idx: int, kind: str) -> str:
            nonlocal counter
            if idx in idx_to_ref:
                return idx_to_ref[idx]
            counter += 1
            ref = f"c{counter}"
            registry[ref] = RefEntry(tuple(path), idx, kind, gen)
            idx_to_ref[idx] = ref
            return ref

        controls: list[PerceivedControl] = []
        for c in data["controls"]:
            box = Box(**c["box"])
            controls.append(PerceivedControl(
                ref=mint(c["idx"], "cell" if c["role"] == "cell" else "control"),
                frame_path=path, role=c["role"], name=c["name"],
                inferred_label=c["inferred_label"], label_source=c["label_source"],
                label_confidence=c["label_confidence"], attrs=c["attrs"], box=box,
                page_box=Box(x=box.x + dx, y=box.y + dy, width=box.width, height=box.height),
                enabled=c["enabled"], value=c["value"],
                table_position=TablePosition(**c["table_position"]) if c["table_position"] else None,
            ))
        tables = [
            PerceivedTable(
                ref=mint(t["idx"], "table"), frame_path=path, caption=t["caption"], columns=t["columns"],
                anchor_column=t["anchor_column"], rows=t["rows"],
                # Only cells that are controls: a cell whose sole content is an input was skipped
                # above, and a ref observation.control() cannot find is a ref nobody can act on.
                cell_refs=[{h: idx_to_ref[i] for h, i in row.items() if i in idx_to_ref} for row in t["cell_refs"]],
            )
            for t in data["tables"]
        ]
        frames.append(FramePerception(
            path=path, url=data["url"], title=data["title"], text_digest=data["text_digest"],
            controls=controls, tables=tables,
        ))

    focus = current_frame(session)
    try:
        title = await focus.title()
    except Error:
        title = ""
    doc = session.last_document_status()
    obs = Observation(
        gen=gen, url=focus.url, title=title, frames=frames,
        document_status=doc.status if doc else None, warnings=warnings,
    )
    return obs, registry


async def deref(frame: Frame, entry: RefEntry) -> ElementHandle:
    """The live element behind a ref. Raises LookupError when the ref is from an older
    observation or the element left the DOM — never resolves to whatever sits there now."""
    try:
        handle = await frame.evaluate_handle(DEREF_JS, [entry.idx, entry.gen])
    except Error as e:
        raise LookupError(f"ref is stale ({e.message.splitlines()[0]}); re-observe") from e
    el = handle.as_element()
    if el is None:
        raise LookupError("ref does not point at an element; re-observe")
    return el
