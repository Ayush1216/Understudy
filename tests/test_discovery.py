"""The discovery loop against a real Chromium and the live target app, with a scripted model
whose turns pick refs from what the page actually shows. No API key anywhere."""

from __future__ import annotations

import itertools
import json
import os
from datetime import datetime, timezone

import httpx
import pytest

from understudy.discover import (
    ConfigurationError,
    DiscoveryOptions,
    DiscoveryRun,
    OpenAICompatClient,
    ScriptedModelClient,
    ToolCall,
    load_env_file,
)
from understudy.discover import client as client_module
from understudy.schema import Capability, Resolution, assert_lints_clean

GOAL = "look up member 100234 and read their name and regular shares balance"
_ids = itertools.count(1)


def call(tool, **args):
    return [ToolCall(id=f"{tool}-{next(_ids)}", name=tool, arguments=args)]


def _by_label(obs, role, label):
    return next(c.ref for c in obs.all_controls() if c.role == role and c.inferred_label == label)


def _by_name(obs, role, name):
    return next(c.ref for c in obs.all_controls() if c.role == role and c.name == name)


def type_into(label, text):
    return lambda obs, _: call("type_text", ref=_by_label(obs, "textbox", label), text=text, why=f"enter the {label}")


def click(name, role="button"):
    return lambda obs, _: call("click", ref=_by_name(obs, role, name), why=f"press {name}")


def assert_text(text):
    return lambda obs, _: call("assert_checkpoint", kind="text_present", text=text, why="a new screen")


def extract_labeled(label, name, type):
    return lambda obs, _: call("extract", ref=_by_label(obs, "cell", label), output_name=name, type=type, description=f"{label} on the record", why=f"read {label}")


def extract_cell(row, column, name, type):
    def turn(obs, _):
        ref = next(c.ref for c in obs.all_controls() if c.table_position and (c.table_position.row_anchor, c.table_position.column) == (row, column))
        return call("extract", ref=ref, output_name=name, type=type, description=f"{column} of {row}", why=f"read the {row} {column}")
    return turn


def help_():
    return lambda obs, _: call("request_human_help", reason="test", what_i_was_trying="the test")


SIGN_ON = [
    type_into("Operator ID", "{{secrets.APP_OPERATOR}}"),
    type_into("Password", "{{secrets.APP_PASSWORD}}"),
    click("Sign On"),
    assert_text("MAIN MENU"),
]
TO_RECORD = [
    *SIGN_ON,
    click("Member Search", role="link"),
    lambda obs, _: call("declare_input", name="member_number", type="string", description="Six-digit member number", sensitivity="pii", example="100234", pattern=r"^\d{6}$"),
    type_into("Member No.", "100234"),
    click("Retrieve"),
    assert_text("MEMBER RECORD"),
]
HAPPY = [
    *TO_RECORD,
    extract_labeled("Name", "member_name", "string"),
    extract_cell("Regular Shares", "Balance", "primary_share_balance", "currency"),
    lambda obs, _: call("declare_outcome", code="MEMBER_NOT_FOUND", description="No member record exists for that number", detect_text="RECORD NOT FOUND"),
    lambda obs, _: call("declare_outcome", code="ALREADY_ON_RECORD", description="would shadow success", detect_text="MEMBER RECORD"),
    lambda obs, _: call("finish", summary="Signs on, searches the member and reads the name and Regular Shares balance.", success_text="SHARES / BALANCES"),
]


async def discover(tmp_path, base, policy_path, turns, *, escalator=None, **opts):
    run = DiscoveryRun(DiscoveryOptions(
        goal=GOAL, entry_url=f"{base}/t/alpha/", secret_names=["APP_OPERATOR", "APP_PASSWORD"], tenant="alpha",
        operator_policy_path=policy_path, evidence_root=tmp_path / "evidence", capabilities_dir=tmp_path / "capabilities",
        model=ScriptedModelClient(turns), **opts,
    ), escalator=escalator)
    await run.prepare()
    try:
        return run, await run.execute()
    finally:
        await run.close()


def tool_results(run):
    return [m["content"] for m in run.messages if m["role"] == "tool"]


def events(result):
    return [json.loads(line) for line in (result.evidence_dir / "run.jsonl").read_text().splitlines()]


# ---- the loop --------------------------------------------------------------------------------


