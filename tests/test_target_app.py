"""The hostile target app, driven over ASGI (no browser, no port)."""

import re

import httpx
import pytest
from httpx import ASGITransport

from target_app.app import app
from target_app.faults import Session

TOKEN = re.compile(r'name="_token" value="([0-9a-f]+)"')
HIDDEN = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)">')
FORM = re.compile(r'<form method="(\w+)" action="([^"]+)">')
NAMES = {"alpha": ("u", "p"), "beta": ("user", "pass")}


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def token_of(html: str) -> str:
    return TOKEN.search(html).group(1)


def form_of(html: str) -> tuple[str, str, dict[str, str]]:
    method, action = FORM.search(html).groups()
    return method, action, dict(HIDDEN.findall(html))


async def sign_on(c: httpx.AsyncClient, tenant="alpha", user="teller1", password="password") -> httpx.Response:
    u, p = NAMES[tenant]
    r = await c.get(f"/t/{tenant}/signon")
    return await c.post(f"/t/{tenant}/signon", data={u: user, p: password, "_token": token_of(r.text)})


async def open_share_token(c: httpx.AsyncClient, mid="100234") -> str:
    r = await c.get(f"/t/alpha/members/{mid}/open-share")
    assert r.status_code == 200
    return token_of(r.text)


@pytest.fixture(autouse=True)
async def _reset_between_tests():
    async with client() as c:
        await c.post("/_reset")


# ---- sign-on and chrome ---------------------------------------------------------------------


async def test_content_route_redirects_to_signon_when_not_signed_on():
    async with client() as c:
        r = await c.get("/t/alpha/menu")
        assert r.status_code == 302 and r.headers["location"].endswith("/t/alpha/signon")
        r = await c.get("/t/alpha/members/100234")
        assert r.status_code == 302


async def test_signon_succeeds_and_menu_shows_status_bar_and_fkey_footer():
    async with client() as c:
        r = await sign_on(c)
        assert r.status_code == 302 and r.headers["location"].endswith("/t/alpha/menu")
        r = await c.get("/t/alpha/menu")
        assert r.status_code == 200
        assert "MAIN MENU" in r.text and "Member Search" in r.text and "Sign Off" in r.text
        assert re.search(r"OPR TELLER1 \| BR MAIN-001 \| \d\d/\d\d/\d{4} \| SID [0-9a-f]{8}", r.text)
        assert "F3=Sign Off  F5=Main Menu  F7=Member Inquiry  F12=Cancel" in r.text


async def test_wrong_credentials_rerender_signon_with_error():
    async with client() as c:
        r = await sign_on(c, password="nope")
        assert r.status_code == 200
        assert '<font class="err">Invalid operator credentials.</font>' in r.text
        assert "OPERATOR SIGN ON" in r.text
        assert (await c.get("/t/alpha/menu")).status_code == 302


async def test_signoff_ends_the_session():
    async with client() as c:
        await sign_on(c)
        r = await c.get("/t/alpha/signoff")
        assert r.status_code == 302
        assert (await c.get("/t/alpha/menu")).status_code == 302


async def test_alpha_entry_is_a_frameset_with_nav_and_content_frames():
    async with client() as c:
        r = await c.get("/t/alpha/")
        assert "<frameset" in r.text and 'name="nav"' in r.text and 'name="content"' in r.text
        nav = await c.get("/t/alpha/nav")
        assert nav.status_code == 200 and 'target="content"' in nav.text
        for text in ("Main Menu", "Member Search", "Sign Off"):
            assert text in nav.text


async def test_frame_loads_share_the_session_minted_by_the_frameset_document():
    async with client() as c:
        await c.get("/t/alpha/")
        sid = c.cookies["sid"]
        await c.get("/t/alpha/nav")
        r = await c.get("/t/alpha/signon")
        assert c.cookies["sid"] == sid
        u, p = NAMES["alpha"]
        r = await c.post("/t/alpha/signon", data={u: "teller1", p: "password", "_token": token_of(r.text)})
        assert r.status_code == 302


