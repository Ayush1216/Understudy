"""Deterministic in-memory member data shared by both tenants. Fake people, fake numbers."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from decimal import Decimal

SHARE_TYPES = ("Regular Shares", "Money Market", "Certificate")
MIN_DEPOSIT = Decimal("25.00")

# operator id -> (password, role)
OPERATORS: dict[str, tuple[str, str]] = {
    "teller1": ("password", "TELLER"),
    "super1": ("password", "SUPERVISOR"),
}


@dataclass
class Share:
    id: str
    type: str
    balance: Decimal
    status: str = "OPEN"


@dataclass
class Member:
    number: str
    name: str
    email: str
    phone: str
    address: str
    restricted: bool = False
    shares: list[Share] = field(default_factory=list)


def _seed() -> dict[str, Member]:
    def m(number: str, name: str, email: str, phone: str, address: str,
          shares: list[tuple[str, str]], restricted: bool = False) -> Member:
        return Member(number, name, email, phone, address, restricted, [
            Share(f"{number}-S{i:04d}", t, Decimal(b)) for i, (t, b) in enumerate(shares, 1)
        ])

    members = [
        m("100234", "Lovelace, Ada", "ada.lovelace@example.net", "555-0134",
          "12 Analytical Way, Bath, NY 14810",
          [("Regular Shares", "2499.00"), ("Money Market", "15250.75")]),
        m("100987", "Hopper, Grace", "grace.hopper@example.net", "555-0187",
          "9 Compiler Court, Arlington, VA 22201",
          [("Regular Shares", "310.42"), ("Certificate", "10000.00"), ("Money Market", "4020.10")]),
        m("101555", "Turing, Alan", "alan.turing@example.net", "555-0155",
          "1 Bletchley Lane, Milton, MA 02186",
          [("Regular Shares", "88.00")]),
        m("102777", "Hamilton, Margaret", "margaret.hamilton@example.net", "555-0177",
          "77 Apollo Drive, Paoli, IN 47454",
          [("Regular Shares", "5400.00"), ("Certificate", "25000.00"),
           ("Money Market", "1200.00"), ("Certificate", "5000.00")]),
        m("103001", "Restricted, Example", "restricted@example.net", "555-0101",
          "1 Locked Box Rd, Nowhere, KS 66000",
          [("Regular Shares", "1.00"), ("Money Market", "999999.99")], restricted=True),
    ]
    return {x.number: x for x in members}


MEMBERS: dict[str, Member] = _seed()
_refs = itertools.count(1)


def open_share(member: Member, share_type: str, deposit: Decimal) -> tuple[Share, str]:
    """Mutate the member and return (new share, reference number). The irreversible step."""
    share = Share(f"{member.number}-S{len(member.shares) + 1:04d}", share_type, deposit)
    member.shares.append(share)
    return share, f"SA-{member.number}-{next(_refs):04d}"


def reset() -> None:
    global _refs
    MEMBERS.clear()
    MEMBERS.update(_seed())
    _refs = itertools.count(1)


def money(d: Decimal) -> str:
    return f"${d:,.2f}"
