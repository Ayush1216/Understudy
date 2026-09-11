"""The web surface against static hostile pages: a frameset, layout tables, no <label for>,
no test ids. A real Chromium, a stdlib HTTP server, no target app and no model."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import pathlib
import re
import threading
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from understudy.schema import Aliases, ClickAction, NavigateAction, TargetDescriptor, TypeAction, Viewport
from understudy.surface import ActContext, AmbiguousTarget, ControlLost, PolicyBlocked, SurfaceError
from understudy.surface.web import BrowserSession, WebSurface, perceive

PAGES = pathlib.Path(__file__).parent / "hostile_pages"


@dataclass
class Decision:
    decision: str
    reason: str = "test gate"


class Gate:
    """Allows everything except the action types it was told to deny, and any URL containing 'evil'."""

    def __init__(self, deny: set[str] = frozenset()):
        self.deny = deny

    def check(self, *, action_type, url, risk, mode, approval_granted=False):
        if action_type in self.deny or (url and "evil" in url):
            return Decision("deny")
        return Decision("allow")


@pytest.fixture(scope="module")
def base_url():
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            # `?status=NNN` serves that status instead of the file: how one frame of a frameset 500s.
            m = re.search(r"[?&]status=(\d{3})", self.path)
            if not m:
                return super().do_GET()
            body = b"<html><body><b>APPLICATION ERROR</b></body></html>"
            self.send_response(int(m.group(1)))
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    handler = functools.partial(Quiet, directory=str(PAGES))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)  # frames load concurrently
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()


@pytest.fixture
async def session():
    s = await BrowserSession.launch(headless=True, viewport=Viewport())
    yield s
    await s.close()


@pytest.fixture
async def surface(session, base_url):
    await session.page.goto(base_url + "index.html")
    return WebSurface(session, policy_gate=Gate())


def _control(obs, name_attr):
    return next(c for c in obs.all_controls() if c.attrs.get("name") == name_attr)


def _balance_cell(obs):
    return next(c for c in obs.all_controls()
                if c.table_position and c.table_position.row_anchor == "Regular Shares" and c.table_position.column == "Balance")


def _td(role, strategies, *, frame=("content",), intent="act"):
    return TargetDescriptor(role=role, frame_path=list(frame), intent=intent, strategies=strategies)


def _label(role, label, name_attr):
    return _td(role, [
        {"kind": "inferred_label", "role": role, "label": label, "confidence": 0.9, "origin": "captured"},
        {"kind": "attribute", "attr": "name", "value": name_attr, "role": role, "confidence": 0.7, "origin": "captured"},
    ])


BALANCE = _td("cell", [
    {"kind": "table_cell", "row_anchor": "Regular Shares", "anchor_column": "Type", "column": "Balance", "confidence": 0.85, "origin": "captured"},
], intent="read")


async def _until_closed(session):
    for _ in range(80):
        if session.page.is_closed():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("session did not close after the denied navigation")


async def _trip_redirect(surface, base_url):
    """Navigate into a meta-refresh to a denied URL and wait for the session to fail closed. The
    refresh fires around the load event, so whether act() itself returns or raises is timing;
    the claim under test is what happens to the session afterwards."""
    with contextlib.suppress(PolicyBlocked):
        await surface.act(NavigateAction(type="navigate", url=base_url + "redirect.html"), ActContext())
    await _until_closed(surface.session)


# ---- perception -----------------------------------------------------------------------------


async def test_unlabeled_input_is_named_from_the_adjacent_cell(session, base_url):
    await session.page.goto(base_url + "index.html")
    obs, registry = await perceive(session, cfg={}, gen=1)
    q = _control(obs, "q")
    assert q.role == "textbox"
    assert q.name == ""  # the AX tree says textbox "" — that is the whole problem
    assert q.inferred_label == "Member No."
    assert q.label_source == "row_cell"
    assert 'label="Member No." (row_cell)' in obs.to_text()
    assert registry[q.ref].frame_path == ("content",)


async def test_web_surface_implements_the_seam(surface):
    from understudy.surface import Surface
    assert isinstance(surface, Surface) and surface.kind == "legacy_web"


async def test_frame_paths_are_semantic_never_positional(surface):
    obs = await surface.observe()
    paths = [f.path for f in obs.frames]
    assert paths == [[], ["nav"], ["content"]]
    assert _control(obs, "q").frame_path == ["content"]
    assert all(not seg.startswith("frame-") for p in paths for seg in p)


async def test_div_layout_input_is_labeled_from_preceding_text_and_geometry(session, base_url):
    await session.page.goto(base_url + "divs.html")
    obs, _ = await perceive(session, cfg={}, gen=1)
    acct = _control(obs, "acct")
    assert (acct.inferred_label, acct.label_source) == ("Account Number", "preceding_text")
    zip_ = _control(obs, "zip")
    assert (zip_.inferred_label, zip_.label_source) == ("Zip", "geometric")


async def test_table_cells_carry_row_anchor_and_column_header(surface):
    obs = await surface.observe()
    cell = _balance_cell(obs)
    assert cell.value == "$2,499.00"
    assert cell.table_position.anchor_column == "Type"
    table = obs.frames[2].tables[0]
    assert table.columns == ["Share ID", "Type", "Balance", "Status"]
    assert table.rows[0]["Balance"] == "$2,499.00"
    assert table.cell_refs[0]["Balance"] == cell.ref


async def test_anchor_column_skips_ids_that_happen_to_contain_letters(session, base_url):
    await session.page.goto(base_url + "grid.html")
    obs, _ = await perceive(session, cfg={}, gen=1)
    shares = next(t for t in obs.frames[0].tables if t.columns[0] == "Share ID")
    assert shares.anchor_column == "Type"  # "SAV-001" is per-record data, not a row identity
    assert shares.rows[0]["Type"] == "Regular Shares"


async def test_cell_refs_never_name_a_ref_that_is_not_a_control(session, base_url):
    await session.page.goto(base_url + "grid.html")
    obs, _ = await perceive(session, cfg={}, gen=1)
    amounts = next(t for t in obs.frames[0].tables if "Amount" in t.columns)
    assert "Amount" not in amounts.cell_refs[0]  # that cell's only content is an input, covered by the input
    assert all(obs.control(r) is not None for t in obs.frames[0].tables for row in t.cell_refs for r in row.values())


async def test_page_box_in_a_nested_frame_matches_playwrights_main_frame_box(session, base_url):
    await session.page.goto(base_url + "nested.html")
    obs, _ = await perceive(session, cfg={}, gen=1)
    acct = _control(obs, "acct")
    assert acct.frame_path == ["outer", "inner"]
    real = await session.page.frame(name="inner").locator("[name=acct]").bounding_box()
    assert (acct.page_box.x, acct.page_box.y) == pytest.approx((real["x"], real["y"]), abs=1)


async def test_password_value_is_never_perceived(surface):
    await surface.act(TypeAction(type="type", target=_label("textbox", "Operator PIN", "pin"), value="hunter2"), ActContext())
    obs = await surface.observe()
    pin = _control(obs, "pin")
    assert pin.value is None and "value" not in pin.attrs
    assert "hunter2" not in obs.to_text()


# ---- capture_descriptor -----------------------------------------------------------------------


async def test_captured_descriptor_for_the_unlabeled_input_leads_with_inferred_label(surface):
    obs = await surface.observe()
    td = await surface.capture_descriptor(_control(obs, "q").ref, obs, intent="act")
    kinds = td.strategy_kinds()
    assert kinds[0] == "inferred_label"
    assert td.strategies[0].label == "Member No." and td.strategies[0].matches_at_capture == 1
    assert "role_name" not in kinds  # AX name is empty; a rung that cannot match never enters the artifact
    attr = next(s for s in td.strategies if s.kind == "attribute")
    assert (attr.attr, attr.value) == ("name", "q")
    assert td.frame_path == ["content"] and td.label == "Member No."
    assert td.rationale == 'adjacent-cell label "Member No."; no <label for> on this markup; name=q as fallback'


async def test_captured_read_descriptor_for_a_balance_cell_is_a_table_cell_without_position(surface):
    obs = await surface.observe()
    td = await surface.capture_descriptor(_balance_cell(obs).ref, obs, intent="read")
    tc = td.strategies[0]
    assert tc.kind == "table_cell"
    assert (tc.row_anchor, tc.anchor_column, tc.column) == ("Regular Shares", "Type", "Balance")
    assert tc.matches_at_capture == 1
    assert not {"nth_of_role", "css", "coordinate"} & set(td.strategy_kinds())


async def test_captured_button_has_role_name_then_text_then_value(surface):
    obs = await surface.observe()
    btn = next(c for c in obs.all_controls() if c.name == "Retrieve")
    td = await surface.capture_descriptor(btn.ref, obs, intent="act")
    assert td.strategy_kinds()[:3] == ["role_name", "text", "attribute"]
    assert all(s.matches_at_capture == 1 for s in td.strategies[:3])


async def test_stale_ref_raises_instead_of_resolving_to_whatever_is_there_now(surface):
    old = await surface.observe()
    ref = _control(old, "q").ref
    await surface.observe()
    with pytest.raises(LookupError, match="re-observe"):
        await surface.capture_descriptor(ref, old, intent="act")


async def test_ref_that_outlived_a_navigation_raises_from_the_page_side_guard(surface):
    obs = await surface.observe()
    ref = next(c for c in obs.all_controls() if c.name == "Retrieve").ref
    await surface.session.page.frame(name="content").goto(surface.session.page.url.replace("index.html", "results.html"))
    with pytest.raises(LookupError, match="stale-generation"):
        await surface.capture_descriptor(ref, obs, intent="act")


# ---- resolve --------------------------------------------------------------------------------


async def test_read_text_finds_the_balance_by_row_anchor_and_column(surface):
    assert await surface.read_text(BALANCE) == "$2,499.00"
    r = await surface.resolve(BALANCE)
    assert r.won == "table_cell" and r.rung_index == 0 and r.frame_path == ["content"]


async def test_read_target_skips_positional_rungs(surface):
    td = _td("cell", [
        {"kind": "css", "selector": "table:nth-of-type(3) tr:nth-of-type(2) td:nth-of-type(3)", "confidence": 0.4, "origin": "derived"},
        BALANCE.strategies[0].model_dump(),
    ], intent="read")
    r = await surface.resolve(td)
    assert r.attempts[0].kind == "css" and r.attempts[0].note == "forbidden-for-read"
    assert r.won == "table_cell" and r.rung_index == 1


async def test_inferred_label_resolves_a_value_cell(surface):
    td = _td("cell", [{"kind": "inferred_label", "role": "cell", "label": "Name", "confidence": 0.9, "origin": "captured"}], intent="read")
    assert await surface.read_text(td) == "Lovelace, Ada"


async def test_tenant_aliases_find_account_number_for_member_no(session, base_url):
    await session.page.goto(base_url + "divs.html")
    surface = WebSurface(session, policy_gate=Gate())
    surface.set_tenant_vocabulary(Aliases(labels={"Member No.": ["Account Number"]}), {"content": ""})
    r = await surface.resolve(_label("textbox", "Member No.", "q"))
    assert r.won == "inferred_label"
    assert await r.handle.get_attribute("name") == "acct"


async def test_coordinate_rung_is_skipped_when_the_viewport_differs(surface):
    td = _td("textbox", [
        {"kind": "coordinate", "x": 10, "y": 10, "viewport": {"width": 800, "height": 600}, "confidence": 0.2, "origin": "derived"},
        {"kind": "attribute", "attr": "name", "value": "q", "confidence": 0.7, "origin": "captured"},
    ])
    r = await surface.resolve(td)
    assert r.attempts[0].note == "viewport differs" and r.won == "attribute"


async def test_captured_coordinate_resolves_at_the_same_viewport(surface):
    obs = await surface.observe()
    td = await surface.capture_descriptor(_control(obs, "q").ref, obs, intent="act")
    coord = next(s for s in td.strategies if s.kind == "coordinate")
    r = await surface.resolve(_td("textbox", [coord.model_dump()]))
    assert await r.handle.get_attribute("name") == "q"


async def test_unresolvable_target_names_every_attempt(surface):
    from understudy.surface import TargetNotResolved
    with pytest.raises(TargetNotResolved) as ei:
        await surface.resolve(_label("textbox", "Nonexistent", "nope"))
    assert [a.kind for a in ei.value.attempts] == ["inferred_label", "attribute"]
    assert "inferred_label=0" in ei.value.observed


# ---- act ------------------------------------------------------------------------------------


async def test_click_with_expected_navigation_lands_the_content_frame_on_results(surface):
    await surface.act(TypeAction(type="type", target=_label("textbox", "Member No.", "q"), value="100234"), ActContext())
    retrieve = _td("button", [{"kind": "role_name", "role": "button", "name": "Retrieve", "confidence": 0.95, "origin": "captured"}])
    result = await surface.act(ClickAction(type="click", target=retrieve), ActContext(expects_navigation=True))
    assert result.navigated and "results.html?q=100234" in result.url_after
    assert "results.html?q=100234" in await surface.url()
    obs = await surface.observe()
    content = next(f for f in obs.frames if f.path == ["content"])
    assert "results.html?q=100234" in content.url
    assert "MEMBER RECORD" in content.text_digest
    assert obs.url == content.url  # the frameset's main URL is meaningless


async def test_nav_link_targeting_another_frame_settles_on_that_frame(surface):
    link = _td("link", [{"kind": "role_name", "role": "link", "name": "Member Record", "confidence": 0.95, "origin": "captured"}], frame=("nav",))
    result = await surface.act(ClickAction(type="click", target=link), ActContext(expects_navigation=True))
    assert result.url_after.endswith("results.html")
    obs = await surface.observe()
    assert next(f for f in obs.frames if f.path == ["content"]).url.endswith("results.html")
    assert next(f for f in obs.frames if f.path == ["nav"]).url.endswith("nav.html")


async def test_act_raises_control_lost_while_a_human_holds_the_session(surface):
    surface.session.control.transfer_to("human", "int-1")
    with pytest.raises(ControlLost, match="int-1"):
        await surface.act(TypeAction(type="type", target=_label("textbox", "Member No.", "q"), value="x"), ActContext())


async def test_denied_action_raises_before_touching_the_page(session, base_url):
    await session.page.goto(base_url + "index.html")
    surface = WebSurface(session, policy_gate=Gate(deny={"type"}))
    with pytest.raises(PolicyBlocked) as ei:
        await surface.act(TypeAction(type="type", target=_label("textbox", "Member No.", "q"), value="100234"), ActContext())
    assert ei.value.decision == "deny" and ei.value.error_class == "POLICY_BLOCKED"
    assert await session.page.frame(name="content").locator("[name=q]").input_value() == ""


async def test_navigate_then_observe_sees_the_frameset_children(session, base_url):
    surface = WebSurface(session, policy_gate=Gate())
    result = await surface.act(NavigateAction(type="navigate", url=base_url + "index.html"), ActContext())
    assert result.navigated and result.document_status == 200
    obs = await surface.observe()
    assert _control(obs, "q").frame_path == ["content"]
    assert (await surface.resolve(_label("textbox", "Member No.", "q"))).won == "inferred_label"


async def test_dispatch_failure_is_a_typed_surface_error(surface):
    cell = _td("cell", [BALANCE.strategies[0].model_dump()])  # a <td> cannot be filled
    with pytest.raises(SurfaceError) as ei:
        await surface.act(TypeAction(type="type", target=cell, value="x"), ActContext(timeout_ms=2000))
    assert ei.value.error_class == "INTERNAL" and ei.value.message.startswith("type failed:")
    assert "not an <input>" in ei.value.observed


async def test_non_surface_actions_are_refused(surface):
    from understudy.schema import ExtractAction
    with pytest.raises(ValueError):
        await surface.act(ExtractAction(type="extract", outputs=["x"]), ActContext())


async def test_redirect_to_a_denied_url_closes_the_session(base_url):
    seen = []
    s = await BrowserSession.launch(headless=True, policy_gate=Gate(), on_violation=lambda url, reason: seen.append(url))
    try:
        await s.page.goto(base_url + "redirect.html")
        for _ in range(40):
            if s.page.is_closed():
                break
            await asyncio.sleep(0.05)
        assert s.violation and s.violation["url"].endswith("evil.html")
        assert seen and seen[0].endswith("evil.html")
        assert s.page.is_closed()
    finally:
        await s.close()


async def test_gate_given_only_to_the_surface_still_fails_the_redirect_closed(session, base_url):
    surface = WebSurface(session, policy_gate=Gate())  # the session was launched with no gate
    await _trip_redirect(surface, base_url)
    assert session.violation and session.violation["url"].endswith("evil.html")


async def test_reads_after_a_denied_redirect_raise_policy_blocked_not_a_blank_observation(session, base_url):
    surface = WebSurface(session, policy_gate=Gate())
    await _trip_redirect(surface, base_url)
    with pytest.raises(PolicyBlocked) as ei:
        await surface.observe()
    assert ei.value.error_class == "POLICY_BLOCKED" and "evil.html" in ei.value.expected
    with pytest.raises(PolicyBlocked):
        await surface.screenshot()
    with pytest.raises(PolicyBlocked):
        await surface.read_text(BALANCE)


async def test_popup_to_a_denied_url_closes_the_session(session, base_url):
    surface = WebSurface(session, policy_gate=Gate())
    await session.page.goto(base_url + "popup.html")
    link = _td("link", [{"kind": "role_name", "role": "link", "name": "Open statement", "confidence": 0.95, "origin": "captured"}], frame=())
    with contextlib.suppress(PolicyBlocked):  # the popup may land before or after act() returns
        await surface.act(ClickAction(type="click", target=link), ActContext())
    await _until_closed(session)
    assert session.violation and session.violation["url"].endswith("evil.html")


# ---- screenshots and status ---------------------------------------------------------------


async def test_screenshot_masks_the_password_field(surface):
    masked = await surface.screenshot(mask_sensitive=True)
    unmasked = await surface.screenshot(mask_sensitive=False)
    assert masked[:8] == b"\x89PNG\r\n\x1a\n" and masked != unmasked
    obs = await surface.observe(include_screenshot=True)
    assert obs.screenshot_b64 and (await surface.observe()).screenshot_b64 is None


async def test_screenshot_masks_registered_sensitive_targets(surface):
    plain = await surface.screenshot(mask_sensitive=True)
    surface.register_sensitive_target(_label("textbox", "Member No.", "q"))
    assert await surface.screenshot(mask_sensitive=True) != plain
    assert await surface.session.page.frame(name="content").locator("[data-us-mask]").count() == 0


async def test_masking_covers_every_match_of_an_ambiguous_sensitive_target(session, base_url):
    await session.page.goto(base_url + "grid.html")
    surface = WebSurface(session, policy_gate=Gate())
    await session.page.fill("[name=s1]", "123-45-6789")
    await session.page.fill("[name=s2]", "987-65-4321")
    ssn = _td("textbox", [{"kind": "inferred_label", "role": "textbox", "label": "SSN", "confidence": 0.9, "origin": "captured"}], frame=())
    with pytest.raises(AmbiguousTarget):
        await surface.resolve(ssn)
    surface.register_sensitive_target(ssn)
    both = await session.page.screenshot(type="png", mask=[session.page.locator("[name=s1], [name=s2]")])
    assert await surface.screenshot(mask_sensitive=True) == both  # not just one of them
    assert await session.page.locator("[data-us-mask]").count() == 0


async def test_last_document_status_reflects_the_last_document_response(surface, base_url):
    assert (await surface.last_document_status()).status == 200
    result = await surface.act(NavigateAction(type="navigate", url=base_url + "missing.html"), ActContext())
    assert result.document_status == 404
    assert (await surface.last_document_status()).status == 404
    assert (await surface.observe()).document_status == 404


async def test_content_frame_5xx_is_not_hidden_by_the_nav_frames_200(session, base_url):
    surface = WebSurface(session, policy_gate=Gate())
    result = await surface.act(NavigateAction(type="navigate", url=base_url + "broken.html"), ActContext())
    assert result.document_status == 500  # the frameset document itself was a 200
    assert (await surface.last_document_status()).status == 500
    assert (await surface.observe()).document_status == 500
    await surface.act(NavigateAction(type="navigate", url=base_url + "index.html"), ActContext())
    assert (await surface.last_document_status()).status == 200  # the broken frame is gone


async def test_dom_snapshot_is_labeled_per_frame(surface):
    snap = await surface.dom_snapshot()
    assert "<!-- frame /nav " in snap and "<!-- frame /content " in snap
    assert 'name="q"' in snap
