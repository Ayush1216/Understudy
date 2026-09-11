"""Both tenants' routes, plus the fault-injection control endpoints.

A renderer is a module with one function per screen; the tenant in the path picks the module.
On signed-on content routes the handler order is: session -> injection -> form token ->
business logic. Injection before the token check is deliberate: a maintenance interstitial
replays the original POST, and its `_token` must still be valid when it arrives.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal, InvalidOperation
from types import ModuleType
from urllib.parse import parse_qsl, urlencode

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.base import RequestResponseEndpoint

from . import data, faults, render_alpha, render_beta
from .data import MEMBERS, MIN_DEPOSIT, OPERATORS, SHARE_TYPES, Member
from .faults import SESSIONS, Session

app = FastAPI(title="Understudy target app", docs_url=None, redoc_url=None, openapi_url=None)

RENDER: dict[str, ModuleType] = {"alpha": render_alpha, "beta": render_beta}
COOKIE = "sid"


@app.middleware("http")
async def session_cookie(request: Request, call_next: RequestResponseEndpoint) -> Response:
    sid = request.cookies.get(COOKIE)
    request.state.session = SESSIONS.get(sid) or faults.new_session()
    response = await call_next(request)
    s: Session = request.state.session  # session_expired swaps in a fresh one mid-request
    if s.id != sid:
        response.set_cookie(COOKIE, s.id, httponly=True)
    return response


# ---- helpers --------------------------------------------------------------------------------


def page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=302)  # never 307: a POST must not be replayed


def renderer(tenant: str) -> ModuleType:
    if tenant not in RENDER:
        raise HTTPException(404, f"unknown tenant {tenant!r}")
    return RENDER[tenant]


async def form_of(request: Request) -> dict[str, str]:
    # python-multipart is not installed; every form here is urlencoded and stdlib parses that.
    return dict(parse_qsl((await request.body()).decode(), keep_blank_values=True))


def check_token(s: Session, form: dict[str, str]) -> None:
    if not s.take_token(form.get("_token")):
        raise HTTPException(400, "missing, unknown or already-used form token")


def parse_deposit(raw: str) -> Decimal | None:
    # NaN compares by raising and Infinity passes >=, so finiteness is checked first; quantize
    # raises too when the coefficient exceeds the context precision (e.g. "1e30").
    try:
        d = Decimal(raw.strip().lstrip("$").replace(",", ""))
        if not d.is_finite() or d < MIN_DEPOSIT:
            return None
        return d.quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def validate_share(form: dict[str, str]) -> tuple[str, Decimal | None]:
    share_type = form.get("share_type", "")
    if share_type not in SHARE_TYPES:
        raise HTTPException(400, f"share_type must be one of {SHARE_TYPES}")
    return share_type, parse_deposit(form.get("f1", ""))


async def guard(request: Request, form: dict[str, str] | None = None) -> Response | None:
    """For signed-on content routes: 302 to sign-on if signed off, else the session's armed
    fault as a response, else None (proceed normally)."""
    s: Session = request.state.session
    tenant = request.path_params["tenant"]
    R = renderer(tenant)
    if not s.operator:
        return redirect(f"/t/{tenant}/signon")
    override = request.query_params.get("inject")
    if override is not None and override not in faults.PER_REQUEST_MODES:
        raise HTTPException(400, f"unknown inject mode {override!r}; one of {sorted(faults.PER_REQUEST_MODES)}")
    mode = s.take(is_post=request.method == "POST", override=override)
    if mode is None:
        return None
    if mode == "slow":
        await asyncio.sleep(4)
        return None
    # A route with no member in scope (e.g. /menu) still renders a well-formed page.
    mid = request.path_params.get("mid") or request.query_params.get(R.NAMES["member"]) or "000000"
    match mode:
        case "not_found":
            return page(R.search(s, not_found=mid))
        case "validation":
            return page(R.open_share(s, mid, s.new_token(), error=True))
        case "permission":
            return page(R.permission(s), 403)
        case "app_error":
            return page(R.app_error(s), 500)
        case "session_expired":
            SESSIONS.pop(s.id, None)
            request.state.session = fresh = faults.new_session()
            return page(R.signon(fresh, fresh.new_token(), error="Your session has expired."), 440)
        case "maintenance":
            # The interstitial replays the original request. `inject` is dropped or the
            # replay would just hit the interstitial again.
            query = {k: v for k, v in request.query_params.items() if k != "inject"}
            if form is None:
                return page(R.maintenance(s, "GET", request.url.path, query), 503)
            action = request.url.path + (f"?{urlencode(query)}" if query else "")
            return page(R.maintenance(s, "POST", action, form), 503)
    raise HTTPException(500, f"unhandled fault mode {mode!r}")


def member_or_response(request: Request, mid: str) -> Member | Response:
    """Every member-scoped route goes through here, so a restricted member is refused on the
    record, the open-share form, review and confirm alike."""
    s: Session = request.state.session
    R = renderer(request.path_params["tenant"])
    m = MEMBERS.get(mid)
    if m is None:
        return page(R.search(s, not_found=mid))
    if m.restricted and s.role != "SUPERVISOR":
        return page(R.permission(s), 403)
    return m


# ---- tenant routes --------------------------------------------------------------------------


@app.get("/t/{tenant}/")
async def entry(tenant: str, request: Request) -> Response:
    R = renderer(tenant)
    if tenant == "alpha":
        return page(R.frameset())
    return redirect(f"/t/{tenant}/{'menu' if request.state.session.operator else 'signon'}")


@app.get("/t/{tenant}/nav")
async def nav(tenant: str) -> Response:
    R = renderer(tenant)
    return page(R.nav()) if tenant == "alpha" else redirect(f"/t/{tenant}/")


@app.get("/t/{tenant}/signon")
async def signon_form(tenant: str, request: Request) -> Response:
    s: Session = request.state.session
    if s.operator:
        return redirect(f"/t/{tenant}/menu")
    return page(renderer(tenant).signon(s, s.new_token()))


@app.post("/t/{tenant}/signon")
async def signon_submit(tenant: str, request: Request) -> Response:
    s: Session = request.state.session
    R = renderer(tenant)
    form = await form_of(request)
    check_token(s, form)
    user = form.get(R.NAMES["user"], "").strip().lower()
    if user in OPERATORS and OPERATORS[user][0] == form.get(R.NAMES["pass"], ""):
        s.operator, s.role = user, OPERATORS[user][1]
        return redirect(f"/t/{tenant}/menu")
    return page(R.signon(s, s.new_token(), error="Invalid operator credentials."))


@app.get("/t/{tenant}/signoff")
async def signoff(tenant: str, request: Request) -> Response:
    s: Session = request.state.session
    s.operator, s.role = None, ""
    s.tokens.clear()
    return redirect(f"/t/{tenant}/signon")


@app.get("/t/{tenant}/menu")
async def menu(tenant: str, request: Request) -> Response:
    if r := await guard(request):
        return r
    return page(renderer(tenant).menu(request.state.session))


@app.get("/t/{tenant}/search")
async def search(tenant: str, request: Request) -> Response:
    if r := await guard(request):
        return r
    s: Session = request.state.session
    R = renderer(tenant)
    q = request.query_params.get(R.NAMES["member"], "").strip()
    if not q:
        return page(R.search(s))
    if q not in MEMBERS:
        return page(R.search(s, not_found=q))
    return redirect(f"/t/{tenant}/members/{q}")


@app.get("/t/{tenant}/members/{mid}")
async def record(tenant: str, mid: str, request: Request) -> Response:
    if r := await guard(request):
        return r
    m = member_or_response(request, mid)
    if isinstance(m, Response):
        return m
    return page(renderer(tenant).record(request.state.session, m))


@app.get("/t/{tenant}/members/{mid}/open-share")
async def open_share_form(tenant: str, mid: str, request: Request) -> Response:
    if r := await guard(request):
        return r
    m = member_or_response(request, mid)
    if isinstance(m, Response):
        return m
    s: Session = request.state.session
    return page(renderer(tenant).open_share(s, m.number, s.new_token()))


@app.post("/t/{tenant}/members/{mid}/open-share")
async def open_share_review(tenant: str, mid: str, request: Request) -> Response:
    form = await form_of(request)
    if r := await guard(request, form):
        return r
    m = member_or_response(request, mid)
    if isinstance(m, Response):
        return m
    s: Session = request.state.session
    R = renderer(tenant)
    check_token(s, form)
    share_type, deposit = validate_share(form)
    if deposit is None:
        return page(R.open_share(s, m.number, s.new_token(), error=True))
    return page(R.review(s, m, share_type, deposit, s.new_token()))


@app.post("/t/{tenant}/members/{mid}/open-share/confirm")
async def open_share_confirm(tenant: str, mid: str, request: Request) -> Response:
    form = await form_of(request)
    if r := await guard(request, form):
        return r
    m = member_or_response(request, mid)
    if isinstance(m, Response):
        return m
    s: Session = request.state.session
    R = renderer(tenant)
    check_token(s, form)
    # The review page's hidden fields are client-controlled: the irreversible step re-validates.
    share_type, deposit = validate_share(form)
    if deposit is None:
        return page(R.open_share(s, m.number, s.new_token(), error=True))
    share, ref = data.open_share(m, share_type, deposit)
    return page(R.confirmed(s, m, share, ref))


# ---- fault-injection control ----------------------------------------------------------------


@app.post("/_inject")
async def inject(request: Request) -> dict[str, object]:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object body required")
    mode = body.get("mode")
    try:
        ttl, rate = int(body.get("ttl", 1)), float(body.get("rate", 0.0))
    except (TypeError, ValueError):
        raise HTTPException(400, "ttl and rate must be numbers")
    if mode not in faults.MODES:
        raise HTTPException(400, f"unknown mode {mode!r}; one of {faults.MODES}")
    if ttl == 0 or ttl < -1:
        raise HTTPException(400, "ttl must be -1 (sticky) or >= 1")
    if mode == "error_rate" and not 0 < rate <= 1:
        raise HTTPException(400, "error_rate needs 0 < rate <= 1")
    target: Session = request.state.session
    if sid := body.get("session"):
        if sid not in SESSIONS:
            raise HTTPException(404, f"no session {sid!r}; see GET /_sessions")
        target = SESSIONS[sid]
    target.arm(mode, ttl, rate)
    return {"session": target.id, "mode": target.mode, "ttl": target.ttl, "rate": target.rate}


@app.post("/_reset")
async def reset() -> dict[str, bool]:
    faults.reset()
    data.reset()
    return {"ok": True}


@app.get("/_sessions")
async def sessions() -> list[dict[str, object]]:
    return [{"session": s.id, "operator": s.operator, "mode": s.mode, "ttl": s.ttl} for s in SESSIONS.values()]


PANEL = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Understudy target app</title>
<style>body{{font:14px system-ui;margin:24px;max-width:680px}} button{{margin:2px}} pre{{background:#eee;padding:8px}}</style></head>
<body>
<h1>Target app control panel</h1>
<p>Tenants: <a href="/t/alpha/">alpha — HARBORLINE CU Member Services Console v4.2.1</a> ·
<a href="/t/beta/">beta — CEDARVALE FCU Account Servicing v5.0</a></p>
<p>Arm a fault for <select id="sid"><option value="">this browser's session</option></select>
ttl <input id="ttl" value="1" size="3"> (-1 = sticky) rate <input id="rate" value="0.3" size="3"> (error_rate only)</p>
<p id="modes"></p>
<p><button onclick="reset()">Reset all injection, counters and data</button> <button onclick="refresh()">Refresh sessions</button></p>
<pre id="out">Faults apply only to signed-on content routes of the chosen session. Any tenant URL also accepts ?inject=&lt;mode&gt; for that one request.</pre>
<script>
const MODES = {json.dumps(faults.MODES)};
const out = m => document.getElementById('out').textContent = JSON.stringify(m, null, 1);
async function inject(mode) {{
  const body = {{mode, ttl: +ttl.value, rate: +rate.value}};
  if (sid.value) body.session = sid.value;
  out(await (await fetch('/_inject', {{method: 'POST', headers: {{'content-type': 'application/json'}}, body: JSON.stringify(body)}})).json());
}}
async function reset() {{ out(await (await fetch('/_reset', {{method: 'POST'}})).json()); refresh(); }}
async function refresh() {{
  sid.length = 1;
  for (const s of await (await fetch('/_sessions')).json())
    sid.add(new Option(`${{s.session.slice(0, 8)}} ${{s.operator || '(signed off)'}} ${{s.mode || ''}}`, s.session));
}}
modes.innerHTML = MODES.map(m => `<button onclick="inject('${{m}}')">${{m}}</button>`).join(' ');
refresh();
</script>
</body></html>"""


@app.get("/")
async def panel() -> HTMLResponse:
    return HTMLResponse(PANEL)
