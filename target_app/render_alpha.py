"""Tenant alpha: HARBORLINE CU "Member Services Console" v3.7.2.

1998 enterprise HTML on purpose: a frameset, layout tables, <td class="lbl"> label cells that
are siblings of the input (never <label for>), a <font> wrapper one level below every visible
string, no ARIA, no test ids, submit buttons named only by value=. tests/fixtures.py is written
against this markup; every visible string referenced there must appear here verbatim.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from html import escape as esc

from .data import SHARE_TYPES, Member, Share, money
from .faults import Session

BASE = "/t/alpha"
NAMES = {"user": "u", "pass": "p", "member": "q"}
DOCTYPE = '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" "http://www.w3.org/TR/html4/loose.dtd">'
HEADERS = ("Share ID", "Type", "Balance", "Status")


def f(text: str, size: int = 2) -> str:
    return f'<font face="Verdana" size="{size}">{text}</font>'


def h1(text: str) -> str:
    return f"<h1>{f(text, 4)}</h1>"


def err(text: str) -> str:
    return f'<font class="err">{text}</font>'


def a(href: str, text: str, target: str | None = None) -> str:
    t = f' target="{target}"' if target else ""
    return f'<a href="{href}"{t}>{f(text)}</a>'


def lbl(text: str) -> str:
    return f'<td class="lbl" align="right" nowrap>{f(text)}</td>'


def row(label: str, value: str) -> str:
    return f"<tr>{lbl(label)}<td>{f(esc(value))}</td></tr>"


def hidden(params: dict[str, str]) -> str:
    return "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">' for k, v in params.items())


def _page(s: Session, title: str, body: str) -> str:
    status = ""
    if s.operator:
        status = (
            '<tr><td bgcolor="#3a2f1b"><font face="Verdana" size="1" color="#f5c542">'
            f"OPR {s.operator.upper()} | BR MAIN-001 | {date.today():%m/%d/%Y} | SID {s.id[:8]}"
            "</font></td></tr>"
        )
    return f"""{DOCTYPE}
