"""WebSurface: the Playwright implementation of the Surface seam."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from playwright.async_api import ElementHandle, Error, Frame
from playwright.async_api import TimeoutError as PlaywrightTimeout

from understudy.schema import (
    POSITIONAL_KINDS,
    Action,
    Aliases,
    AttributeStrategy,
    CoordinateStrategy,
    CssStrategy,
    InferredLabelStrategy,
    Intent,
    NthOfRoleStrategy,
    Observation,
    PerceivedControl,
    PlaceholderStrategy,
    RoleNameStrategy,
    Scope,
    SurfaceKind,
    TableCellStrategy,
    TargetDescriptor,
    TextStrategy,
    Viewport,
)

from ..control import Holder
from ..protocol import (
    ActContext,
    ActResult,
    Attempt,
    DocStatus,
    PolicyBlocked,
    PolicyGate,
    Resolved,
    SurfaceError,
    SurfaceGone,
    TargetNotResolved,
)
from .perception import RefRegistry, await_frame, current_frame, deref, find_frame, frame_name, frame_tree, perceive
from .resolver import Skipped, Vocab, mapped_path, resolve, run_strategy
from .session import BrowserSession

# Label sources that name a control by its layout; the others are covered by their own rung.
_LAYOUT_SOURCES = {"label_for", "label_wrap", "row_cell", "preceding_text", "geometric"}
_CSS_PATH_JS = (
    "e => { const parts = []; let n = e;"
    " while (n && n.nodeType === 1 && parts.length < 4 && n.tagName !== 'BODY' && n.tagName !== 'HTML') {"
    "   let i = 1, s = n; while ((s = s.previousElementSibling)) if (s.tagName === n.tagName) i++;"
    "   parts.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + i + ')'); n = n.parentElement; }"
    " return parts.join(' > '); }"
)
_ROW_ANCHOR_JS = "e => { const r = e.closest('tr'); const c = r && r.querySelector('td,th'); return c ? c.innerText.trim() : ''; }"


class WebSurface:
    kind: SurfaceKind = "legacy_web"

    def __init__(
        self,
        session: BrowserSession,
        *,
        policy_gate: PolicyGate,
        caller: Holder = "automation",
        perception_cfg: dict[str, Any] | None = None,
    ) -> None:
        self.session = session
        self.policy_gate = policy_gate
        # One gate per session: the redirect listener in BrowserSession must judge by the same
        # gate as act(), or a caller that wires only the surface gets redirects unchecked.
        if session.policy_gate is None:
            session.policy_gate = policy_gate
        self.caller = caller
        self._vocab = Vocab(perception_cfg=dict(perception_cfg or {}))
        self._gen = 0
        self._registry: RefRegistry = {}
        self._sensitive: list[TargetDescriptor] = []

    @property
    def page(self):
        return self.session.page

    # ---- observe / resolve / act ------------------------------------------------------------

    def _guard(self, action_type: str) -> None:
        """Nothing is read from a page that policy closed, or that closed underneath the run: a
        blank observation of the denied URL must never reach a model or a checkpoint."""
        v = self.session.violation
        if v is not None:
            raise PolicyBlocked("deny", v["reason"], action_type=action_type, url=v["url"])
        if self.page.is_closed():
            raise SurfaceGone("browser closed")

    async def observe(self, *, include_screenshot: bool = False) -> Observation:
        self._guard("observe")
        self._gen += 1
        try:
            obs, self._registry = await perceive(self.session, cfg=self._vocab.perception_cfg, gen=self._gen)
        except SurfaceGone:
            self._guard("observe")  # closed by a violation mid-observe: a policy block, not a crash
            raise
        if include_screenshot:
            obs.screenshot_b64 = base64.b64encode(await self.screenshot(mask_sensitive=True)).decode()
        return obs

    async def _frame_for(self, target: TargetDescriptor) -> Frame:
        path = mapped_path(target.frame_path, self._vocab.frame_map)
        frame = await find_frame(self.page, path)
        if frame is None:
            raise TargetNotResolved(target, [Attempt("frame", 0, f"frame /{'/'.join(path)} not found")])
        return frame

    async def resolve(self, target: TargetDescriptor) -> Resolved:
        self._guard("resolve")
        return await resolve(await self._frame_for(target), target, self._vocab)

    async def act(self, action: Action, ctx: ActContext) -> ActResult:
        self.session.control.assert_held_by(self.caller)
        if action.type in ("wait", "assert", "extract"):
            raise ValueError(f"{action.type} is not a surface action")
        self._guard(action.type)
        url = action.url if action.type == "navigate" else await self.url()
        decision = self.policy_gate.check(
            action_type=action.type, url=url, risk=ctx.risk, mode=ctx.mode, approval_granted=ctx.approval_granted
        )
        if decision.decision == "deny":
            raise PolicyBlocked("deny", decision.reason, action_type=action.type, url=url)
        if decision.decision == "require_approval" and not ctx.approval_granted:
            raise PolicyBlocked("require_approval", decision.reason, action_type=action.type, url=url)

        resolved: Resolved | None = None
        try:
            if action.type == "navigate":
                # "load", not "domcontentloaded": for a frameset the main frame's load event is
                # the only signal that its children have their first document.
                await self.page.goto(action.url, timeout=ctx.timeout_ms, wait_until="load")
            else:
                if action.target is not None:
                    resolved = await self.resolve(action.target)
                if ctx.expects_navigation:
                    # Arm a frame-scoped wait BEFORE dispatching. The tempting shortcut —
                    # asyncio.gather(page.wait_for_load_state(), handle.click()) — is banned in
                    # this repo: page load state does not track child-frame navigation, so it
                    # returns while the content frame is still on its old URL and the next read
                    # throws "Execution context was destroyed". Any frame may be the one that
                    # moves (a link in `nav` with target="content"), so we accept any real
                    # navigation and then re-acquire THAT frame by name: a frameset reload
                    # recreates its children.
                    async with self.page.expect_event(
                        "framenavigated", predicate=lambda f: not f.url.startswith("about:"), timeout=ctx.timeout_ms
                    ) as event:
                        await self._dispatch(action, resolved, ctx)
                    moved = await event.value
                    fallback = resolved.frame_path if resolved else []
                    frame = await await_frame(self.page, await self._path_of(moved, fallback), ctx.timeout_ms)
                    await frame.wait_for_load_state("domcontentloaded", timeout=ctx.timeout_ms)
                else:
                    await self._dispatch(action, resolved, ctx)
                    if action.type == "click":
                        await asyncio.sleep(0.25)  # quiet settle for in-page effects
        except PlaywrightTimeout as e:
            raise SurfaceError(
                f"{action.type} did not complete within {ctx.timeout_ms}ms",
                expected="a frame navigation" if ctx.expects_navigation else f"{action.type} to complete",
                observed=e.message.splitlines()[0],
            ) from e
        except Error as e:
            if self.session.violation is not None:
                v = self.session.violation
                raise PolicyBlocked("deny", v["reason"], action_type=action.type, url=v["url"]) from e
            msg = e.message.splitlines()[0]
            if "closed" in msg:
                raise SurfaceGone(msg) from e
            raise SurfaceError(f"{action.type} failed: {msg}", expected=f"{action.type} to complete", observed=msg) from e
        self._guard(action.type)  # a redirect the artifact never asked for may have been denied meanwhile
        url_after = await self.url()
        doc = self.session.last_document_status()
        return ActResult(
            resolved=resolved, navigated=action.type == "navigate" or ctx.expects_navigation or url_after != url,
            url_after=url_after, document_status=doc.status if doc else None,
        )

    async def _dispatch(self, action: Action, resolved: Resolved | None, ctx: ActContext) -> None:
        handle: ElementHandle | None = resolved.handle if resolved else None
        t = ctx.timeout_ms
        if action.type == "click":
            await handle.click(timeout=t)
        elif action.type == "type":
            if action.clear_first:
                await handle.fill(action.value, timeout=t)
            else:
                await handle.focus()
                await self.page.keyboard.type(action.value)
        elif action.type == "select":
            await handle.select_option(action.value, timeout=t)
        elif action.type == "press":
            if handle is not None:
                await handle.press(action.key, timeout=t)
            else:
                await self.page.keyboard.press(action.key)

    async def _path_of(self, frame: Frame, fallback: list[str]) -> list[str]:
        path: list[str] = []
        while frame.parent_frame is not None:
            name = await frame_name(frame)
            if not name:  # detached before we could read it; the target's own frame is the best guess
                return fallback
            path.insert(0, name)
            frame = frame.parent_frame
        return path

    # ---- reads ------------------------------------------------------------------------------

    async def read_text(self, target: TargetDescriptor) -> str | None:
        if target.intent != "read":
            raise ValueError("read_text requires a target captured with intent='read'")
        r = await self.resolve(target)
        text = await r.handle.evaluate("e => e.matches('input,select,textarea') ? e.value : e.innerText")
        return text.strip()

    async def read_attribute(self, target: TargetDescriptor, attr: str) -> str | None:
        r = await self.resolve(target)
        return await r.handle.get_attribute(attr)

    async def url(self) -> str:
        return current_frame(self.session).url

    async def title(self) -> str:
        try:
            return await current_frame(self.session).title()
        except Error:
            return ""

    async def last_document_status(self) -> DocStatus | None:
        return self.session.last_document_status()

    async def screenshot(self, *, mask_sensitive: bool = True) -> bytes:
        self._guard("screenshot")
        if not mask_sensitive:
            return await self.page.screenshot(type="png")
        frames = [f for f in self.page.frames if not f.is_detached()]
        marked: list[ElementHandle] = []
        for td in self._sensitive:
            try:
                handles = await self._every_match(td)
            except (SurfaceError, Error):
                continue  # a sensitive control not on this screen has nothing to mask
            for h in handles:
                try:
                    await h.evaluate("e => e.setAttribute('data-us-mask', '')")
                    marked.append(h)
                except Error:
                    pass
        try:
            masks = [f.locator("input[type=password], [data-us-mask]") for f in frames]
            return await self.page.screenshot(type="png", mask=masks)
        finally:
            for h in marked:
                try:
                    await h.evaluate("e => e.removeAttribute('data-us-mask')")
                except Error:
                    pass

    async def _every_match(self, td: TargetDescriptor) -> list[ElementHandle]:
        """Every element the first productive rung finds — not resolve()'s exactly-one. Masking
        two controls that share a label must cover both, never neither."""
        frame = await self._frame_for(td)
        for s in td.strategies:
            try:
                handles = await run_strategy(frame, s, self._vocab)
            except Skipped:
                continue
            if handles:
                return handles
        return []

    async def dom_snapshot(self) -> str:
        self._guard("dom_snapshot")
        parts = []
        for path, frame, _ in await frame_tree(self.page):
            try:
                html = await frame.content()
            except Error:
                continue
            parts.append(f"<!-- frame /{'/'.join(path)} {frame.url} -->\n{html}")
        return "\n".join(parts)

    async def visible_text(self, frame_path: list[str] | None = None) -> str:
        self._guard("visible_text")
        wanted = mapped_path(frame_path, self._vocab.frame_map) if frame_path is not None else None
        parts: list[str] = []
        for path, frame, _ in await frame_tree(self.page):
            if wanted is not None and path != wanted:
                continue
            try:
                parts.append(await frame.evaluate("() => (document.body && document.body.innerText) || ''"))
            except Error:
                continue
        return "\n".join(parts)

    # ---- descriptors --------------------------------------------------------------------------

    async def capture_descriptor(self, ref: str, observation: Observation, *, intent: Intent) -> TargetDescriptor:
        self._guard("capture_descriptor")
        entry = self._registry.get(ref)
        if entry is None or observation.gen != self._gen or entry.gen != self._gen:
            raise LookupError(f"ref {ref!r} is not from the current observation; re-observe")
        control = observation.control(ref)
        if control is None:
            raise LookupError(f"ref {ref!r} is not a control")
        path = list(entry.frame_path)
        frame = await find_frame(self.page, path)
        if frame is None:
            raise LookupError(f"frame /{'/'.join(path)} of ref {ref!r} is gone; re-observe")
        target = await deref(frame, entry)

        kept = []
        for s in await self._candidates(frame, control, target):
            try:
                handles = await run_strategy(frame, s, self._vocab)
            except Skipped:
                continue
            # A strategy that does not select the target right now never enters the artifact.
            if not handles or not await target.evaluate("(t, hs) => hs.some(h => h === t)", handles):
                continue
            kept.append(s.model_copy(update={"matches_at_capture": len(handles)}))
        scope = None
        if any(s.matches_at_capture > 1 for s in kept):
            anchor = control.table_position.row_anchor if control.table_position else await target.evaluate(_ROW_ANCHOR_JS)
            if anchor:
                scope = Scope(within_row_matching=anchor)
        kept.sort(key=lambda s: -s.confidence)
        if intent == "read":
            kept = [s for s in kept if s.kind not in POSITIONAL_KINDS]
        if not kept:
            raise LookupError(f"no {'semantic ' if intent == 'read' else ''}strategy selects ref {ref!r}; it cannot be recorded")
        return TargetDescriptor(
            role=control.role,
            label=control.inferred_label or control.name or control.attrs.get("name", ""),
            rationale=_rationale(control, kept),
            frame_path=path, intent=intent, scope=scope, strategies=kept,
        )

    async def _candidates(self, frame: Frame, c: PerceivedControl, target: ElementHandle) -> list[Any]:
        role = c.role
        out: list[Any] = []
        if c.name:
            out.append(RoleNameStrategy(kind="role_name", role=role, name=c.name, confidence=0.95, origin="captured"))
        if c.inferred_label and c.label_source in _LAYOUT_SOURCES:
            out.append(InferredLabelStrategy(kind="inferred_label", role=role, label=c.inferred_label, confidence=0.90, origin="captured"))
        if c.attrs.get("placeholder"):
            out.append(PlaceholderStrategy(kind="placeholder", text=c.attrs["placeholder"], confidence=0.88, origin="captured"))
        if c.table_position:
            tp = c.table_position
            out.append(TableCellStrategy(
                kind="table_cell", row_anchor=tp.row_anchor, column=tp.column, anchor_column=tp.anchor_column,
                inner_role=None if role == "cell" else role, confidence=0.85, origin="captured",
            ))
        if role in ("link", "button") and c.name:
            out.append(TextStrategy(kind="text", text=c.name, role=role, confidence=0.80, origin="captured"))
        for attr in ("name", "id"):
            if c.attrs.get(attr):
                out.append(AttributeStrategy(kind="attribute", attr=attr, value=c.attrs[attr], role=role, confidence=0.70, origin="captured"))
        if role == "button" and c.attrs.get("value"):
            out.append(AttributeStrategy(kind="attribute", attr="value", value=c.attrs["value"], role=role, confidence=0.70, origin="captured"))
        # Derived, positional. Same enumeration the nth_of_role rung uses, or the index is off.
        peers = await frame.get_by_role(role).filter(visible=True).element_handles()
        index = await target.evaluate("(t, hs) => hs.indexOf(t)", peers)
        if index >= 0:
            out.append(NthOfRoleStrategy(kind="nth_of_role", role=role, index=index, confidence=0.55, origin="derived"))
        css = await target.evaluate(_CSS_PATH_JS)
        if css:
            out.append(CssStrategy(kind="css", selector=css, confidence=0.40, origin="derived"))
        vp = self.page.viewport_size
        if vp:
            out.append(CoordinateStrategy(
                kind="coordinate", x=c.box.x + c.box.width / 2, y=c.box.y + c.box.height / 2,
                viewport=Viewport(width=vp["width"], height=vp["height"]), confidence=0.20, origin="derived",
            ))
        return out

    # ---- configuration ------------------------------------------------------------------------

    def register_sensitive_target(self, target: TargetDescriptor) -> None:
        self._sensitive.append(target)

    def set_tenant_vocabulary(self, aliases: Aliases, frame_map: dict[str, str]) -> None:
        self._vocab = Vocab(aliases=aliases, frame_map=dict(frame_map), perception_cfg=self._vocab.perception_cfg)


def _rationale(c: PerceivedControl, kept: list[Any]) -> str:
    lab = c.inferred_label
    by_source = {
        "label_for": f'<label for> "{lab}"',
        "label_wrap": f'wrapping <label> "{lab}"',
        "row_cell": f'adjacent-cell label "{lab}"; no <label for> on this markup',
        "preceding_text": f'preceding text "{lab}"',
        "geometric": f'nearest text left/above "{lab}" (geometric)',
        "value": f'button value "{lab}"',
        "aria": f'accessible name "{lab}"',
    }
    bits = []
    if c.name and c.label_source not in ("value", "aria"):
        bits.append(f'accessible name "{c.name}"')
    if c.label_source in by_source:
        bits.append(by_source[c.label_source])
    if c.table_position:
        tp = c.table_position
        bits.append(f'row "{tp.row_anchor}" ({tp.anchor_column}) x column "{tp.column}"')
    attr = next((s for s in kept if s.kind == "attribute"), None)
    if attr:
        bits.append(f"{attr.attr}={attr.value} as fallback")
    if kept[0].kind in POSITIONAL_KINDS:
        bits.append("no semantic anchor found; position only — re-record if this drifts")
    return "; ".join(bits)
