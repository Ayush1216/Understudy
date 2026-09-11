"""Tenant beta: CEDARVALE FCU "Account Servicing" v5.0 — the same product, reskinned.

No frameset, div layout for forms, different labels, button text and chrome. It differs from
alpha only where tests/fixtures.py's beta override says it does; everything else is identical,
so that override really is the whole port.
"""

from __future__ import annotations

from decimal import Decimal
from html import escape as esc

from .data import SHARE_TYPES, Member, Share, money
from .faults import Session
from .render_alpha import hidden  # hidden inputs have no skin

BASE = "/t/beta"
NAMES = {"user": "user", "pass": "pass", "member": "acct"}
HEADERS = ("Account ID", "Type", "Balance", "Status")


def err(text: str) -> str:
    return f'<div class="err">{text}</div>'


def field(label: str, control: str) -> str:
    return f'<div class="field"><span class="lbl">{label}</span>{control}</div>'


def row(label: str, value: str) -> str:
    return f'<tr><td class="lbl">{label}</td><td>{esc(value)}</td></tr>'


def _page(s: Session, title: str, body: str) -> str:
    who =f" · signed in as {esc(s.operator)} · session {s.id[:8]}" if s.operator else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Account Servicing - {esc(title)}</title>
<style>
body {{ margin: 0; font: 14px/1.4 Arial, sans-serif; background: #f4f6f5; color: #222; }}
.topbar {{ background: #1b5e20; color: #fff; padding: 10px 16px; display: flex; gap: 24px; align-items: center; }}
.topbar a {{ color: #fff; text-decoration: none; }} .brand {{ font-weight: bold; margin-right: auto; }}
main {{ max-width: 760px; margin: 24px auto; background: #fff; padding: 20px 28px; border: 1px solid #cfd8d3; }}
h1 {{ font-size: 20px; margin: 0 0 12px; }} h2 {{ font-size: 15px; margin: 18px 0 6px; }}
.field {{ margin: 10px 0; }} .field .lbl {{ display: inline-block; width: 160px; color: #444; }}
.err {{ color: #b00020; font-weight: bold; margin: 8px 0; }} .hint {{ color: #888; font-size: 12px; }}
button {{ background: #1b5e20; color: #fff; border: 0; padding: 6px 14px; }}
table.grid {{ border-collapse: collapse; }} table.grid th, table.grid td {{ border: 1px solid #bbb; padding: 4px 10px; }}
table.profile td {{ padding: 3px 12px 3px 0; }} table.profile td.lbl {{ color: #444; }}
footer {{ max-width: 760px; margin: 12px auto; color: #777; font-size: 12px; }}
</style></head>
<body>
<nav class="topbar"><span class="brand">CEDARVALE FCU · Account Servicing</span>
<a href="{BASE}/menu">Home</a><a href="{BASE}/search">Find Member</a><a href="{BASE}/signoff">Sign Off</a></nav>
<main>{body}</main>
<footer>Account Servicing v5.0{who}</footer>
</body></html>"""


def signon(s: Session, token: str, error: str | None = None) -> str:
    body = "<h1>SIGN IN</h1>" + (err(error) if error else "") + f"""
<form method="POST" action="{BASE}/signon">{hidden({"_token": token})}
{field("User ID", f'<input name="{NAMES["user"]}" maxlength="8">')}
{field("Passcode", f'<input type="password" name="{NAMES["pass"]}" maxlength="16">')}
<button type="submit">Sign In</button>
</form>
<p class="hint">Demo users: teller1 / password (TELLER) · super1 / password (SUPERVISOR)</p>"""
    return _page(s, "Sign In", body)


def menu(s: Session) -> str:
    # Functions are reachable from the top bar only. Repeating them here would put two
    # "Find Member" links in one document; alpha keeps its copies in separate frames.
    body = (
        f"<h1>HOME</h1><p>Welcome, {esc(s.operator or '').upper()}. Use <strong>Find Member</strong> in the bar "
        "above to look up an account, or <strong>Sign Off</strong> when you are done.</p>"
    )
    return _page(s, "Home", body)


def search(s: Session, not_found: str | None = None) -> str:
    nf = ""
    if not_found is not None:
        nf = err("No matching member") + f"<p>No member record found for account number {esc(not_found)}.</p>"
    body = "<h1>FIND MEMBER</h1>" + nf + f"""
<form method="GET" action="{BASE}/search">
{field("Account Number", f'<input name="{NAMES["member"]}" maxlength="6">')}
<button type="submit">Search</button>
</form>
<p class="hint">Try account numbers 100234, 100987, 101555, 102777, 103001.</p>"""
    return _page(s, "Find Member", body)


def record(s: Session, m: Member) -> str:
    head = "".join(f"<th>{h}</th>" for h in HEADERS)
    rows = "".join(
        f'<tr><td>{sh.id}</td><td>{esc(sh.type)}</td><td style="text-align:right">{money(sh.balance)}</td><td>{sh.status}</td></tr>'
        for sh in m.shares
    )
    body = (
        "<h1>MEMBER PROFILE</h1>"
        + '<table class="profile">'
        + row("Account Number", m.number) + row("Member Name", m.name) + row("E-mail", m.email)
        + row("Phone", m.phone) + row("Address", m.address)
        + "</table><h2>ACCOUNTS / BALANCES</h2>"
        + f'<table class="grid"><tr>{head}</tr>{rows}</table>'
        + f'<h2>ACTIONS</h2><a href="{BASE}/members/{m.number}/open-share">Open New Share</a>'
    )
    return _page(s, "Member Profile", body)


def open_share(s: Session, member_number: str, token: str, error: bool = False) -> str:
    opts = "".join(f"<option>{t}</option>" for t in SHARE_TYPES)
    e = err("Initial deposit must be at least $25.00") if error else ""
    body = "<h1>OPEN NEW SHARE</h1>" + e + f"""
<form method="POST" action="{BASE}/members/{esc(member_number)}/open-share">{hidden({"_token": token})}
{field("Account Number", esc(member_number))}
{field("Share Type", f'<select name="share_type">{opts}</select>')}
{field("Initial Deposit", '<input name="f1">')}
<button type="submit">Review</button>
</form>"""
    return _page(s, "Open New Share", body)


def review(s: Session, m: Member, share_type: str, deposit: Decimal, token: str) -> str:
    body = (
        "<h1>REVIEW NEW SHARE</h1>"
        "<p>Verify the details below, then press Confirm to create the sub-account. This action cannot be undone.</p>"
        '<table class="profile">'
        + row("Account Number", m.number) + row("Member Name", m.name)
        + row("Share Type", share_type) + row("Initial Deposit", money(deposit))
        + "</table>"
        + f'<form method="POST" action="{BASE}/members/{m.number}/open-share/confirm">'
        + hidden({"_token": token, "share_type": share_type, "f1": str(deposit)})
        + f'<button type="submit">Confirm</button> <a href="{BASE}/members/{m.number}">Cancel</a></form>'
    )
    return _page(s, "Review New Share", body)


def confirmed(s: Session, m: Member, share: Share, ref: str) -> str:
    body = (
        f"<h1>SUB-ACCOUNT CREATED</h1><p><strong>Reference Number: {ref}</strong></p>"
        '<table class="profile">'
        + row("Account Number", m.number) + row("Account ID", share.id) + row("Type", share.type)
        + row("Balance", money(share.balance)) + row("Status", share.status)
        + f'</table><p><a href="{BASE}/members/{m.number}">Return to Member Profile</a></p>'
    )
    return _page(s, "Sub-Account Created", body)


def permission(s: Session) -> str:
    body = (
        "<h1>NOT AUTHORIZED</h1>" + err("Supervisor override required")
        + "<p>This function requires supervisor authority. Sign in as a supervisor or contact the branch manager.</p>"
    )
    return _page(s, "Not Authorized", body)


def maintenance(s: Session, method: str, action: str, params: dict[str, str]) -> str:
    body = (
        "<h1>Scheduled maintenance</h1>"
        "<p>The core system is completing a scheduled maintenance cycle. Press Proceed to resume your request.</p>"
        + f'<form method="{method}" action="{esc(action)}">{hidden(params)}<button type="submit">Proceed</button></form>'
    )
    return _page(s, "Scheduled Maintenance", body)


def app_error(s: Session) -> str:
    body = (
        "<h1>APPLICATION ERROR</h1>" + err("APPLICATION ERROR - REF 0x5A2")
        + "<p>An unexpected condition was encountered. Contact the help desk and quote the reference above.</p>"
    )
    return _page(s, "Application Error", body)
