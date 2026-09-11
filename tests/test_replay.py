"""Replay against a real Chromium and the live target app. No model anywhere."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from understudy.evidence import read_stability
from understudy.replay import ReplayOptions, ReplayRun
from understudy.schema import Capability, RecoveryRule, Resolution, exit_code

from .conftest import alpha_capability, write_policy
from .fixtures import _button, _cp_text, _label, _link, _td

MEMBER = {"member_number": "100234"}


def _opts(tmp_path: Path, policy_path: Path, **kw: Any) -> ReplayOptions:
    return ReplayOptions(inputs=kw.pop("inputs", dict(MEMBER)), operator_policy_path=policy_path,
                         evidence_root=tmp_path / "evidence", **kw)


async def _run(cap, opts, escalator=None, *, arm: tuple[str, str] | None = None, base: str = "") -> tuple[ReplayRun, Any]:
    run = ReplayRun(cap, opts)
    if arm:
        opts.before_step[arm[0]] = _arm(run, base, arm[1])
    await run.prepare()
    run.escalator = escalator
    if escalator is not None:
        escalator.run = run
    try:
        return run, await run.execute()
    finally:
        await run.close()


def _events(run: ReplayRun, type: str | None = None) -> list[dict[str, Any]]:
    lines = run.evidence.log_path.read_text().splitlines()
    events = [json.loads(line) for line in lines]
    return [e for e in events if type is None or e["type"] == type]


def _arm(run: ReplayRun, base: str, mode: str):
    """A before_step hook that arms a fault on the BROWSER's session (cookies are shared)."""
    async def hook() -> None:
        r = await run.session.context.request.post(f"{base}/_inject", data={"mode": mode, "ttl": 1})
        assert r.ok
    return hook


# ---- happy path and business outcomes -------------------------------------------------------


async def test_happy_path_returns_typed_outputs_and_writes_evidence(reset_target, operator_env, policy_path, tmp_path):
    cap = alpha_capability(reset_target)
    run, result = await _run(cap, _opts(tmp_path, policy_path))
    assert result.status == "success", result
    assert result.outputs == {"member_name": "Lovelace, Ada", "primary_share_balance": 2499.0}
    assert result.steps_executed == 7 and exit_code(result) == 0
    resolved = _events(run, "target.resolved")
    assert [e["step_id"] for e in resolved] == ["s1", "s2", "s3", "s4", "s5", "s6"]
    assert all(e["rung_index"] == 0 for e in resolved)
    assert (run.evidence.run_dir / "result.json").exists()
    assert (run.evidence.run_dir / "screenshots" / "00-entry.png").exists()
    assert read_stability(tmp_path / "evidence", cap.key, None)["runs"] == 1


async def test_unknown_member_is_a_business_outcome_not_a_failure(reset_target, operator_env, policy_path, tmp_path):
    run, result = await _run(alpha_capability(reset_target), _opts(tmp_path, policy_path, inputs={"member_number": "999999"}))
    assert result.status == "business_outcome", result
    assert result.code == "MEMBER_NOT_FOUND" and result.at_step_id == "s6" and exit_code(result) == 0
    assert _events(run, "outcome.matched")[0]["code"] == "MEMBER_NOT_FOUND"


# ---- injected faults: recovered, absorbed, hard ---------------------------------------------


async def test_maintenance_interstitial_is_dismissed_and_the_run_succeeds(reset_target, operator_env, policy_path, tmp_path):
    run, result = await _run(alpha_capability(reset_target), _opts(tmp_path, policy_path), arm=("s6", "maintenance"), base=reset_target)
    assert result.status == "success", result
    assert result.outputs["member_name"] == "Lovelace, Ada"
    assert [e["rule_id"] for e in _events(run, "recovery.applied")] == ["dismiss_maintenance"]