# ---- search and record -----------------------------------------------------------------------


async def test_search_finds_member_and_record_shows_shares_table():
    async with client() as c:
        await sign_on(c)
        r = await c.get("/t/alpha/search")
        assert "MEMBER INQUIRY / SELECTION" in r.text and "Member No.:" in r.text and 'value="Retrieve"' in r.text
        r = await c.get("/t/alpha/search", params={"q": "100234"}, follow_redirects=True)
        assert r.status_code == 200 and "MEMBER RECORD" in r.text
        for label in ("Member No.:", "Name:", "E-mail:", "Phone:", "Address:"):
            assert label in r.text
        assert "Lovelace, Ada" in r.text and "SHARES / BALANCES" in r.text
        for h in ("Share ID", "Type", "Balance", "Status"):
            assert f">{h}</font></th>" in r.text
        assert "100234-S0001" in r.text and "Regular Shares" in r.text and "$2,499.00" in r.text
        assert "ACTIONS" in r.text and "Open New Share" in r.text


async def test_every_member_has_a_regular_shares_row():
    async with client() as c:
        await sign_on(c, user="super1")
        for mid in ("100234", "100987", "101555", "102777", "103001"):
            r = await c.get(f"/t/alpha/members/{mid}")
            assert r.status_code == 200 and "Regular Shares" in r.text


async def test_unknown_member_is_record_not_found_with_http_200():
    async with client() as c:
        await sign_on(c)
        for r in (await c.get("/t/alpha/search", params={"q": "999999"}), await c.get("/t/alpha/members/999999")):
            assert r.status_code == 200
            assert "MEMBER INQUIRY / SELECTION" in r.text
            assert '<font class="err">RECORD NOT FOUND</font>' in r.text
            assert "No member record found for ID 999999." in r.text


async def test_restricted_member_is_forbidden_for_teller_and_visible_to_supervisor():
    async with client() as c:
        await sign_on(c)
        for path in ("/t/alpha/members/103001", "/t/alpha/members/103001/open-share"):
            r = await c.get(path)
            assert r.status_code == 403
            assert "NOT AUTHORIZED" in r.text and "SUPERVISOR OVERRIDE REQUIRED" in r.text
    async with client() as c:
        await sign_on(c, user="super1")
        r = await c.get("/t/alpha/members/103001")
        assert r.status_code == 200 and "Restricted, Example" in r.text


# ---- open share ------------------------------------------------------------------------------


@pytest.mark.parametrize("deposit", ["10", "24.99", "abc", "", "NaN", "Infinity", "-100", "1e30", "9" * 40])
async def test_deposit_under_minimum_or_non_numeric_rerenders_form_with_validation_error(deposit):
    async with client() as c:
        await sign_on(c)
        token = await open_share_token(c)
        r = await c.post("/t/alpha/members/100234/open-share", data={"share_type": "Regular Shares", "f1": deposit, "_token": token})
        assert r.status_code == 200
        assert '<font class="err">Initial deposit must be at least $25.00</font>' in r.text
        assert "REVIEW NEW SHARE" not in r.text


async def test_review_then_confirm_creates_a_share_that_appears_on_the_record():
    async with client() as c:
        await sign_on(c)
        before = (await c.get("/t/alpha/members/100234")).text.count("100234-S")
        token = await open_share_token(c)
        r = await c.post("/t/alpha/members/100234/open-share", data={"share_type": "Money Market", "f1": "100", "_token": token})
        assert r.status_code == 200 and "REVIEW NEW SHARE" in r.text and "$100.00" in r.text
        method, action, hidden = form_of(r.text)
        assert (method, action) == ("POST", "/t/alpha/members/100234/open-share/confirm")
        assert hidden["share_type"] == "Money Market" and hidden["f1"] == "100.00"
        r = await c.post(action, data=hidden)
        assert r.status_code == 200 and "SUB-ACCOUNT CREATED" in r.text
        assert re.search(r"Reference Number:.*SA-100234-\d{4}", r.text)
        new_id = f"100234-S{before + 1:04d}"
        assert new_id in r.text
        r = await c.get("/t/alpha/members/100234")
        assert r.text.count("100234-S") == before + 1
        assert new_id in r.text and "Money Market" in r.text and "$100.00" in r.text


