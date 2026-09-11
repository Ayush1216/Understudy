"""ONE evaluator for all six assertion sites: step preconditions, step postcondition, wait/assert
bodies, recovery triggers, outcome detectors, and the success checkpoint.

`observed` is always a human sentence, produced here by the code that made the decision, so a
failure result reads "expected X; observed Y" without anyone opening a screenshot.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

from understudy.schema import (
    Aliases,
    All,
    AnyOf,
    Checkpoint,
    ControlAbsent,
    ControlPresent,
    HttpStatus,
    Not,
    TargetDescriptor,
    TextAbsent,
    TextPresent,
    TitleMatches,
    UrlMatches,
)
from understudy.surface import Surface, TargetNotResolved

TICK_S = 0.15


def head(text: str, n: int = 120) -> str:
    return " ".join(text.split())[:n]


def describe(cp: Checkpoint) -> str:
    """The `expected` prose for a checkpoint."""
    if isinstance(cp, (TextPresent, TextAbsent)):
        return f'text "{cp.text}" {"present" if cp.kind == "text_present" else "absent"}{_where(cp.frame_path)}'
    if isinstance(cp, (ControlPresent, ControlAbsent)):
        t = cp.target
        return f'{t.role} "{t.label or t.strategies[0].kind}" {"present" if cp.kind == "control_present" else "absent"}{_where(t.frame_path)}'
    if isinstance(cp, UrlMatches):
        return f"URL matching /{cp.pattern}/"
    if isinstance(cp, TitleMatches):
        return f"title matching /{cp.pattern}/"
    if isinstance(cp, HttpStatus):
        return f"HTTP status in {cp.min}..{cp.max}"
    if isinstance(cp, All):
        return "all of [" + "; ".join(describe(c) for c in cp.of) + "]"
    if isinstance(cp, AnyOf):
        return "any of [" + "; ".join(describe(c) for c in cp.of) + "]"
    return f"not ({describe(cp.of)})"


def _where(frame_path: list[str] | None) -> str:
    return "" if frame_path is None else f" in frame /{'/'.join(frame_path)}"


def _matches(needle: str, text: str, how: str) -> bool:
    if how == "regex":
        return re.search(needle, text) is not None
    if how == "exact":
        return any(line.strip() == needle for line in text.splitlines())
    return needle in text


async def evaluate(cp: Checkpoint, surface: Surface, *, aliases: Aliases) -> tuple[bool, str]:
    """(ok, observed). Never throws: a surface that cannot be inspected is a failed checkpoint
    with that fact as the observation."""
    try:
        return await _evaluate(cp, surface, aliases)
    except Exception as e:  # noqa: BLE001 — playwright errors are not SurfaceErrors, and neither may end a poll
        return False, f"surface could not be inspected: {e}"


async def _evaluate(cp: Checkpoint, s: Surface, aliases: Aliases) -> tuple[bool, str]:
    if isinstance(cp, (TextPresent, TextAbsent)):
        text = await s.visible_text(cp.frame_path)
        found = any(_matches(t, text, cp.match) for t in (cp.text, *aliases.texts.get(cp.text, [])))
        where = _where(cp.frame_path)
        if found:
            return cp.kind == "text_present", f'text "{cp.text}" present{where}'
        return cp.kind == "text_absent", f'text "{cp.text}" absent{where}; page shows: {head(text)}'
    if isinstance(cp, (ControlPresent, ControlAbsent)):
        t = cp.target
        try:
            await s.resolve(t)
            found = True
        except TargetNotResolved:
            found = False
        observed = f'{t.role} "{t.label or t.strategies[0].kind}" {"present" if found else "absent"}{_where(t.frame_path)}'
        return found == (cp.kind == "control_present"), observed
    if isinstance(cp, UrlMatches):
        url = await s.url()
        return re.search(cp.pattern, url) is not None, f"URL is {url}"
    if isinstance(cp, TitleMatches):
        title = await s.title()
        return re.search(cp.pattern, title) is not None, f'title is "{title}"'
    if isinstance(cp, HttpStatus):
        doc = await s.last_document_status()
        if doc is None:
            return False, "no document status recorded"
        return cp.min <= doc.status <= cp.max, f"HTTP {doc.status} from {doc.url}"
    if isinstance(cp, All):
        seen = []
        for c in cp.of:
            ok, observed = await _evaluate(c, s, aliases)
            if not ok:
                return False, observed
            seen.append(observed)
        return True, "; ".join(seen)
    if isinstance(cp, AnyOf):
        seen = []
        for c in cp.of:
            ok, observed = await _evaluate(c, s, aliases)
            if ok:
                return True, observed
            seen.append(observed)
        return False, "; ".join(seen)
    ok, observed = await _evaluate(cp.of, s, aliases)  # Not
    return not ok, observed


async def poll(cp: Checkpoint, surface: Surface, *, aliases: Aliases) -> tuple[bool, str]:
    """Poll the ROOT checkpoint's wait_ms; 0 means evaluate once."""
    deadline = monotonic() + cp.wait_ms / 1000
    while True:
        ok, observed = await evaluate(cp, surface, aliases=aliases)
        if ok or monotonic() >= deadline:
            return ok, observed
        await asyncio.sleep(TICK_S)


class TickView:
    """One read per tick: every checkpoint the executor evaluates in a tick (outcomes, recovery
    triggers, the error check, the postcondition) sees the same page, in that order."""

    def __init__(self, surface: Surface) -> None:
        self._s = surface
        self._text: dict[tuple[str, ...] | None, str] = {}
        self._memo: dict[str, Any] = {}

    async def visible_text(self, frame_path: list[str] | None = None) -> str:
        key = None if frame_path is None else tuple(frame_path)
        if key not in self._text:
            self._text[key] = await self._s.visible_text(frame_path)
        return self._text[key]

    async def url(self) -> str:
        return await self._cached("url", self._s.url)

    async def title(self) -> str:
        return await self._cached("title", self._s.title)

    async def last_document_status(self):
        return await self._cached("doc", self._s.last_document_status)

    async def resolve(self, target: TargetDescriptor):
        return await self._s.resolve(target)

    async def _cached(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        if key not in self._memo:
            self._memo[key] = await fn()
        return self._memo[key]