async def test_scripted_happy_path_compiles_a_lint_clean_capability(tmp_path, reset_target, policy_path, operator_env):
    run, result = await discover(tmp_path, reset_target, policy_path, HAPPY)
    assert result.status == "completed", result.reason
    text = result.capability_path.read_text()
    cap = Capability.model_validate_json(text)
    assert_lints_clean(cap, known_values=["Lovelace, Ada", "$2,499.00"])

    assert [s.expects_navigation for s in cap.steps] == [False, False, True, True, False, True, False]
    assert [s.action.value for s in cap.steps[:2]] == ["{{secrets.APP_OPERATOR}}", "{{secrets.APP_PASSWORD}}"]
    assert cap.steps[4].action.value == "{{inputs.member_number}}"
    assert cap.secrets_required == ["APP_OPERATOR", "APP_PASSWORD"] and "teller1" not in text
    assert cap.steps[2].postcondition.text == "MAIN MENU" and cap.steps[5].postcondition.text == "MEMBER RECORD"
    assert cap.steps[3].postcondition.text == "MEMBER INQUIRY / SELECTION"  # synthesized: no assert after that click
    assert cap.steps[0].action.target.strategies[0].label == "Operator ID"
    assert cap.steps[6].action.outputs == ["member_name", "primary_share_balance"]
    assert cap.outputs[1].source.transform == "currency_to_number" and cap.outputs[1].source.target.strategies[0].kind == "table_cell"
    assert [o.code for o in cap.outcomes] == ["MEMBER_NOT_FOUND"]
    assert cap.success_checkpoint.text == "SHARES / BALANCES"
    assert cap.input_by_name("member_number").example == "100234"
    assert cap.policy_declaration.origins == [reset_target] and cap.policy_declaration.path_patterns == ["/t/alpha/**"]
    assert cap.provenance.llm_step_count == result.llm_steps == len(HAPPY) and cap.approval == "draft"
    assert any("member_name = 'Lovelace, Ada'" in r for r in tool_results(run))

    transcript = (result.evidence_dir / "discovery" / "transcript.jsonl").read_text()
    assert len(transcript.splitlines()) == len(HAPPY) + 1
    assert "teller1" not in transcript and "password" not in transcript
    assert (result.evidence_dir / "capability.json").exists()
    assert min(p.name for p in (result.evidence_dir / "screenshots").iterdir()) == "00-entry.png"
    log = events(result)
    assert any(e["type"] == "recorder.overruled" and e["what"] == "outcomes[ALREADY_ON_RECORD]" for e in log)
    assert log[-1]["type"] == "run.end" and log[-1]["status"] == "completed"


async def test_an_unknown_ref_is_an_error_and_nothing_is_traced(tmp_path, reset_target, policy_path, operator_env):
    run, result = await discover(tmp_path, reset_target, policy_path, [call("click", ref="c999", why="a guess"), help_()])
    assert tool_results(run)[0].startswith("ERROR: Unknown ref c999; re-observe")
    assert run.trace == []
    assert result.status == "escalated" and result.reason.startswith("AGENT_REQUESTED")


async def test_extra_tool_calls_in_one_turn_are_refused(tmp_path, reset_target, policy_path, operator_env):
    run, _ = await discover(tmp_path, reset_target, policy_path, [[*call("observe"), *call("observe")], help_()])
    first, second = tool_results(run)[:2]
    assert first.startswith("URL:") and "Not executed" in second


async def test_an_irreversible_click_is_denied_in_discovery_and_not_traced(tmp_path, reset_target, policy_path, operator_env):
    run, result = await discover(tmp_path, reset_target, policy_path, [click("Sign Off", role="link"), help_()])
    assert tool_results(run)[0].startswith("ERROR: Refused by policy")
    assert run.trace == [] and run.obs.url.endswith("/signon")
    decision = next(e for e in events(result) if e["type"] == "policy.decision")
    assert (decision["risk"], decision["decision"]) == ("irreversible", "deny")


async def test_three_actions_without_progress_escalate(tmp_path, reset_target, policy_path, operator_env):
    poke = lambda obs, _: call("click", ref=_by_label(obs, "textbox", "Operator ID"), why="poke")
    run, result = await discover(tmp_path, reset_target, policy_path, [poke, poke, poke])
    assert result.status == "escalated" and result.reason.startswith("NO_PROGRESS")
    third = run.model.requests[2]["messages"]
    assert isinstance(third[-1]["content"], list)  # a screenshot follows a no-progress tick


async def test_request_human_help_escalates_agent_requested(tmp_path, reset_target, policy_path, operator_env):
    _, result = await discover(tmp_path, reset_target, policy_path, [help_()])
    assert result.status == "escalated" and result.reason.startswith("AGENT_REQUESTED: test")
    assert any(e["type"] == "escalation.raised" for e in events(result))


async def test_the_step_cap_escalates_and_the_clock_stops(tmp_path, reset_target, policy_path, operator_env):
    _, result = await discover(tmp_path, reset_target, policy_path, [call("observe")], max_steps=1)
    assert result.status == "escalated" and result.reason.startswith("MAX_STEPS")
    _, result = await discover(tmp_path, reset_target, policy_path, [], timeout_s=0)
    assert result.status == "stopped" and "clock" in result.reason and result.llm_steps == 0