async def test_session_expiry_is_reauthenticated_and_the_step_retried(reset_target, operator_env, policy_path, tmp_path):
    cap = alpha_capability(reset_target)
    cap.recoveries.append(RecoveryRule.model_validate({
        "id": "reauthenticate", "description": "Sign on again and return to the member search",
        "when": {"kind": "text_present", "text": "Your session has expired."},
        "do": [
            {"type": "type", "target": _label("textbox", "Operator ID", "u"), "value": "{{secrets.APP_OPERATOR}}"},
            {"type": "type", "target": _label("textbox", "Password", "p"), "value": "{{secrets.APP_PASSWORD}}"},
            {"type": "click", "target": _button("Sign On")},
            {"type": "click", "target": _link("Member Search")},
            {"type": "type", "target": _label("textbox", "Member No.", "q"), "value": "{{inputs.member_number}}"},
        ],
        "then_retry_step": True,
    }))
    run, result = await _run(cap, _opts(tmp_path, policy_path), arm=("s6", "session_expired"), base=reset_target)
    assert result.status == "success", result
    assert result.outputs["primary_share_balance"] == 2499.0
    assert [e["rule_id"] for e in _events(run, "recovery.applied")] == ["reauthenticate"]
    retrieves = [e for e in _events(run, "action.performed") if e["step_id"] == "s6" and e.get("action", {}).get("target", {}).get("label") == "Retrieve"]
    assert len(retrieves) == 2  # the step's own action was re-run after the repair


async def test_app_error_is_a_surface_error_with_status_and_evidence(reset_target, operator_env, policy_path, tmp_path):
    run, result = await _run(alpha_capability(reset_target), _opts(tmp_path, policy_path), arm=("s6", "app_error"), base=reset_target)
    assert result.status == "failed", result
    assert result.error.kind == "SURFACE_ERROR" and result.error.step_id == "s6" and exit_code(result) == 1
    assert "HTTP 500" in result.error.observed and "APPLICATION ERROR" in result.error.observed
    assert result.error.expected == "the application to return a usable page"
    assert list((run.evidence.run_dir / "screenshots").glob("*s6-failure.png"))
    assert list((run.evidence.run_dir / "dom").glob("*s6-failure.html"))


async def test_slow_response_is_absorbed_by_the_postcondition_wait(reset_target, operator_env, policy_path, tmp_path):
    _, result = await _run(alpha_capability(reset_target), _opts(tmp_path, policy_path), arm=("s6", "slow"), base=reset_target)
    assert result.status == "success", result


async def test_a_wait_step_reports_a_late_outcome_not_a_checkpoint_failure(reset_target, operator_env, policy_path, tmp_path):
    # The click was recorded without a navigation wait; a `wait` step carries it, and the outcome page renders late.
    d = _unwaited_click(reset_target)
    d["steps"].insert(6, {"id": "s6b", "description": "Wait for the record", "action": {"type": "wait", "checkpoint": _cp_text("MEMBER RECORD")}})
    run, result = await _run(Capability.model_validate(d), _opts(tmp_path, policy_path, inputs={"member_number": "999999"}), arm=("s6", "slow"), base=reset_target)
    assert result.status == "business_outcome" and result.code == "MEMBER_NOT_FOUND" and result.at_step_id == "s6b", result


async def test_the_success_checkpoint_reports_a_late_outcome_not_a_checkpoint_failure(reset_target, operator_env, policy_path, tmp_path):
    d = _unwaited_click(reset_target)
    d["steps"], d["outputs"] = d["steps"][:6], []
    _, result = await _run(Capability.model_validate(d), _opts(tmp_path, policy_path, inputs={"member_number": "999999"}), arm=("s6", "slow"), base=reset_target)
    assert result.status == "business_outcome" and result.code == "MEMBER_NOT_FOUND" and result.at_step_id is None, result


# ---- refused before a browser opens ---------------------------------------------------------


