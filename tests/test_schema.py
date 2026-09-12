import json

import pytest
from pydantic import ValidationError

from understudy.schema import (
    Capability,
    Observation,
    OutputSpec,
    ParamSpec,
    TargetDescriptor,
    exit_code,
    FailedResult,
    RunError,
    CapabilityRef,
    SuccessResult,
    BusinessOutcomeResult,
    EscalatedResult,
)

from .fixtures import member_balance, member_balance_dict


def test_fixture_capability_round_trips_through_json():
    cap = member_balance()
    text = cap.model_dump_json()
    again = Capability.model_validate_json(text)
    assert again == cap
    assert again.key == "alpha.member.balance@1.0.0"


def test_unknown_key_is_rejected_at_load():
    d = member_balance_dict()
    d["steps"][0]["timeoutMs"] = 5  # camelCase typo of a real field
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        Capability.model_validate(d)


def test_version_must_be_semver():
    with pytest.raises(ValidationError, match="MAJOR.MINOR.PATCH"):
        member_balance(version="v1")


def test_positional_frame_path_is_rejected():
    with pytest.raises(ValidationError, match="positional frame path"):
        TargetDescriptor(role="button", frame_path=["frame-1"], strategies=[
            {"kind": "role_name", "role": "button", "name": "Go", "confidence": 0.9, "origin": "captured"},
        ])


def test_read_target_with_only_positional_strategies_is_rejected():
    with pytest.raises(ValidationError, match="only positional strategies"):
        TargetDescriptor(role="cell", intent="read", strategies=[
            {"kind": "css", "selector": "tr:nth-child(2) td:nth-child(3)", "confidence": 0.4, "origin": "derived"},
        ])


def test_act_target_may_be_positional():
    td = TargetDescriptor(role="button", intent="act", strategies=[
        {"kind": "nth_of_role", "role": "button", "index": 0, "confidence": 0.5, "origin": "derived"},
    ])
    assert td.strategy_kinds() == ["nth_of_role"]


def test_output_target_must_be_read_intent():
    with pytest.raises(ValidationError, match="intent='read'"):
        OutputSpec(name="x", type="string", description="", source={
            "kind": "text_of",
            "target": {"role": "cell", "intent": "act", "strategies": [
                {"kind": "role_name", "role": "cell", "name": "n", "confidence": 0.9, "origin": "captured"}]},
        })


def test_enum_param_requires_values():
    with pytest.raises(ValidationError, match="requires enum_values"):
        ParamSpec(name="branch", type="enum", description="")


def test_duplicate_outcome_codes_rejected():
    d = member_balance_dict()
    d["outcomes"].append(dict(d["outcomes"][0]))
    with pytest.raises(ValidationError, match="duplicate outcome codes"):
        Capability.model_validate(d)


def test_checkpoint_union_is_recursive():
    cap = member_balance(success_checkpoint={
        "kind": "all", "wait_ms": 3000, "of": [
            {"kind": "text_present", "text": "SHARES / BALANCES"},
            {"kind": "not", "of": {"kind": "text_present", "text": "RECORD NOT FOUND"}},
            {"kind": "any", "of": [{"kind": "url_matches", "pattern": "/members/"}, {"kind": "http_status", "min": 200, "max": 299}]},
        ],
    })
    assert cap.success_checkpoint.kind == "all"
    assert cap.success_checkpoint.of[1].of.text == "RECORD NOT FOUND"


def test_inputs_produce_a_tool_schema():
    schema = ParamSpec.model_json_schema()
    assert schema["additionalProperties"] is False
    assert "sensitivity" in schema["properties"]
    # and the capability itself has a JSON schema a reviewer can read
    full = Capability.model_json_schema()
    assert "steps" in full["properties"]
    json.dumps(full)


def test_result_exit_codes():
    ref = CapabilityRef(id="c", name="n", version="1.0.0")
    common = dict(run_id="r", capability=ref, evidence_dir="e")
    assert exit_code(SuccessResult(status="success", outputs={}, steps_executed=1, **common)) == 0
    assert exit_code(BusinessOutcomeResult(status="business_outcome", code="X", message="m", **common)) == 0
    assert exit_code(FailedResult(status="failed", error=RunError(kind="TIMEOUT", expected="a", observed="b", message="m"), **common)) == 1
    assert exit_code(EscalatedResult(status="escalated", intervention_id="i", reason="UNRECOVERABLE", **common)) == 2


def test_observation_renders_label_for_unnamed_control():
    obs = Observation(gen=1, url="http://x/", title="T", frames=[{
        "path": ["content"], "url": "http://x/c", "controls": [{
            "ref": "c1", "frame_path": ["content"], "role": "textbox", "name": "",
            "inferred_label": "Member No.", "label_source": "row_cell", "label_confidence": 0.9,
            "attrs": {"name": "q"},
            "box": {"x": 0, "y": 0, "width": 10, "height": 10},
            "page_box": {"x": 0, "y": 0, "width": 10, "height": 10},
        }],
    }])
    text = obs.to_text()
    assert 'label="Member No." (row_cell)' in text
    assert "name=q" in text
    assert obs.control("c1").display_name() == "Member No."
    assert obs.control("nope") is None