async def test_finish_is_refused_when_the_success_text_is_not_on_screen(tmp_path, reset_target, policy_path, operator_env):
    run, result = await discover(tmp_path, reset_target, policy_path, [call("finish", summary="x", success_text="NOT ON THIS SCREEN"), help_()])
    assert "success_text is not visible" in tool_results(run)[0]
    assert result.status == "escalated"  # the loop continued to the next turn


async def test_finish_is_refused_when_the_success_text_embeds_a_supplied_input(
    tmp_path, reset_target, policy_path, operator_env
):
    """Either the recorder substitutes the literal, giving a checkpoint that depends on how the
    application renders the value, or it cannot and lint L006 rejects the recording after the fact.
    Refusing at the tool is the only ending where the model can still pick a heading instead."""
    # Visible on the entry screen, so it clears the visibility check and reaches the value check.
    finish = call("finish", summary="x", success_text="Demo operators: teller1")
    run, result = await discover(tmp_path, reset_target, policy_path, [finish, help_()],
                                 inputs={"operator_id": "teller1"})
    assert "embeds the supplied value 'teller1'" in tool_results(run)[0]
    assert result.status == "escalated"  # refused at the tool, and the loop went on


async def test_extract_on_a_ref_without_a_semantic_anchor_is_an_error(tmp_path, reset_target, policy_path, operator_env):
    table = lambda obs, _: call("extract", ref=next(t.ref for f in obs.frames for t in f.tables), output_name="x", type="string", description="", why="")
    run, _ = await discover(tmp_path, reset_target, policy_path, [*TO_RECORD, table, help_()])
    assert "no semantic anchor" in tool_results(run)[-2]


class Operator:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.requests = []

    async def raise_intervention(self, request):
        self.requests.append(request)
        return request.model_copy(update={"status": "resolved", "resolution": Resolution(at=datetime.now(timezone.utc), decision=self.decisions.pop(0))})


async def test_a_human_resume_hands_back_a_fresh_observation_and_abort_stops_the_run(tmp_path, reset_target, policy_path, operator_env):
    def after_resume(obs, messages):
        content = messages[-1]["content"]
        assert content[0]["text"].startswith("A human operator intervened") and content[1]["type"] == "image_url"
        return call("request_human_help", reason="again", what_i_was_trying="x")

    operator = Operator(["resume", "abort"])
    _, result = await discover(tmp_path, reset_target, policy_path, [help_(), after_resume], escalator=operator)
    assert result.status == "stopped" and "aborted" in result.reason
    first = operator.requests[0]
    assert first.origin == "discovery" and first.reason == "AGENT_REQUESTED" and first.goal == GOAL
    assert (tmp_path / first.context.screenshot_path).exists() and (tmp_path / first.context.dom_path).exists()


# ---- the client ------------------------------------------------------------------------------

COMPLETION = {
    "id": "x", "object": "chat.completion", "created": 0, "model": "m",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "observe", "arguments": '{"include_screenshot": true'}}],
    }}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


async def test_client_drops_parallel_tool_calls_when_rejected_retries_a_429_and_survives_bad_json():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(400, json={"error": {"message": "parallel_tool_calls is not supported"}})
        if len(seen) == 2:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=COMPLETION)

    client = OpenAICompatClient("http://test/v1", "k", "m", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    turn = await client.complete(system="s", messages=[{"role": "user", "content": "hi"}], tools=[])
    assert [("parallel_tool_calls" in body) for body in seen] == [True, False, False]
    assert turn.tool_calls[0].name == "observe" and turn.tool_calls[0].arguments == {"_parse_error": '{"include_screenshot": true'}
    assert (turn.input_tokens, turn.output_tokens) == (10, 5) and seen[-1]["messages"][0] == {"role": "system", "content": "s"}
    await client.complete(system="s", messages=[], tools=[])
    assert "parallel_tool_calls" not in seen[-1]  # the rejection is remembered


def test_from_env_names_the_fix_when_the_key_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(client_module, "ENV_FILE", tmp_path / ".env")
    with pytest.raises(ConfigurationError, match="aistudio.google.com/apikey"):
        OpenAICompatClient.from_env()


def test_env_file_parser_handles_quotes_and_comments_without_overriding(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEP", "original")
    (tmp_path / ".env").write_text('# comment\nKEEP=changed\nA="quoted value"\nB = bare\n\nnot a pair\n')
    try:
        load_env_file(tmp_path / ".env")
        assert (os.environ["KEEP"], os.environ["A"], os.environ["B"]) == ("original", "quoted value", "bare")
    finally:
        os.environ.pop("A", None), os.environ.pop("B", None)
