"""The whole thread, end to end, against a real Chromium and the live target app, no API key.

(a) A scripted discovery compiles an artifact that then replays for a member the recording never
    saw: the recorder generalized. (b) The committed demo artifacts produce every README verdict
    in-process through the CLI, including an operator approving the irreversible step over the
    console API while the run is parked on it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import httpx
import pytest

from understudy.cli import main
from understudy.replay import ReplayOptions, replay
from understudy.schema import Capability

from .conftest import _free_port
from .test_discovery import HAPPY, discover
from .test_replay import _share_count

CAPS = Path(__file__).resolve().parents[1] / "capabilities"
BALANCE = "alpha.member.balance.v1.0.0.json"
SHARE = "alpha.share.open.v1.0.0.json"


async def test_a_scripted_discovery_replays_for_a_member_the_recording_never_saw(tmp_path, reset_target, policy_path, operator_env):
    _, discovered = await discover(tmp_path, reset_target, policy_path, HAPPY)
    assert discovered.status == "completed", discovered.reason
    cap = Capability.model_validate_json(discovered.capability_path.read_text())
    assert cap.approval == "draft" and cap.input_by_name("member_number").example == "100234"
    result = await replay(cap, ReplayOptions(inputs={"member_number": "100987"}, allow_draft=True,
                                             operator_policy_path=policy_path, evidence_root=tmp_path / "evidence"))
    assert result.status == "success", result
    assert result.outputs == {"member_name": "Hopper, Grace", "primary_share_balance": 310.42}


@pytest.fixture
def demo(target_server, policy_path, tmp_path, capsys):
    """The committed demo artifacts rebound to the test server. run(name, *args) -> (exit code, result, verdict)."""
    for name in (BALANCE, SHARE):
        (tmp_path / name).write_text((CAPS / name).read_text().replace("http://localhost:4599", target_server))

    def run(name: str, *args: str):
        code = main(["replay", str(tmp_path / name), *args, "--policy", str(policy_path), "--evidence", str(tmp_path / "evidence")])
        out, err = capsys.readouterr()
        return code, json.loads(out), err.rstrip().splitlines()[-1]

    return run


def _rules_applied(tmp_path: Path, result: dict) -> list[str]:
    events = map(json.loads, (tmp_path / result["evidence_dir"] / "run.jsonl").read_text().splitlines())
    return [e["rule_id"] for e in events if e["type"] == "recovery.applied"]


def test_replay_reads_another_member_with_exit_0(reset_target, operator_env, demo):
    code, result, verdict = demo(BALANCE, "--input", "member_number=100987")
    assert (code, result["status"]) == (0, "success"), result
    assert result["outputs"] == {"member_name": "Hopper, Grace", "primary_share_balance": 310.42}
    assert verdict.startswith('success: outputs {"member_name": "Hopper, Grace"')


def test_unknown_member_is_a_business_outcome_with_exit_0(reset_target, operator_env, demo):
    code, result, verdict = demo(BALANCE, "--input", "member_number=999999")
    assert (code, result["status"], result["code"]) == (0, "business_outcome", "MEMBER_NOT_FOUND")
    assert verdict.startswith("business_outcome MEMBER_NOT_FOUND (legitimate answer; exit 0)")


def test_injected_maintenance_is_recovered_and_invisible_in_the_result(reset_target, operator_env, demo, tmp_path):
    code, result, _ = demo(BALANCE, "--input", "member_number=100234", "--inject", "maintenance")
    assert (code, result["status"]) == (0, "success"), result
    assert _rules_applied(tmp_path, result) == ["dismiss_maintenance"]


def test_injected_session_expiry_is_reauthenticated_by_the_curated_recovery(reset_target, operator_env, demo, tmp_path):
    code, result, _ = demo(BALANCE, "--input", "member_number=100234", "--inject", "session_expired")
    assert (code, result["status"]) == (0, "success"), result
    assert result["outputs"]["member_name"] == "Lovelace, Ada"
    assert _rules_applied(tmp_path, result) == ["reauthenticate"]


def test_injected_app_error_is_a_failure_with_exit_1(reset_target, operator_env, demo):
    code, result, verdict = demo(BALANCE, "--input", "member_number=100234", "--inject", "app_error")
    assert (code, result["status"], result["error"]["kind"]) == (1, "failed", "SURFACE_ERROR")
    assert verdict.startswith("failed SURFACE_ERROR at s6: expected the application to return a usable page; observed HTTP 500")


def test_the_same_artifact_replays_on_tenant_beta(reset_target, operator_env, demo):
    code, result, _ = demo(BALANCE, "--input", "member_number=100234", "--tenant", "beta")
    assert (code, result["status"], result["tenant"]) == (0, "success", "beta"), result
    assert result["outputs"] == {"member_name": "Lovelace, Ada", "primary_share_balance": 2499.0}


def test_confirm_parks_on_approval_and_exits_2_when_nobody_answers(reset_target, operator_env, demo):
    before = _share_count(reset_target)
    code, result, verdict = demo(SHARE, "--input", "member_number=100234", "--input", "deposit=50")
    assert (code, result["status"], result["reason"], result["at_step_id"]) == (2, "escalated", "RISKY_ACTION_APPROVAL", "s8")
    assert verdict.startswith("escalated RISKY_ACTION_APPROVAL at s8: intervention iv_")
    assert _share_count(reset_target) == before  # left unperformed


def test_an_operator_approving_over_the_console_api_performs_confirm(reset_target, operator_env, demo, tmp_path):
    before = _share_count(reset_target)
    port = _free_port()
    seen: list[dict] = []

    def operator() -> None:
        api = f"http://127.0.0.1:{port}/api"
        for _ in range(600):  # the console is up once the browser has launched; the run parks at s8
            try:
                iv = next((i for i in httpx.get(f"{api}/run").json()["interventions"] if i["status"] == "open"), None)
            except httpx.HTTPError:
                iv = None
            if iv:
                break
            time.sleep(0.1)
        else:
            return
        seen.append(iv)
        httpx.post(f"{api}/interventions/{iv['id']}/claim")
        httpx.post(f"{api}/interventions/{iv['id']}/resolve", json={"decision": "approve", "note": "deposit reviewed"})

    threading.Thread(target=operator, daemon=True).start()
    code, result, _ = demo(SHARE, "--input", "member_number=100234", "--input", "deposit=50", "--console", "--console-port", str(port))
    assert (code, result["status"]) == (0, "success"), result
    assert seen[0]["reason"] == "RISKY_ACTION_APPROVAL" and seen[0]["proposed_action"]["target"]["label"] == "Confirm"
    assert re.fullmatch(r"SA-100234-\d{4}", result["outputs"]["reference_number"])
    assert _share_count(reset_target) == before + 1
    recorded = json.loads((tmp_path / result["evidence_dir"] / "interventions.json").read_text())
    assert recorded[0]["status"] == "resolved" and recorded[0]["resolution"]["decision"] == "approve"


def test_a_deposit_below_the_minimum_is_a_business_outcome(reset_target, operator_env, demo):
    code, result, _ = demo(SHARE, "--input", "member_number=100234", "--input", "deposit=10")
    assert (code, result["status"], result["code"], result["at_step_id"]) == (0, "business_outcome", "DEPOSIT_TOO_SMALL", "s7")


def test_a_restricted_member_is_a_permission_outcome_not_a_failure(reset_target, operator_env, demo):
    code, result, _ = demo(SHARE, "--input", "member_number=103001", "--input", "deposit=50")
    assert (code, result["status"], result["code"], result["at_step_id"]) == (0, "business_outcome", "PERMISSION_DENIED", "s4")
