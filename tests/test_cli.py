"""The command line, in-process: main([...]) against the live target app, real Chromium, no model."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from understudy.catalog import write_capability
from understudy.cli import main

from .conftest import _free_port, alpha_capability

MEMBER = ["--input", "member_number=100234"]
KEY = "alpha.member.balance@1.0.0"


@pytest.fixture
def artifact(target_server: str, tmp_path: Path) -> Path:
    return write_capability(alpha_capability(target_server), tmp_path / "caps")


def _common(policy_path: Path, tmp_path: Path) -> list[str]:
    return ["--policy", str(policy_path), "--evidence", str(tmp_path / "evidence")]


def test_replay_success_prints_result_json_and_exits_0(reset_target, operator_env, policy_path, tmp_path, artifact, capsys):
    code = main(["replay", str(artifact), *MEMBER, *_common(policy_path, tmp_path)])
    out, err = capsys.readouterr()
    result = json.loads(out)
    assert code == 0 and result["status"] == "success"
    assert result["outputs"] == {"member_name": "Lovelace, Ada", "primary_share_balance": 2499.0}
    assert err.startswith("success: outputs")
    assert (tmp_path / "evidence" / result["evidence_dir"].removeprefix("evidence/") / "result.json").exists()


def test_unknown_member_is_a_business_outcome_with_exit_0(reset_target, operator_env, policy_path, tmp_path, artifact, capsys):
    code = main(["replay", str(artifact), "--input", "member_number=999999", *_common(policy_path, tmp_path)])
    out, err = capsys.readouterr()
    assert code == 0 and json.loads(out)["status"] == "business_outcome"
    assert err.startswith("business_outcome MEMBER_NOT_FOUND (legitimate answer; exit 0)")


def test_injected_app_error_is_a_failure_with_exit_1(reset_target, operator_env, policy_path, tmp_path, artifact, capsys):
    code = main(["replay", str(artifact), *MEMBER, "--inject", "app_error@s6", *_common(policy_path, tmp_path)])
    out, err = capsys.readouterr()
    result = json.loads(out)
    assert code == 1 and result["status"] == "failed" and result["error"]["kind"] == "SURFACE_ERROR"
    assert "armed fault 'app_error' on the browser's session before step s6" in err
    assert "failed SURFACE_ERROR at s6: expected the application to return a usable page; observed HTTP 500" in err


def test_escalating_step_with_no_console_exits_2(reset_target, operator_env, policy_path, tmp_path, capsys):
    cap = alpha_capability(reset_target)
    cap.steps[5].on_failure = "escalate"
    path = write_capability(cap, tmp_path / "escalating")
    code = main(["replay", str(path), *MEMBER, "--inject", "app_error", *_common(policy_path, tmp_path)])
    out, err = capsys.readouterr()
    result = json.loads(out)
    assert code == 2 and result["status"] == "escalated" and result["at_step_id"] == "s6"
    assert "before step s6" in err  # the default step is the last navigating one
    assert err.rstrip().splitlines()[-1].startswith("escalated UNRECOVERABLE at s6")


def test_console_mounts_on_the_run_and_stops_with_it(reset_target, operator_env, policy_path, tmp_path, artifact, capsys):
    port = _free_port()
    code = main(["replay", str(artifact), *MEMBER, "--console", "--console-port", str(port), *_common(policy_path, tmp_path)])
    out, err = capsys.readouterr()
    assert code == 0 and json.loads(out)["status"] == "success"
    assert f"operator console: http://127.0.0.1:{port}" in err


def test_console_teardown_drops_a_viewer_still_attached(reset_target, operator_env, policy_path, tmp_path, artifact):
    """An operator page's event stream never ends on its own; the run must not wait for it."""
    port = _free_port()
    attached, ended = threading.Event(), threading.Event()

    def viewer() -> None:
        for _ in range(100):  # the console is up once the browser has launched; s6 is slowed to ~4s
            try:
                with httpx.stream("GET", f"http://127.0.0.1:{port}/api/events", timeout=None) as r:
                    attached.set()
                    for _ in r.iter_lines():
                        pass
                break
            except httpx.ConnectError:
                time.sleep(0.1)
            except httpx.HTTPError:  # the server dropped us: the point
                break
        ended.set()

    threading.Thread(target=viewer, daemon=True).start()
    code = main(["replay", str(artifact), *MEMBER, "--inject", "slow", "--console", "--console-port", str(port), *_common(policy_path, tmp_path)])
    assert code == 0 and attached.is_set() and ended.wait(5)


