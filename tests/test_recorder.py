"""The recorder as a pure function: hand-built trace entries in, a capability out. No browser,
no model. The claims are the ways the recorder overrules a model that over-fits to one record."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from understudy.discover import DiscoveryOptions, Finish, TraceEntry, compile
from understudy.evidence import RunLogger
from understudy.policy import Redactor
from understudy.schema import (
    BusinessOutcome,
    ClickAction,
    ControlPresent,
    LintError,
    NavigateAction,
    OutputSource,
    OutputSpec,
    ParamSpec,
    Provenance,
    TargetDescriptor,
    TextPresent,
    TypeAction,
    lint_capability,
)

ENTRY = "http://localhost:4599/t/alpha/"
SIGNON, MENU, SEARCH, RECORD = ENTRY + "signon", ENTRY + "menu", ENTRY + "search", ENTRY + "members/100234"

ATTR_Q = {"kind": "attribute", "attr": "name", "value": "q", "role": "textbox", "confidence": 0.7, "origin": "captured"}
NTH = {"kind": "nth_of_role", "role": "textbox", "index": 0, "confidence": 0.55, "origin": "derived"}


def td(role="textbox", strategies=(ATTR_Q,), *, intent="act", label="Member No."):
    return TargetDescriptor(role=role, label=label, frame_path=["content"], intent=intent, strategies=list(strategies))


def role_name(role, name):
    return {"kind": "role_name", "role": role, "name": name, "confidence": 0.95, "origin": "captured"}


NAME_CELL = td("cell", [{"kind": "inferred_label", "role": "cell", "label": "Name", "confidence": 0.9, "origin": "captured"}], intent="read", label="Name")
BALANCE_CELL = td("cell", [{"kind": "table_cell", "row_anchor": "Regular Shares", "column": "Balance", "anchor_column": "Type", "confidence": 0.85, "origin": "captured"}], intent="read", label="Balance")


def typed(seq, value, target=None):
    t = target or td()
    return TraceEntry(seq=seq, tool="type_text", args={"ref": "c1", "text": value}, why="enter the member number",
                      target=t, action=TypeAction(type="type", target=t, value=value), url_before=SEARCH, url_after=SEARCH)


def clicked(seq, name="Retrieve", *, navigated=True, url_before=SEARCH, url_after=RECORD, text_before="", text_after="MEMBER RECORD"):
    t = td("button", [role_name("button", name)], label=name)
    return TraceEntry(seq=seq, tool="click", args={"ref": "c2"}, why=f"press {name}", target=t, action=ClickAction(type="click", target=t),
                      url_before=url_before, url_after=url_after, navigated=navigated, text_before=text_before, text_after=text_after)


def navigated_to(seq, url, *, url_before):
    return TraceEntry(seq=seq, tool="navigate", args={"url": url}, why="go back", action=NavigateAction(type="navigate", url=url),
                      url_before=url_before, url_after=url, navigated=True)


def asserted(seq, text):
    return TraceEntry(seq=seq, tool="assert_checkpoint", args={"kind": "text_present", "text": text}, why="a new screen")


def extracted(seq, name, target):
    return TraceEntry(seq=seq, tool="extract", args={"ref": "c9", "output_name": name, "type": "string", "description": name}, why=f"read {name}", target=target)


def output(name, target, type="string"):
    transform = "currency_to_number" if type in ("currency", "number") else "trim"
    return OutputSpec(name=name, type=type, description=name,
                      source=OutputSource(kind="text_of", target=target, transform=transform))


def outcome(code, text):
    return BusinessOutcome(code=code, description=code, detect=TextPresent(kind="text_present", text=text))


@pytest.fixture
def compiled(tmp_path):
    events = []
    logger = RunLogger("discover-test", tmp_path / "run.jsonl", Redactor())
    logger.subscribe(events.append)

    def run(trace, *, inputs=None, declared_inputs=(), outputs=(), outcomes=(), success_text="SHARES / BALANCES",
            visible_text="MEMBER RECORD\nSHARES / BALANCES", **opts):
        cap = compile(
            trace, declared_inputs=list(declared_inputs), declared_outputs=list(outputs), declared_outcomes=list(outcomes),
            finish=Finish(summary="Look up a member.", success_text=success_text, visible_text=visible_text),
            options=DiscoveryOptions(goal="look up member 100234 and read their balance", entry_url=ENTRY, inputs=inputs or {}, **opts),
            provenance=Provenance(recorded_at=datetime.now(timezone.utc), goal="g", model="scripted", discovery_run_id="discover-test"),
            logger=logger,
        )
        return cap, [e for e in events if e["type"] == "recorder.overruled"]

    return run


def test_supplied_input_value_is_parameterized_in_values_urls_and_checkpoints(compiled):
    trace = [typed(1, "100234"), clicked(2), asserted(3, "Record for 100234"), navigated_to(4, SEARCH + "?q=100234", url_before=RECORD)]
    cap, overruled = compiled(trace, inputs={"member_number": "100234"}, success_text="Member 100234 shares")
    assert cap.steps[0].action.value == "{{inputs.member_number}}"
    assert cap.steps[1].postcondition.text == "Record for {{inputs.member_number}}"
    assert cap.steps[2].action.url == SEARCH + "?q={{inputs.member_number}}"
    assert cap.success_checkpoint.text == "Member {{inputs.member_number}} shares"
    auto = cap.input_by_name("member_number")
    assert auto.sensitivity == "pii" and auto.example == "100234"  # never declared by the model
    assert len(overruled) == 4 and all("{{inputs.member_number}}" in e["why"] for e in overruled)
    assert lint_capability(cap, known_values=["100234"]) == []


def test_a_short_supplied_value_is_parameterized_on_exact_match_only(compiled):
    cap, _ = compiled([typed(1, "42"), asserted(2, "Page 42")], inputs={"n": "42"})
    assert cap.steps[0].action.value == "{{inputs.n}}"
    assert cap.steps[0].postcondition.text == "Page 42"  # a two-character substring is not a value


def test_a_declared_example_is_parameterized_when_nothing_was_supplied(compiled):
    declared = [ParamSpec(name="member_number", type="string", description="Member number", example="100234")]
    cap, _ = compiled([typed(1, "100234"), clicked(2)], declared_inputs=declared)
    assert cap.steps[0].action.value == "{{inputs.member_number}}"
    assert cap.inputs == declared


def test_a_navigated_step_without_an_assert_gets_the_first_new_line_that_is_not_a_value(compiled):
    click = clicked(1, "Sign On", url_before=SIGNON, url_after=MENU,
                    text_before="OPERATOR SIGN ON\nOperator ID:\nTry 100234 first",
                    text_after="OPERATOR SIGN ON\nTry 100234 first\nID 100234 ok\nMAIN MENU\nSelect a function:")
    cap, _ = compiled([click], inputs={"member_number": "100234"})
    assert cap.steps[0].expects_navigation is True
    assert cap.steps[0].postcondition == TextPresent(kind="text_present", text="MAIN MENU", wait_ms=8000)


def test_a_type_without_navigation_asserts_its_own_control_and_a_plain_click_asserts_nothing(compiled):
    cap, _ = compiled([asserted(1, "MEMBER INQUIRY"), typed(2, "x"), clicked(3, "Reset", navigated=False, url_after=SEARCH)])
    assert cap.steps[0].preconditions == [TextPresent(kind="text_present", text="MEMBER INQUIRY", wait_ms=8000)]
    assert cap.steps[0].postcondition == ControlPresent(kind="control_present", target=td(), wait_ms=2000)
    assert cap.steps[1].postcondition is None and cap.steps[1].expects_navigation is False


def test_a_dead_end_and_the_asserts_made_on_it_are_pruned(compiled):
    trace = [
        clicked(1, "Open New Share", url_before=RECORD, url_after=RECORD + "/open-share", text_after="OPEN NEW SHARE"),
        asserted(2, "OPEN NEW SHARE"),
        navigated_to(3, RECORD, url_before=RECORD + "/open-share"),
        clicked(4, "Retrieve"), asserted(5, "MEMBER RECORD"),
    ]
    cap, overruled = compiled(trace)
    assert [s.description for s in cap.steps] == ["press Retrieve"]
    assert cap.steps[0].postcondition.text == "MEMBER RECORD"
    assert any("dead end" in e["why"] for e in overruled)


def test_a_strategy_naming_a_data_value_is_dropped_with_derived_rungs_as_the_fallback(compiled):
    cap, overruled = compiled([typed(1, "100234", td(strategies=[role_name("textbox", "100234"), ATTR_Q]))], inputs={"member_number": "100234"})
    assert cap.steps[0].action.target.strategy_kinds() == ["attribute"]
    assert any("data value" in e["why"] for e in overruled)
    cap, _ = compiled([typed(1, "100234", td(strategies=[role_name("textbox", "100234"), NTH]))], inputs={"member_number": "100234"})
    assert cap.steps[0].action.target.strategy_kinds() == ["nth_of_role"]


def test_a_read_target_whose_only_anchor_is_the_extracted_value_fails_lint(compiled):
    by_value = td("cell", [{"kind": "inferred_label", "role": "cell", "label": "Lovelace, Ada", "confidence": 0.9, "origin": "captured"}], intent="read")
    with pytest.raises(LintError, match="L007"):
        compiled([clicked(1), extracted(2, "member_name", by_value)], outputs=[(output("member_name", by_value), "Lovelace, Ada")])


def test_an_asserted_text_embedding_an_extracted_value_becomes_control_present(compiled):
    trace = [clicked(1), asserted(2, "Lovelace, Ada"), extracted(3, "member_name", NAME_CELL)]
    cap, overruled = compiled(trace, outputs=[(output("member_name", NAME_CELL), "Lovelace, Ada")])
    assert cap.steps[0].postcondition == ControlPresent(kind="control_present", target=NAME_CELL, wait_ms=8000)
    assert cap.steps[1].action.outputs == ["member_name"]
    assert any(e["what"] == "steps[s1].postcondition" for e in overruled)


def test_a_success_text_embedding_an_extracted_value_becomes_control_present_on_the_last_extraction(compiled):
    trace = [clicked(1), extracted(2, "member_name", NAME_CELL), extracted(3, "primary_share_balance", BALANCE_CELL)]
    outputs = [(output("member_name", NAME_CELL), "Lovelace, Ada"), (output("primary_share_balance", BALANCE_CELL, "currency"), "$2,499.00")]
    cap, _ = compiled(trace, outputs=outputs, success_text="Balance $2,499.00")
    assert cap.success_checkpoint == ControlPresent(kind="control_present", target=BALANCE_CELL, wait_ms=5000)
    assert cap.steps[1].action.outputs == ["member_name", "primary_share_balance"]  # consecutive extracts merge
    assert lint_capability(cap, known_values=["Lovelace, Ada", "$2,499.00"]) == []


def test_an_outcome_already_true_on_the_final_screen_is_dropped(compiled):
    cap, overruled = compiled([clicked(1)], outcomes=[outcome("MEMBER_NOT_FOUND", "RECORD NOT FOUND"), outcome("SHADOW", "MEMBER RECORD")])
    assert [o.code for o in cap.outcomes] == ["MEMBER_NOT_FOUND"]
    assert [e["what"] for e in overruled] == ["outcomes[SHADOW]"]


def test_secrets_policy_declaration_and_name_are_derived_from_the_trace(compiled):
    trace = [typed(1, "{{secrets.APP_OPERATOR}}"), clicked(2, "Sign On", url_before=SIGNON, url_after=MENU, text_after="MAIN MENU")]
    cap, _ = compiled(trace, tenant="alpha", app="harborline-cu")
    assert cap.secrets_required == ["APP_OPERATOR"]
    assert cap.policy_declaration.origins == ["http://localhost:4599"]
    assert cap.policy_declaration.path_patterns == ["/t/alpha/**"] and cap.policy_declaration.max_risk == "safe"
    assert (cap.name, cap.id, cap.version, cap.approval) == ("look.up.member.100234.and", "cap_look_up_member_100234_and", "1.0.0", "draft")
    assert [s.id for s in cap.steps] == ["s1", "s2"] and cap.recoveries == []
    assert cap.target.tenant == "alpha" and cap.target.entry_url == ENTRY
    assert lint_capability(cap) == []


def test_a_declared_input_no_step_uses_is_dropped_from_the_contract(compiled):
    declared = [
        ParamSpec(name="member_number", type="string", description="Member number", example="100234"),
        ParamSpec(name="operator_id", type="string", description="Operator", example="teller1"),
    ]
    cap, overruled = compiled([typed(1, "100234"), clicked(2)], declared_inputs=declared)
    assert [p.name for p in cap.inputs] == ["member_number"]
    assert any(e["what"] == "inputs.operator_id" for e in overruled)
