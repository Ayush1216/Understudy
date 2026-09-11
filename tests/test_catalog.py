"""The catalog never opens a browser: every test here is filesystem-only."""

import json
import shutil
from pathlib import Path

import pytest

from understudy.catalog import (
    Catalog,
    capability_key_for,
    load_catalog,
    tool_name,
    write_capability,
)
from understudy.evidence import record_stability

from .fixtures import member_balance

KEY = "alpha.member.balance@1.0.0"


@pytest.fixture
def caps(tmp_path: Path) -> Path:
    d = tmp_path / "capabilities"
    write_capability(member_balance(), d)
    write_capability(member_balance(version="1.1.0", approval="draft"), d)
    return d


def test_every_file_passes_three_gates_and_one_bad_file_never_takes_down_the_catalog(caps: Path, tmp_path: Path):
    (caps / "corrupt.json").write_text("{not json", encoding="utf-8")
    (caps / "bad_schema.json").write_text(
        json.dumps(member_balance().model_dump(mode="json") | {"version": "1.0"}), encoding="utf-8")
    write_capability(member_balance(version="2.0.0", success_checkpoint={"kind": "url_matches", "pattern": ".*"}), caps)
    shutil.copy(caps / "alpha.member.balance.v1.0.0.json", caps / "dup.json")

    cat = load_catalog(caps, evidence_root=tmp_path / "evidence")

    assert set(cat.entries) == {KEY, "alpha.member.balance@1.1.0"}
    assert cat.paths[KEY] == caps / "alpha.member.balance.v1.0.0.json"
    reasons = {i.path.name: i.reason for i in cat.invalid}
    assert "Expecting" in reasons["corrupt.json"]
    assert "MAJOR.MINOR.PATCH" in reasons["bad_schema.json"]
    assert "L004" in reasons["alpha.member.balance.v2.0.0.json"]
    assert reasons["dup.json"].startswith(f"duplicate key {KEY}")
    assert load_catalog(tmp_path / "missing").entries == {}


def test_get_prefers_the_latest_semver_not_the_latest_string(tmp_path: Path):
    for v in ("1.0.0", "1.10.0", "1.9.0"):
        write_capability(member_balance(version=v), tmp_path)
    cat = load_catalog(tmp_path)
    assert cat.get("alpha.member.balance").version == "1.10.0"
    assert cat.get("alpha.member.balance", "1.9.0").version == "1.9.0"
    with pytest.raises(KeyError, match=r"available versions: \['1.0.0', '1.9.0', '1.10.0'\]"):
        cat.get("alpha.member.balance", "3.0.0")
    with pytest.raises(KeyError, match="no capability named 'nope'"):
        cat.get("nope")


def test_list_carries_the_calling_contract_and_the_stability_sidecar(caps: Path, tmp_path: Path):
    root = tmp_path / "evidence"
    cat = load_catalog(caps, evidence_root=root)
    row = cat.list()[0]
    assert row["key"] == KEY and row["approval"] == "approved" and row["tenant"] == "alpha"
    assert row["inputs"] == [{"name": "member_number", "type": "string", "required": True,
                              "description": "Six-digit member number", "sensitivity": "pii"}]
    assert [o["name"] for o in row["outputs"]] == ["member_name", "primary_share_balance"]
    assert row["outcomes"] == ["MEMBER_NOT_FOUND"]
    assert row["stability"] is None
    record_stability(root, KEY, None, "success", {"s6": 0})
    assert cat.list()[0]["stability"]["runs"] == 1


def test_describe_is_the_full_artifact(caps: Path):
    assert load_catalog(caps).describe("alpha.member.balance", "1.0.0") == member_balance().model_dump(mode="json")


def test_tool_schemas_export_approved_only_unless_drafts_are_asked_for(caps: Path):
    write_capability(member_balance(version="0.9.0", approval="deprecated"), caps)
    cat = load_catalog(caps)
    assert [t["function"]["name"] for t in cat.tool_schemas()] == ["alpha__member__balance__v_1_0_0"]
    assert [t["function"]["name"] for t in cat.tool_schemas(include_draft=True)] == [
        "alpha__member__balance__v_1_0_0", "alpha__member__balance__v_1_1_0"]
    fn = cat.tool_schemas()[0]["function"]
    assert fn["description"] == (
        "Look up a member and read their primary share balance. "
        "Returns: member_name (string), primary_share_balance (currency). Business outcomes: MEMBER_NOT_FOUND")
    assert fn["parameters"] == {
        "type": "object",
        "properties": {"member_number": {"type": "string", "description": "Six-digit member number", "pattern": r"^\d{6}$"}},
        "required": ["member_number"],
        "additionalProperties": False,
    }


def test_tool_schema_maps_enum_inputs_and_omits_secret_outputs_from_the_contract(tmp_path: Path):
    cap = member_balance(inputs=[
        {"name": "member_number", "type": "string", "description": "n", "pattern": r"^\d{6}$", "sensitivity": "pii", "example": "100234"},
        {"name": "share_type", "type": "enum", "enum_values": ["regular", "checking"], "description": "which", "required": False},
    ])
    d = cap.model_dump(mode="json")
    # Lint refuses an input no step uses, so give share_type a real consumer.
    d["steps"].insert(5, {
        "id": "s5b", "description": "Choose the share type",
        "action": {"type": "select", "value": "{{inputs.share_type}}",
                   "target": d["steps"][4]["action"]["target"]},
    })
    d["outputs"][0]["sensitivity"] = "secret"
    write_capability(cap.model_validate(d), tmp_path)
    fn = load_catalog(tmp_path).tool_schemas()[0]["function"]
    assert fn["parameters"]["properties"]["share_type"] == {"type": "string", "description": "which", "enum": ["regular", "checking"]}
    assert fn["parameters"]["required"] == ["member_number"]
    assert "Returns: primary_share_balance (currency)." in fn["description"]


def test_tool_name_round_trips_and_refuses_the_reserved_separator():
    for key in ("a_b.c_d@1.10.0", KEY):
        assert capability_key_for(tool_name(key)) == key
    assert tool_name("a_b.c_d@1.10.0") == "a_b__c_d__v_1_10_0"
    with pytest.raises(ValueError, match="'__'"):
        tool_name("a__b@1.0.0")


def test_approve_flips_draft_once_and_touches_nothing_else(caps: Path):
    cat = load_catalog(caps)
    path = caps / "alpha.member.balance.v1.1.0.json"
    before = path.read_text(encoding="utf-8")

    assert cat.approve(path).approval == "approved"
    after = path.read_text(encoding="utf-8")
    assert after == before.replace('"approval": "draft"', '"approval": "approved"', 1)
    assert cat.entries["alpha.member.balance@1.1.0"].approval == "approved"

    cat.approve(path)  # already approved: no-op
    assert path.read_text(encoding="utf-8") == after

    dep = write_capability(member_balance(version="0.9.0", approval="deprecated"), caps)
    with pytest.raises(ValueError, match="deprecated"):
        cat.approve(dep)


def test_write_capability_names_the_file_by_name_and_version(tmp_path: Path):
    path = write_capability(member_balance(version="1.2.3"), tmp_path / "new")
    assert path == tmp_path / "new" / "alpha.member.balance.v1.2.3.json"
    assert isinstance(load_catalog(tmp_path / "new"), Catalog)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == "1.2.3"
