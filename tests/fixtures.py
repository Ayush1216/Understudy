"""A hand-written, valid capability against tenant `alpha` of the local target app.

This is ALSO the markup contract for target_app/alpha: every label, button text, column
header and frame name referenced here must exist there exactly.
"""

from __future__ import annotations

import copy
from typing import Any

from understudy.schema import Capability

ALPHA_ENTRY = "http://localhost:4599/t/alpha/"


def _td(role: str, strategies: list[dict[str, Any]], *, frame=("content",), intent="act",
        label="", rationale="", scope=None) -> dict[str, Any]:
    d: dict[str, Any] = {
        "role": role, "label": label, "rationale": rationale,
        "frame_path": list(frame), "intent": intent, "strategies": strategies,
    }
    if scope:
        d["scope"] = scope
    return d


def _label(role: str, label: str, name_attr: str, frame=("content",), intent="act") -> dict[str, Any]:
    return _td(
        role,
        [
            {"kind": "inferred_label", "role": role, "label": label, "confidence": 0.9, "origin": "captured", "matches_at_capture": 1},
            {"kind": "attribute", "attr": "name", "value": name_attr, "role": role, "confidence": 0.7, "origin": "captured", "matches_at_capture": 1},
        ],
        frame=frame, intent=intent, label=label,
        rationale=f"adjacent-cell label {label!r}; no <label for> on this markup; name={name_attr} as fallback",
    )


def _button(text: str, frame=("content",)) -> dict[str, Any]:
    return _td(
        "button",
        [
            {"kind": "role_name", "role": "button", "name": text, "confidence": 0.95, "origin": "captured", "matches_at_capture": 1},
            {"kind": "attribute", "attr": "value", "value": text, "role": "button", "confidence": 0.7, "origin": "captured", "matches_at_capture": 1},
        ],
        frame=frame, label=text, rationale="submit button; accessible name comes from value=",
    )


def _link(text: str, frame=("nav",)) -> dict[str, Any]:
    return _td(
        "link",
        [
            {"kind": "role_name", "role": "link", "name": text, "confidence": 0.95, "origin": "captured", "matches_at_capture": 1},
            {"kind": "text", "text": text, "role": "link", "confidence": 0.8, "origin": "captured", "matches_at_capture": 1},
        ],
        frame=frame, label=text, rationale="navigation link identified by its visible text",
    )


def _cp_text(text: str, wait_ms: int = 8000, frame=None) -> dict[str, Any]:
    d: dict[str, Any] = {"kind": "text_present", "text": text, "match": "contains", "wait_ms": wait_ms}
    if frame is not None:
        d["frame_path"] = list(frame)
    return d