async def test_confirm_revalidates_the_hidden_fields():
    async with client() as c:
        await sign_on(c)
        before = (await c.get("/t/alpha/members/100234")).text.count("100234-S")
        token = await open_share_token(c)
        r = await c.post("/t/alpha/members/100234/open-share/confirm", data={"share_type": "Certificate", "f1": "1", "_token": token})
        assert r.status_code == 200 and "Initial deposit must be at least $25.00" in r.text
        token = await open_share_token(c)
        r = await c.post("/t/alpha/members/100234/open-share/confirm", data={"share_type": "Bitcoin", "f1": "100", "_token": token})
        assert r.status_code == 400
        assert (await c.get("/t/alpha/members/100234")).text.count("100234-S") == before


async def test_post_without_a_valid_token_is_rejected():
    async with client() as c:
        r = await c.get("/t/alpha/signon")
        token = token_of(r.text)
        u, p = NAMES["alpha"]
        creds = {u: "teller1", p: "password"}
        assert (await c.post("/t/alpha/signon", data=creds)).status_code == 400
        assert (await c.post("/t/alpha/signon", data={**creds, "_token": "0" * 16})).status_code == 400
        assert (await c.post("/t/alpha/signon", data={**creds, "_token": token})).status_code == 302
        # tokens are single-use: a double submit does not replay
        assert (await c.post("/t/alpha/signon", data={**creds, "_token": token})).status_code == 400


# ---- fault injection -------------------------------------------------------------------------


async def test_maintenance_interstitial_replays_the_original_get():
    async with client() as c:
        await sign_on(c)
        r = await c.get("/t/alpha/search", params={"q": "100234", "inject": "maintenance"})
        assert r.status_code == 503 and "SYSTEM MAINTENANCE NOTICE" in r.text
        assert r.text.count('type="submit"') == 1 and 'value="Continue"' in r.text
        method, action, hidden = form_of(r.text)
        assert (method, action, hidden) == ("GET", "/t/alpha/search", {"q": "100234"})
        r = await c.request(method, action, params=hidden, follow_redirects=True)
        assert r.status_code == 200 and "MEMBER RECORD" in r.text and "Lovelace, Ada" in r.text


async def test_maintenance_on_a_post_replays_with_the_still_valid_token():
    async with client() as c:
        await sign_on(c)
        token = await open_share_token(c)
        r = await c.post("/t/alpha/members/100234/open-share", params={"inject": "maintenance"},
                         data={"share_type": "Certificate", "f1": "500", "_token": token})
        assert r.status_code == 503
        method, action, hidden = form_of(r.text)
        assert (method, action) == ("POST", "/t/alpha/members/100234/open-share")
        assert hidden == {"share_type": "Certificate", "f1": "500", "_token": token}
        r = await c.post(action, data=hidden)
        assert r.status_code == 200 and "REVIEW NEW SHARE" in r.text and "$500.00" in r.text


async def test_session_expired_returns_440_and_issues_a_fresh_session():
    async with client() as c:
        await sign_on(c)
        old = c.cookies["sid"]
        r = await c.get("/t/alpha/menu", params={"inject": "session_expired"})
        assert r.status_code == 440
        assert '<font class="err">Your session has expired.</font>' in r.text and "OPERATOR SIGN ON" in r.text
        assert c.cookies["sid"] != old
        assert (await c.get("/t/alpha/menu")).status_code == 302
        u, p = NAMES["alpha"]
        r = await c.post("/t/alpha/signon", data={u: "teller1", p: "password", "_token": token_of(r.text)})
        assert r.status_code == 302


