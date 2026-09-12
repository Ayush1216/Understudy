import pytest

from understudy.schema import Capability, LintError, assert_lints_clean, lint_capability

from .fixtures import member_balance, member_balance_dict


def codes(cap, **kw):
    return sorted({i.code for i in lint_capability(cap, **kw)})


def test_fixture_is_lint_clean():
    assert lint_capability(member_balance()) == []


def test_duplicate_step_ids():
    d = member_balance_dict()
    d["steps"][1]["id"] = "s1"
    assert "L001" in codes(Capability.model_validate(d))


def test_undeclared_input_and_secret_references():
    d = member_balance_dict()
    d["steps"][4]["action"]["value"] = "{{inputs.nope}}"
    d["steps"][0]["action"]["value"] = "{{secrets.NOPE}}"
    issues = lint_capability(Capability.model_validate(d))
    msgs = [i.message for i in issues if i.code == "L002"]
    assert any("inputs.nope" in m for m in msgs)
    assert any("secrets.NOPE" in m for m in msgs)


def test_extract_of_undeclared_output_and_never_extracted_required_output():
    d = member_balance_dict()
    d["steps"][6]["action"]["outputs"] = ["member_name"]  # drops primary_share_balance
    d["steps"][6]["action"]["outputs"].append("ghost")
    issues = [i for i in lint_capability(Capability.model_validate(d)) if i.code == "L003"]
    assert any("ghost" in i.message for i in issues)
    assert any("primary_share_balance" in i.message for i in issues)


def test_trivially_true_success_checkpoint():
    cap = member_balance(success_checkpoint={"kind": "url_matches", "pattern": ".*"})
    assert "L004" in codes(cap)
    cap = member_balance(success_checkpoint={"kind": "any", "of": [
        {"kind": "text_present", "text": "X"}, {"kind": "url_matches", "pattern": ".*"}]})
    assert "L004" in codes(cap)


def test_a_deposit_matching_the_digits_of_wait_ms_is_not_an_embedded_example():
    """L006 used to search the serialized checkpoint, where an example of 500 matches the digits of
    `wait_ms: 5000` and rejects a recording whose success text is a plain heading. Found by a real
    discovery run, not by a test."""
    d = member_balance_dict()
    d["inputs"][0]["example"] = "500"
    d["success_checkpoint"] = {"kind": "text_present", "text": "REVIEW NEW SHARE", "wait_ms": 5000}
    assert "L006" not in codes(Capability.model_validate(d))
    d["success_checkpoint"]["text"] = "DEPOSIT OF 500 POSTED"
    assert "L006" in codes(Capability.model_validate(d))


def test_success_checkpoint_may_not_embed_an_input_example():
    cap = member_balance(success_checkpoint={"kind": "text_present", "text": "Member 100234"})
    assert "L006" in codes(cap)


def test_value_as_locator_is_rejected():
    # The recorder saw "Lovelace, Ada" as an extracted value and then used it to locate a cell.
    d = member_balance_dict()
    d["outputs"][0]["source"]["target"]["strategies"] = [
        {"kind": "role_name", "role": "cell", "name": "Lovelace, Ada", "confidence": 0.95, "origin": "captured"},
    ]
    cap = Capability.model_validate(d)
    assert "L007" not in codes(cap)  # unknown to lint without the recorded values...
    assert "L007" in codes(cap, known_values=["Lovelace, Ada"])  # ...caught once the recorder tells it
    # An input example used as a locator is caught with no extra information.
    d["steps"][4]["action"]["target"]["strategies"][0] = {
        "kind": "inferred_label", "role": "textbox", "label": "100234", "confidence": 0.9, "origin": "captured"}
    assert "L007" in codes(Capability.model_validate(d))


def test_only_root_checkpoints_may_poll():
    d = member_balance_dict()
    d["outcomes"][0]["detect"]["wait_ms"] = 2000
    assert "L008" in codes(Capability.model_validate(d))
    d = member_balance_dict()
    d["success_checkpoint"] = {"kind": "all", "wait_ms": 3000, "of": [
        {"kind": "text_present", "text": "A", "wait_ms": 1000}]}
    assert "L008" in codes(Capability.model_validate(d))


def test_step_risk_within_declared_ceiling():
    d = member_balance_dict()
    d["steps"][5]["risk"] = "irreversible"
    assert "L009" in codes(Capability.model_validate(d))


def test_trivially_true_outcome_would_shadow_success():
    d = member_balance_dict()
    d["outcomes"][0]["detect"] = {"kind": "url_matches", "pattern": ".*"}
    assert "L010" in codes(Capability.model_validate(d))


def test_assert_lints_clean_raises_with_all_issues():
    d = member_balance_dict()
    d["steps"][1]["id"] = "s1"
    d["success_checkpoint"] = {"kind": "url_matches", "pattern": ".*"}
    with pytest.raises(LintError) as ei:
        assert_lints_clean(Capability.model_validate(d))
    assert {i.code for i in ei.value.issues} >= {"L001", "L004"}


def test_numeric_output_needs_a_numeric_transform():
    # It type-checks at record time and then fails on every replay: the screen holds "$310.42",
    # the contract promises a float, and `transform: none` bridges nothing.
    d = member_balance_dict()
    d["outputs"][1]["source"]["transform"] = "none"
    assert "L011" in codes(Capability.model_validate(d))
    d["outputs"][1]["source"]["transform"] = "currency_to_number"
    assert "L011" not in codes(Capability.model_validate(d))


def test_an_input_nothing_references_is_dead_weight_in_the_contract():
    d = member_balance_dict()
    d["inputs"].append({"name": "operator_id", "type": "string", "description": "unused", "example": "teller1"})
    assert "L012" in codes(Capability.model_validate(d))