def member_balance_dict() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "id": "cap_alpha_member_balance",
        "name": "alpha.member.balance",
        "version": "1.0.0",
        "title": "Look up a member and read their primary share balance",
        "description": "Signs on as the operator, searches a member by number, opens the member record and reads the Regular Shares balance.",
        "approval": "approved",
        "target": {
            "surface": "legacy_web", "app": "harborline-cu", "app_version": "3.7.2",
            "tenant": "alpha", "entry_url": ALPHA_ENTRY,
            "viewport": {"width": 1280, "height": 800},
        },
        "inputs": [
            {"name": "member_number", "type": "string", "description": "Six-digit member number",
             "pattern": r"^\d{6}$", "sensitivity": "pii", "example": "100234"},
        ],
        "outputs": [
            {"name": "member_name", "type": "string", "description": "Member name as shown on the record",
             "source": {"kind": "text_of", "transform": "trim",
                        "target": _td("cell", [
                            {"kind": "inferred_label", "role": "cell", "label": "Name", "confidence": 0.9, "origin": "captured", "matches_at_capture": 1},
                        ], intent="read", label="Name", rationale="value cell adjacent to the 'Name' label cell")}},
            {"name": "primary_share_balance", "type": "currency", "description": "Balance of the Regular Shares row",
             "source": {"kind": "text_of", "transform": "currency_to_number",
                        "target": _td("cell", [
                            {"kind": "table_cell", "row_anchor": "Regular Shares", "anchor_column": "Type", "column": "Balance", "confidence": 0.85, "origin": "captured", "matches_at_capture": 1},
                        ], intent="read", label="Balance of Regular Shares",
                        rationale="header-matched column, row anchored on share Type; never by row position")}},
        ],
        "secrets_required": ["APP_OPERATOR", "APP_PASSWORD"],
        "steps": [
            {"id": "s1", "description": "Enter the operator ID",
             "action": {"type": "type", "target": _label("textbox", "Operator ID", "u"), "value": "{{secrets.APP_OPERATOR}}"}},
            {"id": "s2", "description": "Enter the password",
             "action": {"type": "type", "target": _label("textbox", "Password", "p"), "value": "{{secrets.APP_PASSWORD}}"}},
            {"id": "s3", "description": "Sign on", "expects_navigation": True,
             "action": {"type": "click", "target": _button("Sign On")},
             "postcondition": _cp_text("MAIN MENU")},
            {"id": "s4", "description": "Open member search", "expects_navigation": True,
             "action": {"type": "click", "target": _link("Member Search")},
             "postcondition": {"kind": "control_present", "wait_ms": 8000, "target": _label("textbox", "Member No.", "q")}},
            {"id": "s5", "description": "Enter the member number",
             "action": {"type": "type", "target": _label("textbox", "Member No.", "q"), "value": "{{inputs.member_number}}"}},
            {"id": "s6", "description": "Retrieve the member record", "expects_navigation": True,
             "action": {"type": "click", "target": _button("Retrieve")},
             "postcondition": _cp_text("MEMBER RECORD"), "retries": {"max": 1, "backoff_ms": 500}},
            {"id": "s7", "description": "Read the name and primary share balance",
             "action": {"type": "extract", "outputs": ["member_name", "primary_share_balance"]}},
        ],
        "outcomes": [
            {"code": "MEMBER_NOT_FOUND", "description": "No member record exists for that number",
             "detect": {"kind": "text_present", "text": "RECORD NOT FOUND", "match": "contains"}},
        ],
        "recoveries": [
            {"id": "dismiss_maintenance", "description": "Dismiss the maintenance interstitial and continue",
             "when": {"kind": "text_present", "text": "SYSTEM MAINTENANCE NOTICE", "match": "contains"},
             "do": [{"type": "click", "target": _button("Continue")}],
             "max_attempts": 3, "then_retry_step": False},
        ],
        "success_checkpoint": _cp_text("SHARES / BALANCES", wait_ms=5000),
        "policy_declaration": {
            "origins": ["http://localhost:4599"], "path_patterns": ["/t/alpha/**"],
            "max_risk": "safe", "require_approval_for": ["irreversible"],
        },
        "provenance": {
            "recorded_at": "2026-09-10T00:00:00Z", "goal": "look up member 100234 and read their savings balance",
            "model": "hand-written", "discovery_run_id": "fixture", "llm_step_count": 0, "curated": True,
        },
        "tenant_overrides": {},
    }


def beta_override_dict() -> dict[str, Any]:
    """Tenant `beta`: the SAME vendor product, reskinned. No frameset, divs instead of tables,
    different labels and button text, different version. Every string below is one a naive
    recording would have baked in. This override is the whole port — no re-recording."""
    return {
        "note": "Cedarvale FCU skin, product v6.1: single page (no frameset), div layout, renamed labels.",
        "entry_url": "http://localhost:4599/t/beta/",
        "frame_map": {"nav": "", "content": ""},
        "aliases": {
            "labels": {"Operator ID": ["User ID"], "Password": ["Passcode"], "Member No.": ["Account Number"], "Name": ["Member Name"]},
            "texts": {
                "Sign On": ["Sign In"], "Member Search": ["Find Member"], "Retrieve": ["Search"],
                "MAIN MENU": ["HOME"], "MEMBER RECORD": ["MEMBER PROFILE"],
                "SHARES / BALANCES": ["ACCOUNTS / BALANCES"], "RECORD NOT FOUND": ["No matching member"],
                "SYSTEM MAINTENANCE NOTICE": ["Scheduled maintenance"], "Continue": ["Proceed"],
            },
            "columns": {"Share ID": ["Account ID"]},
        },
    }


def member_balance(**overrides: Any) -> Capability:
    d = member_balance_dict()
    d.update(copy.deepcopy(overrides))
    return Capability.model_validate(d)


def member_balance_with_beta() -> Capability:
    # The override moves entry_url to /t/beta/ and CapabilityOverride carries no policy, so the
    # base declaration must request every tenant's paths.
    policy = {**member_balance_dict()["policy_declaration"], "path_patterns": ["/t/*/**"]}
    return member_balance(tenant_overrides={"beta": beta_override_dict()}, policy_declaration=policy)