async def test_bad_input_is_rejected_before_any_browser_opens(reset_target, operator_env, policy_path, tmp_path):
    run, result = await _run(alpha_capability(reset_target), _opts(tmp_path, policy_path, inputs={"member_number": "12345", "extra": 1}))
    assert result.status == "failed" and result.error.kind == "INPUT_INVALID"
    assert run.session is None
    assert "member_number" in result.error.observed and "unknown input 'extra'" in result.error.observed
    assert (run.evidence.run_dir / "result.json").exists()


async def test_missing_secret_is_named_never_valued(reset_target, operator_env, policy_path, tmp_path, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD")
    run, result = await _run(alpha_capability(reset_target), _opts(tmp_path, policy_path))
    assert result.status == "failed" and result.error.kind == "INPUT_INVALID" and run.session is None
    assert "APP_PASSWORD" in result.error.message and "APP_OPERATOR" not in result.error.message
    assert "password" not in result.model_dump_json()


async def test_ungranted_origin_is_policy_blocked_at_entry(reset_target, operator_env, tmp_path):
    policy = write_policy(tmp_path, "http://localhost:1")
    _, result = await _run(alpha_capability(reset_target), _opts(tmp_path, policy))
    assert result.status == "failed" and result.error.kind == "POLICY_BLOCKED"
    assert result.error.step_id is None and "origin" in result.error.observed


async def test_draft_runs_only_with_allow_draft(reset_target, operator_env, policy_path, tmp_path):
    cap = alpha_capability(reset_target, approval="draft")
    run, result = await _run(cap, _opts(tmp_path, policy_path))
    assert result.status == "failed" and result.error.kind == "INPUT_INVALID" and run.session is None
    assert "--allow-draft" in result.error.message
    _, result = await _run(cap, _opts(tmp_path, policy_path, allow_draft=True))
    assert result.status == "success"


async def test_three_runs_are_identical(reset_target, operator_env, policy_path, tmp_path):
    seen = set()
    for _ in range(3):
        run, result = await _run(alpha_capability(reset_target), _opts(tmp_path, policy_path))
        assert result.status == "success"
        seen.add((json.dumps(result.outputs, sort_keys=True), tuple((e["step_id"], e["won"]) for e in _events(run, "target.resolved"))))
    assert len(seen) == 1


# ---- escalation and the three-way resume ----------------------------------------------------


async def test_escalating_step_with_nobody_there_returns_escalated(reset_target, operator_env, policy_path, tmp_path):
    run, result = await _run(_escalating(reset_target), _opts(tmp_path, policy_path), arm=("s6", "app_error"), base=reset_target)
    assert result.status == "escalated" and result.reason == "UNRECOVERABLE" and result.at_step_id == "s6"
    assert exit_code(result) == 2
    raised = _events(run, "escalation.raised")[0]
    assert raised["intervention_id"] == result.intervention_id and "HTTP 500" in raised["observed"]


async def test_human_abort_fails_with_the_original_error(reset_target, operator_env, policy_path, tmp_path):
    stub = Stub(lambda run, req: _decide("abort"))
    run, result = await _run(_escalating(reset_target), _opts(tmp_path, policy_path), stub, arm=("s6", "app_error"), base=reset_target)
    assert result.status == "failed" and result.error.kind == "SURFACE_ERROR"
    assert _events(run, "human.resolved")[0]["decision"] == "abort"
    assert stub.requests[0].reason == "UNRECOVERABLE"


async def test_human_completing_the_step_advances_without_rerun(reset_target, operator_env, policy_path, tmp_path):
    stub = Stub(lambda run, req: _human_retrieves(run, reset_target))
    run, result = await _run(_escalating(reset_target), _opts(tmp_path, policy_path), stub, arm=("s6", "app_error"), base=reset_target)
    assert result.status == "success", result
    assert result.outputs["member_name"] == "Lovelace, Ada"
    assert _events(run, "human.resolved")[0]["resume_branch"] == "postcondition_satisfied"
    assert [e["step_id"] for e in _events(run, "target.resolved")].count("s6") == 1  # not re-run


async def test_human_doing_nothing_reruns_then_reescalates_then_fails(reset_target, operator_env, policy_path, tmp_path):
    stub = Stub(lambda run, req: _decide("resume"))
    run, result = await _run(_escalating(reset_target), _opts(tmp_path, policy_path), stub, arm=("s6", "app_error"), base=reset_target)
    assert result.status == "failed" and result.error.kind == "CHECKPOINT_FAILED"
    assert "remained unsatisfied after human intervention" in result.error.message
    assert [e["resume_branch"] for e in _events(run, "human.resolved")] == ["preconditions_rerun", "preconditions_rerun"]
    assert len(_events(run, "escalation.raised")) == 2 and len(stub.requests) == 2


async def test_human_fixing_the_page_without_finishing_the_step_gets_one_clean_rerun(reset_target, operator_env, policy_path, tmp_path):
    async def human(run, req):  # brings the search form back, does not click Retrieve
        frame = run.session.page.frame(name="content")
        await frame.goto(f"{reset_target}/t/alpha/search")
        await frame.fill("input[name=q]", MEMBER["member_number"])
        return "resume"
    stub = Stub(human)
    run, result = await _run(_escalating(reset_target), _opts(tmp_path, policy_path), stub, arm=("s6", "app_error"), base=reset_target)
    assert result.status == "success", result  # the fault was armed once; the re-run did not trip it again
    assert [e["resume_branch"] for e in _events(run, "human.resolved")] == ["preconditions_rerun"] and len(stub.requests) == 1


async def test_resume_reruns_an_extract_step_after_the_human_put_the_record_on_screen(reset_target, operator_env, policy_path, tmp_path):
    d = alpha_capability(reset_target).model_dump(mode="json")
    d["steps"][5]["postcondition"], d["steps"][6]["on_failure"] = None, "escalate"
    stub = Stub(lambda run, req: _human_retrieves(run, reset_target))
    run, result = await _run(Capability.model_validate(d), _opts(tmp_path, policy_path), stub, arm=("s6", "permission"), base=reset_target)
    assert result.status == "success" and result.outputs["member_name"] == "Lovelace, Ada", result
    assert [e["resume_branch"] for e in _events(run, "human.resolved")] == ["preconditions_rerun"]


async def test_an_approved_irreversible_action_is_never_redispatched_by_a_recovery(reset_target, operator_env, policy_path, tmp_path):
    cap = _open_share_capability(reset_target)
    cap.recoveries.append(RecoveryRule.model_validate({
        "id": "reauth_and_return", "description": "Sign on again and rebuild the review page",
        "when": {"kind": "text_present", "text": "Your session has expired."},
        "do": [
            {"type": "type", "target": _label("textbox", "Operator ID", "u", frame=()), "value": "{{secrets.APP_OPERATOR}}"},
            {"type": "type", "target": _label("textbox", "Password", "p", frame=()), "value": "{{secrets.APP_PASSWORD}}"},
            {"type": "click", "target": _button("Sign On", frame=())},
            {"type": "navigate", "url": f"{reset_target}/t/alpha/members/{{{{inputs.member_number}}}}/open-share"},
            {"type": "select", "target": cap.steps[4].action.target.model_dump(mode="json"), "value": "Regular Shares"},
            {"type": "type", "target": _label("textbox", "Initial Deposit", "f1", frame=()), "value": "50"},
            {"type": "click", "target": _button("Review", frame=())},
        ],
        "then_retry_step": True,
    }))
    before = _share_count(reset_target)
    stub = Stub(lambda run, req: _decide("approve"))
    run, result = await _run(cap, _opts(tmp_path, policy_path), stub, arm=("s8", "session_expired"), base=reset_target)
    assert result.status == "failed" and result.error.kind == "POLICY_BLOCKED" and result.error.step_id == "s8", result
    assert "one approval, one dispatch" in result.error.message and result.error.recovery_attempts == ["reauth_and_return"]
    confirms = [e for e in _events(run, "action.performed") if e.get("action", {}).get("target", {}).get("label") == "Confirm"]
    assert len(confirms) == 1 and len(stub.requests) == 1 and _share_count(reset_target) == before


async def test_approve_performs_the_irreversible_action(reset_target, operator_env, policy_path, tmp_path):
    before = _share_count(reset_target)
    stub = Stub(lambda run, req: _decide("approve"))
    run, result = await _run(_open_share_capability(reset_target), _opts(tmp_path, policy_path), stub)
    assert result.status == "success", result
    assert stub.requests[0].reason == "RISKY_ACTION_APPROVAL" and stub.requests[0].proposed_action.type == "click"
    assert _share_count(reset_target) == before + 1


async def test_abort_leaves_the_irreversible_action_unperformed(reset_target, operator_env, policy_path, tmp_path):
    before = _share_count(reset_target)
    stub = Stub(lambda run, req: _decide("abort"))
    run, result = await _run(_open_share_capability(reset_target), _opts(tmp_path, policy_path), stub)
    assert result.status == "failed" and result.error.kind == "POLICY_BLOCKED" and result.error.step_id == "s8"
    assert "not approved" in result.error.message
    assert _share_count(reset_target) == before


async def test_human_wait_is_excluded_from_the_wall_clock(reset_target, operator_env, tmp_path):
    policy = write_policy(tmp_path, reset_target)
    policy.write_text(policy.read_text().replace("replay_timeout_ms = 120000", "replay_timeout_ms = 3000"))
    stub = Stub(lambda run, req: _human_retrieves(run, reset_target, sleep_s=4))
    run, result = await _run(_escalating(reset_target), _opts(tmp_path, policy), stub, arm=("s6", "app_error"), base=reset_target)
    assert result.status == "success", result
    end = _events(run, "run.end")[0]
    assert end["human_wait_ms"] >= 4000 and end["duration_ms"] >= 4000


# ---- tenants and secrets --------------------------------------------------------------------


async def test_beta_tenant_replays_the_same_artifact_via_aliases(reset_target, operator_env, policy_path, tmp_path):
    run, result = await _run(alpha_capability(reset_target, with_beta=True), _opts(tmp_path, policy_path, tenant="beta"))
    assert result.status == "success", result
    assert result.outputs == {"member_name": "Lovelace, Ada", "primary_share_balance": 2499.0}
    s5 = next(e for e in _events(run, "target.resolved") if e["step_id"] == "s5")
    assert s5["won"] == "inferred_label" and s5["rung_index"] == 0
    assert read_stability(tmp_path / "evidence", run.cap.key, "beta")["runs"] == 1


async def test_secrets_never_reach_evidence(reset_target, operator_env, policy_path, tmp_path):
    run, result = await _run(alpha_capability(reset_target), _opts(tmp_path, policy_path))
    assert result.status == "success"
    for name in ("run.jsonl", "result.json", "capability.json"):
        assert "password" not in (run.evidence.run_dir / name).read_text(), name
    log = run.evidence.log_path.read_text()
    assert "redacted" in log
    typed = [e for e in _events(run, "action.performed") if e["step_id"] == "s2"][0]
    assert "redacted" in typed["action"]["value"]


# ---- helpers ---------------------------------------------------------------------------------


class Stub:
    """An escalator: `decide(run, request)` is what the human does, returning the decision."""

    def __init__(self, decide):
        self.decide = decide
        self.run: ReplayRun | None = None
        self.requests = []

    async def raise_intervention(self, req):
        self.requests.append(req)
        decision = await self.decide(self.run, req)
        return req.model_copy(update={"status": "resolved", "resolution": Resolution(at=datetime.now(timezone.utc), decision=decision)})


async def _decide(decision: str) -> str:
    return decision


async def _human_retrieves(run: ReplayRun, base: str, sleep_s: float = 0) -> str:
    """The human takes the live page to the member record, then hands back with `resume`."""
    await asyncio.sleep(sleep_s)
    frame = run.session.page.frame(name="content")
    await frame.goto(f"{base}/t/alpha/search")
    await frame.fill("input[name=q]", MEMBER["member_number"])
    await frame.click("input[value=Retrieve]")
    await frame.wait_for_selector("text=MEMBER RECORD")
    return "resume"


def _escalating(base: str):
    cap = alpha_capability(base)
    cap.steps[5].on_failure = "escalate"
    return cap


def _unwaited_click(base: str) -> dict[str, Any]:
    """The Retrieve click recorded with neither a navigation wait nor a postcondition."""
    d = alpha_capability(base).model_dump(mode="json")
    d["steps"][5]["expects_navigation"], d["steps"][5]["postcondition"] = False, None
    return d


def _open_share_capability(base: str):
    """Sign on, open a new share for the member, review, CONFIRM (irreversible)."""
    combo = _td("combobox", [{"kind": "attribute", "attr": "name", "value": "share_type", "role": "combobox", "confidence": 0.7, "origin": "captured"}], frame=())
    return alpha_capability(
        base, id="cap_alpha_open_share", name="alpha.share.open", title="Open a new share", description="Opens a Regular Shares sub-account with a $50 deposit.",
        outputs=[], outcomes=[], recoveries=[],
        steps=[
            {"id": "s1", "description": "Enter the operator ID", "action": {"type": "type", "target": _label("textbox", "Operator ID", "u"), "value": "{{secrets.APP_OPERATOR}}"}},
            {"id": "s2", "description": "Enter the password", "action": {"type": "type", "target": _label("textbox", "Password", "p"), "value": "{{secrets.APP_PASSWORD}}"}},
            {"id": "s3", "description": "Sign on", "expects_navigation": True, "action": {"type": "click", "target": _button("Sign On")}, "postcondition": _cp_text("MAIN MENU")},
            {"id": "s4", "description": "Open the new-share form", "action": {"type": "navigate", "url": f"{base}/t/alpha/members/{{{{inputs.member_number}}}}/open-share"},
             "postcondition": _cp_text("OPEN NEW SHARE")},
            {"id": "s5", "description": "Choose the share type", "action": {"type": "select", "target": combo, "value": "Regular Shares"}},
            {"id": "s6", "description": "Enter the initial deposit", "action": {"type": "type", "target": _label("textbox", "Initial Deposit", "f1", frame=()), "value": "50"}},
            {"id": "s7", "description": "Review the new share", "expects_navigation": True, "risk": "sensitive",
             "action": {"type": "click", "target": _button("Review", frame=())}, "postcondition": _cp_text("REVIEW NEW SHARE")},
            {"id": "s8", "description": "Confirm: creates the sub-account", "expects_navigation": True, "risk": "irreversible",
             "action": {"type": "click", "target": _button("Confirm", frame=())}, "postcondition": _cp_text("SUB-ACCOUNT CREATED")},
        ],
        success_checkpoint=_cp_text("Reference Number", wait_ms=3000),
        policy_declaration={"origins": [base], "path_patterns": ["/t/alpha/**"], "max_risk": "irreversible", "require_approval_for": ["irreversible"]},
    )


def _share_count(base: str, mid: str = "100234") -> int:
    with httpx.Client(base_url=base) as c:
        token = re.search(r'name="_token" value="([0-9a-f]+)"', c.get("/t/alpha/signon").text).group(1)
        c.post("/t/alpha/signon", data={"u": "teller1", "p": "password", "_token": token})
        return c.get(f"/t/alpha/members/{mid}").text.count(f"{mid}-S")