async def test_app_error_returns_500_in_normal_chrome():
    async with client() as c:
        await sign_on(c)
        r = await c.get("/t/alpha/members/100234", params={"inject": "app_error"})
        assert r.status_code == 500
        assert "APPLICATION ERROR - REF 0x5A2" in r.text and "F3=Sign Off" in r.text and "OPR TELLER1" in r.text


async def test_not_found_validation_and_permission_can_be_injected():
    async with client() as c:
        await sign_on(c)
        r = await c.get("/t/alpha/members/100234", params={"inject": "not_found"})
        assert r.status_code == 200 and "RECORD NOT FOUND" in r.text and "for ID 100234." in r.text
        r = await c.get("/t/alpha/members/100234", params={"inject": "permission"})
        assert r.status_code == 403 and "SUPERVISOR OVERRIDE REQUIRED" in r.text
        r = await c.get("/t/alpha/members/100234", params={"inject": "validation"})
        assert r.status_code == 200 and "Initial deposit must be at least $25.00" in r.text
        # a mistyped override must not silently produce a green run
        assert (await c.get("/t/alpha/menu", params={"inject": "maintenence"})).status_code == 400
        assert (await c.get("/t/alpha/menu", params={"inject": "error_rate"})).status_code == 400


async def test_injection_is_scoped_to_the_arming_session():
    async with client() as a, client() as b:
        await sign_on(a)
        await sign_on(b)
        r = await a.post("/_inject", json={"mode": "app_error"})
        assert r.status_code == 200 and r.json()["session"] == a.cookies["sid"]
        assert (await b.get("/t/alpha/menu")).status_code == 200
        assert (await a.get("/t/alpha/menu")).status_code == 500
        assert (await a.get("/t/alpha/menu")).status_code == 200  # ttl=1 was consumed


async def test_inject_can_target_another_session_by_id():
    async with client() as browser, client() as cli:
        await sign_on(browser)
        sid = browser.cookies["sid"]
        assert sid in {s["session"] for s in (await cli.get("/_sessions")).json()}
        r = await cli.post("/_inject", json={"mode": "permission", "session": sid})
        assert r.status_code == 200
        r = await browser.get("/t/alpha/menu")
        assert r.status_code == 403 and "SUPERVISOR OVERRIDE REQUIRED" in r.text
        assert (await cli.post("/_inject", json={"mode": "permission", "session": "nope"})).status_code == 404
        assert (await cli.post("/_inject", json={"mode": "bogus"})).status_code == 400


async def test_inject_rejects_non_numeric_ttl_and_out_of_range_rate():
    async with client() as c:
        for body in ({"mode": "app_error", "ttl": "abc"}, {"mode": "error_rate", "rate": "x"},
                     {"mode": "error_rate", "rate": 5}, {"mode": "error_rate"}):
            assert (await c.post("/_inject", json=body)).status_code == 400, body
        assert (await c.post("/_inject", json={"mode": "error_rate", "rate": 0.5})).status_code == 200


async def test_sticky_injection_persists_until_reset():
    async with client() as c:
        await sign_on(c)
        await c.post("/_inject", json={"mode": "not_found", "ttl": -1})
        for _ in range(3):
            assert "RECORD NOT FOUND" in (await c.get("/t/alpha/members/100234")).text
        await c.post("/_reset")
        assert "MEMBER RECORD" in (await c.get("/t/alpha/members/100234")).text


async def test_error_rate_fails_posts_but_not_gets():
    async with client() as c:
        await sign_on(c)
        await c.post("/_inject", json={"mode": "error_rate", "rate": 1.0})
        assert (await c.get("/t/alpha/menu")).status_code == 200
        token = await open_share_token(c)
        data = {"share_type": "Regular Shares", "f1": "50", "_token": token}
        assert (await c.post("/t/alpha/members/100234/open-share", data=data)).status_code == 500
        await c.post("/_reset")
        # the failed POST never reached the token check, so the same form still submits
        assert "REVIEW NEW SHARE" in (await c.post("/t/alpha/members/100234/open-share", data=data)).text