<html><head><title>Member Services Console - {esc(title)}</title>
<style>
body {{ margin: 0; }} h1 {{ margin: 6px 0 10px 0; }} .lbl {{ font-weight: bold; }}
.err {{ color: #CC0000; font-weight: bold; }}
</style></head>
<body bgcolor="#d6d0bf" text="#000000" link="#7a3b12" vlink="#7a3b12">
<table width="100%" border="0" cellpadding="4" cellspacing="0">
<tr><td bgcolor="#3a2f1b"><font face="Verdana" size="2" color="#f5c542"><b>HARBORLINE CU - Member Services Console v3.7.2</b></font></td></tr>
<tr><td bgcolor="#fbf8ee" valign="top" height="400">{body}</td></tr>
{status}
<tr><td><font face="Courier New" size="1">F3=Sign Off  F5=Main Menu  F7=Member Inquiry  F12=Cancel</font></td></tr>
</table>
</body></html>"""


def frameset() -> str:
    return f"""{DOCTYPE}
<html><head><title>HARBORLINE CU - Member Services Console v3.7.2</title></head>
<frameset cols="200,*" frameborder="1" border="2">
<frame name="nav" src="{BASE}/nav" scrolling="no" noresize>
<frame name="content" src="{BASE}/signon">
<noframes><body>This console requires a frames-capable browser.</body></noframes>
</frameset></html>"""


def nav() -> str:
    links = "".join(
        f"<tr><td>{a(f'{BASE}/{path}', text, target='content')}</td></tr>"
        for path, text in (("menu", "Main Menu"), ("search", "Member Search"), ("signoff", "Sign Off"))
    )
    return f"""{DOCTYPE}
<html><head><title>Navigation</title></head>
<body bgcolor="#3a2f1b" text="#f5c542" link="#f5c542" vlink="#f5c542">
<table border="0" cellpadding="4"><tr><td>{f('<b>HARBORLINE CU</b>')}</td></tr>{links}</table>
</body></html>"""


def signon(s: Session, token: str, error: str | None = None) -> str:
    body = h1("OPERATOR SIGN ON") + (err(error) + "<br><br>" if error else "") + f"""
<form method="POST" action="{BASE}/signon">{hidden({"_token": token})}
<table border="0" cellpadding="2">
<tr>{lbl("Operator ID:")}<td><input type="text" name="{NAMES['user']}" size="12" maxlength="8"></td></tr>
<tr>{lbl("Password:")}<td><input type="password" name="{NAMES['pass']}" size="12" maxlength="16"></td></tr>
<tr><td></td><td><input type="submit" value="Sign On"></td></tr>
</table></form>
<font face="Verdana" size="1" color="#6b6152">Demo operators: teller1 / password (TELLER) &nbsp; super1 / password (SUPERVISOR)</font>"""
    return _page(s, "Sign On", body)


def menu(s: Session) -> str:
    items = "".join(
        f"<tr><td>{f(f'{i}.')}</td><td>{a(f'{BASE}/{path}', text)}</td></tr>"
        for i, (path, text) in enumerate((("search", "Member Search"), ("signoff", "Sign Off")), 1)
    )
    body = h1("MAIN MENU") + f("Select a function:") + f'<table border="0" cellpadding="3">{items}</table>'
    return _page(s, "Main Menu", body)


def search(s: Session, not_found: str | None = None) -> str:
    nf = ""
    if not_found is not None:
        nf = f"{err('RECORD NOT FOUND')}<br>{f('No member record found for ID ' + esc(not_found) + '.')}<br><br>"
    body = h1("MEMBER INQUIRY / SELECTION") + nf + f"""
<form method="GET" action="{BASE}/search">
<table border="0" cellpadding="2">
<tr>{lbl("Member No.:")}<td><input type="text" name="{NAMES['member']}" size="10" maxlength="6"></td><td><input type="submit" value="Retrieve"></td></tr>
</table></form>
<font face="Verdana" size="1" color="#6b6152">Try member numbers 100234, 100987, 101555, 102777, 103001.</font>"""
    return _page(s, "Member Inquiry", body)


def record(s: Session, m: Member) -> str:
    head = "".join(f"<th>{f(h)}</th>" for h in HEADERS)
    rows = "".join(
        f'<tr><td>{f(sh.id)}</td><td>{f(esc(sh.type))}</td><td align="right">{f(money(sh.balance))}</td><td>{f(sh.status)}</td></tr>'
        for sh in m.shares
    )
    body = (
        h1("MEMBER RECORD")
        + '<table border="0" cellpadding="2">'
        + row("Member No.:", m.number) + row("Name:", m.name) + row("E-mail:", m.email)
        + row("Phone:", m.phone) + row("Address:", m.address)
        + "</table><br>" + f("<b>SHARES / BALANCES</b>")
        + f'<table border="1" cellpadding="3" cellspacing="0" bordercolor="#8a8270"><tr bgcolor="#e6e0cd">{head}</tr>{rows}</table><br>'
        + f("<b>ACTIONS</b>") + "<br>" + a(f"{BASE}/members/{m.number}/open-share", "Open New Share")
    )
    return _page(s, "Member Record", body)


def open_share(s: Session, member_number: str, token: str, error: bool = False) -> str:
    opts = "".join(f"<option>{t}</option>" for t in SHARE_TYPES)
    e = err("Initial deposit must be at least $25.00") + "<br><br>" if error else ""
    body = h1("OPEN NEW SHARE") + e + f"""
<form method="POST" action="{BASE}/members/{esc(member_number)}/open-share">{hidden({"_token": token})}
<table border="0" cellpadding="2">
{row("Member No.:", member_number)}
<tr>{lbl("Share Type:")}<td><select name="share_type">{opts}</select></td></tr>
<tr>{lbl("Initial Deposit:")}<td><input type="text" name="f1" size="12"></td></tr>
<tr><td></td><td><input type="submit" value="Review"></td></tr>
</table></form>"""
    return _page(s, "Open New Share", body)


def review(s: Session, m: Member, share_type: str, deposit: Decimal, token: str) -> str:
    body = (
        h1("REVIEW NEW SHARE")
        + f("Verify the details below, then press Confirm to create the sub-account. This action cannot be undone.")
        + '<br><br><table border="0" cellpadding="2">'
        + row("Member No.:", m.number) + row("Name:", m.name)
        + row("Share Type:", share_type) + row("Initial Deposit:", money(deposit))
        + "</table><br>"
        + f'<form method="POST" action="{BASE}/members/{m.number}/open-share/confirm">'
        + hidden({"_token": token, "share_type": share_type, "f1": str(deposit)})
        + f'<input type="submit" value="Confirm"> {a(f"{BASE}/members/{m.number}", "Cancel")}</form>'
    )
    return _page(s, "Review New Share", body)


def confirmed(s: Session, m: Member, share: Share, ref: str) -> str:
    body = (
        h1("SUB-ACCOUNT CREATED")
        + '<table border="0" cellpadding="2">'
        + row("Reference Number:", ref) + row("Member No.:", m.number) + row("Share ID:", share.id) + row("Type:", share.type)
        + row("Balance:", money(share.balance)) + row("Status:", share.status)
        + "</table><br>" + a(f"{BASE}/members/{m.number}", "Return to Member Record")
    )
    return _page(s, "Sub-Account Created", body)


def permission(s: Session) -> str:
    body = (
        h1("NOT AUTHORIZED") + err("SUPERVISOR OVERRIDE REQUIRED") + "<br><br>"
        + f("This function requires supervisor authority. Sign on as a supervisor or contact the branch manager.")
    )
    return _page(s, "Not Authorized", body)


def maintenance(s: Session, method: str, action: str, params: dict[str, str]) -> str:
    body = (
        h1("SYSTEM MAINTENANCE NOTICE")
        + f("The host is completing a scheduled maintenance cycle. Press Continue to resume your request.")
        + f'<br><br><form method="{method}" action="{esc(action)}">{hidden(params)}<input type="submit" value="Continue"></form>'
    )
    return _page(s, "System Maintenance", body)


def app_error(s: Session) -> str:
    body = (
        h1("APPLICATION ERROR") + err("APPLICATION ERROR - REF 0x5A2") + "<br><br>"
        + f("An unexpected condition was encountered. Contact the help desk and quote the reference above.")
    )
    return _page(s, "Application Error", body)