def test_invoke_runs_an_approved_capability_from_the_catalog(reset_target, operator_env, policy_path, tmp_path, artifact, capsys):
    code = main(["invoke", KEY, *MEMBER, "--capabilities", str(artifact.parent), *_common(policy_path, tmp_path)])
    out, _ = capsys.readouterr()
    assert code == 0 and json.loads(out)["status"] == "success"


def test_invoke_refuses_a_draft_before_any_run_exists(target_server, policy_path, tmp_path, capsys):
    caps = tmp_path / "caps"
    write_capability(alpha_capability(target_server, approval="draft"), caps)
    args = [*MEMBER, "--capabilities", str(caps), *_common(policy_path, tmp_path)]
    code = main(["invoke", "alpha.member.balance", *args])
    out, err = capsys.readouterr()
    assert code == 1 and out == ""
    assert "approval is 'draft'" in err and f"catalog approve {KEY}" in err
    assert not (tmp_path / "evidence").exists()
    assert main(["invoke", "nope", *args]) == 1
    assert "no capability named 'nope'" in capsys.readouterr().err


def test_catalog_list_tools_describe_and_approve(target_server, tmp_path, capsys):
    caps = tmp_path / "caps"
    write_capability(alpha_capability(target_server), caps)
    draft = write_capability(alpha_capability(target_server, version="1.1.0", approval="draft"), caps)
    dirs = ["--capabilities", str(caps), "--evidence", str(tmp_path / "evidence")]

    assert main(["catalog", "list", *dirs]) == 0
    out = capsys.readouterr().out
    assert KEY in out and "alpha.member.balance@1.1.0" in out and "draft" in out

    assert main(["catalog", "tools", *dirs]) == 0
    tools = json.loads(capsys.readouterr().out)
    assert [t["function"]["name"] for t in tools] == ["alpha__member__balance__v_1_0_0"]  # a draft is not a tool

    assert main(["catalog", "approve", "alpha.member.balance@1.1.0", *dirs]) == 0
    assert json.loads(draft.read_text())["approval"] == "approved"

    assert main(["catalog", "describe", "alpha.member.balance", *dirs]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == "1.1.0"  # latest semver


def test_malformed_input_is_a_usage_error_with_exit_2(capsys):
    assert main(["replay", "x.json", "--input", "member_number"]) == 2
    assert "expected NAME=VALUE" in capsys.readouterr().err


def test_the_same_input_twice_with_two_values_is_refused_rather_than_last_wins(artifact, policy_path, tmp_path, capsys):
    """Silently taking the last one would run the capability against something nobody asked for."""
    common = _common(policy_path, tmp_path)
    assert main(["replay", str(artifact), "--input", "member_number=100987", "--input", "member_number=100234", *common]) == 2
    assert "given twice" in capsys.readouterr().err
    # A repeat that does not conflict is not a typo: it falls through to ordinary validation.
    assert main(["replay", str(artifact), "--input", "member_number=abc", "--input", "member_number=abc", *common]) == 1
    assert "INPUT_INVALID" in capsys.readouterr().err


def test_an_unknown_or_inexpressible_inject_mode_is_a_usage_error(capsys, artifact):
    assert main(["replay", str(artifact), *MEMBER, "--inject", "not_a_mode"]) == 2
    assert "unknown mode 'not_a_mode'" in capsys.readouterr().err
    assert main(["replay", str(artifact), *MEMBER, "--inject", "error_rate"]) == 2
    assert "sticky and probabilistic" in capsys.readouterr().err


def test_a_console_port_already_taken_names_the_fix_instead_of_spinning(
    reset_target, operator_env, policy_path, tmp_path, artifact, target_server, capsys
):
    """uvicorn logs a bind failure and retries rather than raising; without the preflight the
    run waits on `server.started` forever."""
    port = int(target_server.rsplit(":", 1)[1])  # the target app already holds this one
    assert main(["replay", str(artifact), *MEMBER, "--console", "--console-port", str(port),
                 *_common(policy_path, tmp_path)]) == 2
    assert "cannot bind" in capsys.readouterr().err
