"""The resolution ladder. Walk a descriptor's strategies in order; accept the first that yields
exactly one visible match (after scoping when it yields more). Which rung won is the drift
signal; positional rungs are refused when the intent is to read."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import ElementHandle, Frame, Locator

from understudy.schema import (
    POSITIONAL_KINDS,
    Aliases,
    AttributeStrategy,
    CoordinateStrategy,
    CssStrategy,
    InferredLabelStrategy,
    NthOfRoleStrategy,
    PlaceholderStrategy,
    RoleNameStrategy,
    Scope,
    TableCellStrategy,
    TargetDescriptor,
    TextStrategy,
)

from ..protocol import AmbiguousTarget, Attempt, Resolved, TargetNotResolved
from .perception import PERCEIVE_JS


@dataclass
class Vocab:
    """Tenant deltas consumed inside resolution, plus the perception selectors the JS rungs use."""

    aliases: Aliases = field(default_factory=Aliases)
    frame_map: dict[str, str] = field(default_factory=dict)
    perception_cfg: dict[str, Any] = field(default_factory=dict)


def mapped_path(path: list[str], frame_map: dict[str, str]) -> list[str]:
    key = "/".join(path)
    if key in frame_map:
        return [seg for seg in frame_map[key].split("/") if seg]
    return list(path)


class Skipped(Exception):
    """A rung that declines to run (not a miss). The message goes on the Attempt."""


def _alts(base: str, table: dict[str, list[str]]) -> list[str]:
    return [base, *table.get(base, [])]


async def _first_nonempty(locators: list[Locator]) -> list[ElementHandle]:
    # Alternatives (a base name and its tenant aliases) are tried in order rather than unioned,
    # so one element matched under two names is never counted twice.
    for loc in locators:
        handles = await loc.filter(visible=True).element_handles()
        if handles:
            return handles
    return []


async def _query(frame: Frame, vocab: Vocab, query: dict[str, Any]) -> list[ElementHandle]:
    """Run perceive.js in query mode: same label/table inference as perception, no registry."""
    result = await frame.evaluate_handle(PERCEIVE_JS, {**vocab.perception_cfg, "query": query})
    props = await result.get_properties()
    handles = [(int(k), h.as_element()) for k, h in props.items() if k.isdigit()]
    return [h for _, h in sorted(handles) if h is not None]


async def _role_name(frame: Frame, s: RoleNameStrategy, v: Vocab) -> list[ElementHandle]:
    return await _first_nonempty(
        [frame.get_by_role(s.role, name=n, exact=s.exact) for n in _alts(s.name, v.aliases.texts)]
    )


async def _inferred_label(frame: Frame, s: InferredLabelStrategy, v: Vocab) -> list[ElementHandle]:
    return await _query(frame, v, {"kind": "inferred_label", "role": s.role, "labels": _alts(s.label, v.aliases.labels)})


async def _placeholder(frame: Frame, s: PlaceholderStrategy, v: Vocab) -> list[ElementHandle]:
    return await _first_nonempty([frame.get_by_placeholder(s.text, exact=True)])


async def _table_cell(frame: Frame, s: TableCellStrategy, v: Vocab) -> list[ElementHandle]:
    return await _query(frame, v, {
        "kind": "table_cell",
        "row_anchor": _alts(s.row_anchor, v.aliases.texts),
        "column": _alts(s.column, v.aliases.columns),
        "anchor_column": _alts(s.anchor_column, v.aliases.columns) if s.anchor_column else None,
        "inner_role": s.inner_role,
    })


async def _text(frame: Frame, s: TextStrategy, v: Vocab) -> list[ElementHandle]:
    locators = []
    for t in _alts(s.text, v.aliases.texts):
        loc = frame.get_by_text(t, exact=s.exact)
        locators.append(loc.and_(frame.get_by_role(s.role)) if s.role else loc)
    return await _first_nonempty(locators)


async def _attribute(frame: Frame, s: AttributeStrategy, v: Vocab) -> list[ElementHandle]:
    value = s.value.replace("\\", "\\\\").replace('"', '\\"')
    loc = frame.locator(f'[{s.attr}="{value}"]')
    return await _first_nonempty([loc.and_(frame.get_by_role(s.role)) if s.role else loc])


async def _nth_of_role(frame: Frame, s: NthOfRoleStrategy, v: Vocab) -> list[ElementHandle]:
    return await frame.get_by_role(s.role).filter(visible=True).nth(s.index).element_handles()


async def _css(frame: Frame, s: CssStrategy, v: Vocab) -> list[ElementHandle]:
    return await _first_nonempty([frame.locator(s.selector)])


async def _coordinate(frame: Frame, s: CoordinateStrategy, v: Vocab) -> list[ElementHandle]:
    live = frame.page.viewport_size
    if live != {"width": s.viewport.width, "height": s.viewport.height}:
        raise Skipped("viewport differs")
    # Coordinates are FRAME-LOCAL (captured from the element's frame box): elementFromPoint
    # works in the frame's own viewport, and the point survives a sibling frame being resized.
    handle = await frame.evaluate_handle("([x, y]) => document.elementFromPoint(x, y)", [s.x, s.y])
    el = handle.as_element()
    return [el] if el is not None else []


Rung = Callable[[Frame, Any, Vocab], Awaitable[list[ElementHandle]]]

RUNGS: dict[str, Rung] = {
    "role_name": _role_name,
    "inferred_label": _inferred_label,
    "placeholder": _placeholder,
    "table_cell": _table_cell,
    "text": _text,
    "attribute": _attribute,
    "nth_of_role": _nth_of_role,
    "css": _css,
    "coordinate": _coordinate,
}


async def run_strategy(frame: Frame, strategy: Any, vocab: Vocab) -> list[ElementHandle]:
    return await RUNGS[strategy.kind](frame, strategy, vocab)


async def apply_scope(handles: list[ElementHandle], scope: Scope) -> list[ElementHandle]:
    if scope.within_row_matching:
        keep = []
        for h in handles:
            # ponytail: closest('tr') only; a div-grid tenant's row_selector is not consulted here.
            in_row = await h.evaluate(
                "(e, t) => { const r = e.closest('tr'); return !!r && r.innerText.includes(t); }",
                scope.within_row_matching,
            )
            if in_row:
                keep.append(h)
        handles = keep
    if scope.nth is not None:
        handles = handles[scope.nth:scope.nth + 1]
    return handles


async def resolve(frame: Frame, td: TargetDescriptor, vocab: Vocab) -> Resolved:
    attempts: list[Attempt] = []
    for i, s in enumerate(td.strategies):
        if td.intent == "read" and s.kind in POSITIONAL_KINDS:
            attempts.append(Attempt(s.kind, 0, "forbidden-for-read"))
            continue
        try:
            handles = await run_strategy(frame, s, vocab)
        except Skipped as e:
            attempts.append(Attempt(s.kind, 0, str(e)))
            continue
        count = len(handles)
        if count > 1 and td.scope:
            handles = await apply_scope(handles, td.scope)
        if len(handles) == 1:
            return Resolved(
                handle=handles[0], won=s.kind, rung_index=i,
                frame_path=mapped_path(td.frame_path, vocab.frame_map), attempts=attempts,
            )
        attempts.append(Attempt(s.kind, count, f"{len(handles)} after scope" if count > 1 and td.scope else ""))
    if any(a.match_count > 1 for a in attempts):
        raise AmbiguousTarget(td, attempts)
    raise TargetNotResolved(td, attempts)
