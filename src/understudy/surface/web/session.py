"""One browser, one context, one page, one control lease. The operator console is handed this
same object — no console route ever launches a browser."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from playwright.async_api import Browser, BrowserContext, Error, Frame, Page, Playwright, Response, async_playwright

from understudy.schema import Viewport

from ..control import SessionControl
from ..protocol import DocStatus, PolicyGate


class BrowserSession:
    def __init__(
        self,
        pw: Playwright,
        browser: Browser,
        context: BrowserContext,
        page: Page,
        *,
        policy_gate: PolicyGate | None,
        on_violation: Callable[[str, str], None] | None,
    ) -> None:
        self._pw = pw
        self.browser = browser
        self.context = context
        self.page = page
        self.policy_gate = policy_gate
        self.on_violation = on_violation
        self.control = SessionControl()
        # Set when a navigation the artifact never asked for (a redirect) was denied by policy.
        self.violation: dict[str, str] | None = None
        self._docs: dict[Frame, DocStatus] = {}
        # Frame -> order of its most recent navigation. Breaks the tie between sibling frames
        # when picking "the" current URL of a frameset.
        self.nav_seq: dict[Frame, int] = {}
        self._nav_counter = 0
        self._closing: asyncio.Task[None] | None = None
        page.on("response", self._on_response)
        page.on("framenavigated", self._on_navigated)
        context.on("page", self._on_page)

    @classmethod
    async def launch(
        cls,
        *,
        headless: bool = True,
        viewport: Viewport | None = None,
        policy_gate: PolicyGate | None = None,
        on_violation: Callable[[str, str], None] | None = None,
        slow_mo_ms: int = 0,
    ) -> BrowserSession:
        vp = viewport or Viewport()
        pw = await async_playwright().start()
        # slow_mo is a demo affordance only: it pauses between operations so a person can watch
        # a replay that otherwise finishes in under two seconds. It changes no timing the run
        # depends on — waits and checkpoint polls have their own clocks.
        browser = await pw.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        context = await browser.new_context(viewport={"width": vp.width, "height": vp.height})
        page = await context.new_page()
        return cls(pw, browser, context, page, policy_gate=policy_gate, on_violation=on_violation)

    def last_document_status(self) -> DocStatus | None:
        """Worst status among the documents currently on screen. On a frameset both children
        load in one navigation, and a content-frame 500 must not be hidden by whichever sibling
        happened to commit last."""
        self._docs = {f: d for f, d in self._docs.items() if not f.is_detached()}
        return max(self._docs.values(), key=lambda d: d.status, default=None)

    def _on_response(self, response: Response) -> None:
        if response.request.resource_type == "document":
            self._docs[response.frame] = DocStatus(url=response.url, status=response.status)

    def _on_page(self, page: Page) -> None:
        """A target="_blank" link opens a second page with this session's cookies; it is judged
        by the same gate, or the allowlist would have a hole the width of a popup. Playwright
        reports a popup only once it has reached its first URL, so that URL is checked now —
        the framenavigated for it has already fired."""
        page.on("framenavigated", self._on_navigated)
        self._on_navigated(page.main_frame)

    def _on_navigated(self, frame: Frame) -> None:
        if frame.url.startswith("about:"):
            return
        self._nav_counter += 1
        self.nav_seq[frame] = self._nav_counter
        if self.policy_gate is None or self.violation is not None:
            return
        decision = self.policy_gate.check(action_type="navigate", url=frame.url, risk="safe", mode="replay")
        if decision.decision == "deny":
            self.violation = {"url": frame.url, "reason": decision.reason}
            if self.on_violation:
                self.on_violation(frame.url, decision.reason)
            # Fail closed: the page has already left the allowlist, so the run ends here.
            self._start_close()

    def _start_close(self) -> asyncio.Task[None]:
        if self._closing is None:
            self._closing = asyncio.get_running_loop().create_task(self._close())
        return self._closing

    async def close(self) -> None:
        await self._start_close()

    async def _close(self) -> None:
        try:
            await self.browser.close()
        except Error:
            pass
        await self._pw.stop()