async def test_error_rate_rolls_are_reproducible_for_a_session_id():
    def rolls(sid: str) -> list[str | None]:
        s = Session(sid)
        s.arm("error_rate", rate=0.5)
        return [s.take(is_post=True, override=None) for _ in range(12)]

    assert rolls("fixed") == rolls("fixed")
    assert {"app_error", None} == set(rolls("fixed"))  # mixed outcomes, so the equality means something
    assert rolls("fixed") != rolls("other")


async def test_injection_does_not_touch_signon_or_frame_chrome():
    async with client() as c:
        # ttl=1: chrome must not fire the fault AND must not consume it before the content request
        await c.post("/_inject", json={"mode": "app_error", "ttl": 1})
        assert (await c.get("/t/alpha/")).status_code == 200
        assert (await c.get("/t/alpha/nav")).status_code == 200
        assert (await c.get("/t/alpha/signon")).status_code == 200
        assert (await sign_on(c)).status_code == 302
        assert (await c.get("/t/alpha/menu")).status_code == 500
        assert (await c.get("/t/alpha/menu")).status_code == 200


# ---- tenant beta -----------------------------------------------------------------------------


async def test_beta_is_the_same_flow_in_different_words_without_a_frameset():
    async with client() as c:
        r = await c.get("/t/beta/")
        assert r.status_code == 302 and r.headers["location"].endswith("/t/beta/signon")
        r = await c.get("/t/beta/signon")
        assert "<frameset" not in r.text and "<font" not in r.text
        assert "User ID" in r.text and "Passcode" in r.text and ">Sign In</button>" in r.text
        assert 'name="user"' in r.text and 'name="pass"' in r.text and "v6.1" in r.text
        r = await sign_on(c, tenant="beta")
        assert r.status_code == 302
        r = await c.get("/t/beta/menu")
        assert "HOME" in r.text and "Find Member" in r.text
        r = await c.get("/t/beta/search")
        assert "Account Number" in r.text and 'name="acct"' in r.text and ">Search</button>" in r.text
        r = await c.get("/t/beta/search", params={"acct": "100234"}, follow_redirects=True)
        assert "MEMBER PROFILE" in r.text and "Member Name" in r.text and "Lovelace, Ada" in r.text
        assert "ACCOUNTS / BALANCES" in r.text and "<th>Account ID</th><th>Type</th><th>Balance</th><th>Status</th>" in r.text
        assert "Regular Shares" in r.text and "$2,499.00" in r.text
        r = await c.get("/t/beta/search", params={"acct": "999999"})
        assert r.status_code == 200 and "No matching member" in r.text


async def test_beta_faults_use_beta_vocabulary():
    async with client() as c:
        await sign_on(c, tenant="beta")
        r = await c.get("/t/beta/members/100234", params={"inject": "maintenance"})
        assert r.status_code == 503 and "Scheduled maintenance" in r.text
        assert r.text.count("<button") == 1 and '<button type="submit">Proceed</button>' in r.text
        method, action, hidden = form_of(r.text)
        r = await c.request(method, action, params=hidden)
        assert r.status_code == 200 and "MEMBER PROFILE" in r.text
        r = await c.get("/t/beta/members/103001")
        assert r.status_code == 403 and "Supervisor override required" in r.text
        r = await c.get("/t/beta/members/100234", params={"inject": "app_error"})
        assert r.status_code == 500 and "APPLICATION ERROR - REF 0x5A2" in r.text


async def test_unknown_tenant_is_404():
    async with client() as c:
        assert (await c.get("/t/gamma/")).status_code == 404
        assert (await c.get("/t/gamma/signon")).status_code == 404
