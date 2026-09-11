from pathlib import Path

import pytest

from understudy.policy import (
    OperatorPolicy,
    PolicyEngine,
    classify_risk,
    effective_policy,
    glob_to_regex,
    intersect_for_run,
    load_operator_policy,
)
from understudy.schema import ALL_ACTION_TYPES, PolicyDeclaration
from understudy.surface import PolicyGate

from .fixtures import member_balance

CONFIG = Path(__file__).resolve().parents[1] / "config" / "policy.toml"
URL = "http://localhost:4599/t/alpha/members"


def _operator(**overrides) -> OperatorPolicy:
    fields = dict(
        origins=["http://localhost:4599"], path_patterns=["/t/*/**"], action_types=list(ALL_ACTION_TYPES),
        max_risk="irreversible", require_approval_for=["irreversible"],
        replay_timeout_ms=1000, discovery_timeout_ms=1000, max_steps=10,
    )
    fields.update(overrides)
    return OperatorPolicy(**fields)


def _engine(declaration: PolicyDeclaration | None = None, **overrides) -> PolicyEngine:
    return PolicyEngine(effective_policy(_operator(**overrides), declaration))


def _check(engine: PolicyEngine, **kw) -> str:
    args = dict(action_type="click", url=URL, risk="safe", mode="replay")
    args.update(kw)
    return engine.check(**args).decision


# ---- each dimension ----------------------------------------------------------------------


def test_action_type_outside_the_allowlist_is_denied():
    e = _engine(action_types=["click", "wait"])
    assert _check(e, action_type="click") == "allow"
    assert _check(e, action_type="type") == "deny"
    assert _check(e, action_type="teleport") == "deny"


def test_url_actions_fail_closed_without_a_resolvable_url():
    e = _engine()
    assert _check(e, url=None) == "deny"
    assert _check(e, url="not a url") == "deny"
    assert _check(e, url="/t/alpha/") == "deny"
    assert _check(e, url="http://[::1/t/alpha/") == "deny"


def test_wait_assert_and_extract_need_no_url():
    e = _engine()
    for t in ("wait", "assert", "extract"):
        assert _check(e, action_type=t, url=None) == "allow"


def test_origin_must_match_exactly():
    e = _engine()
    assert _check(e, url="http://localhost:4599/t/alpha/") == "allow"
    assert _check(e, url="https://localhost:4599/t/alpha/") == "deny"
    assert _check(e, url="http://localhost:4600/t/alpha/") == "deny"
    assert _check(e, url="http://evil.example/t/alpha/") == "deny"
    assert _check(e, url="http://localhost:4599.evil.example/t/alpha/") == "deny"


def test_path_must_match_the_allowlist_and_query_is_ignored():
    e = _engine()
    assert _check(e, url="http://localhost:4599/t/alpha/members?id=1#x") == "allow"
    assert _check(e, url="http://localhost:4599/admin") == "deny"
    d = e.check(action_type="click", url="http://localhost:4599/admin", risk="safe", mode="replay")
    assert "/admin" in d.reason and "/t/*/**" in d.reason
    assert _check(e, url="http://localhost:4599/t/alpha/a.b/c..d") == "allow"  # dots inside a segment are literal


@pytest.mark.parametrize("url", [
    "http://localhost:4599/t/alpha/../../admin",
    "http://localhost:4599/t/alpha/%2e%2e/%2e%2e/admin",
    "http://localhost:4599/t/alpha/.%2E/admin",
    "http://localhost:4599/t/alpha\\..\\..\\admin",
    "http://localhost:4599/t/alpha/.\t./.\t./admin",
])
def test_dot_segments_backslashes_and_control_chars_cannot_escape_the_path_allowlist(url: str):
    # The browser normalizes every one of these to http://localhost:4599/admin before requesting.
    assert _check(_engine(), action_type="navigate", url=url) == "deny"


def test_risk_above_the_ceiling_is_denied():
    e = _engine(max_risk="safe")
    assert _check(e, risk="safe") == "allow"
    assert _check(e, risk="sensitive") == "deny"
    assert _check(e, risk="not-a-risk") == "deny"


def test_replay_requires_approval_and_discovery_denies_outright():
    e = _engine()
    assert _check(e, risk="irreversible", mode="replay") == "require_approval"
    assert _check(e, risk="irreversible", mode="replay", approval_granted=True) == "allow"
    assert _check(e, risk="irreversible", mode="discovery") == "deny"
    assert _check(e, risk="irreversible", mode="discovery", approval_granted=True) == "deny"
    assert _check(e, risk="sensitive", mode="discovery") == "allow"
    # An unrecognised mode must not fall into the more permissive replay branch.
    assert _check(e, risk="irreversible", mode="Discovery", approval_granted=True) == "deny"
    assert _check(e, risk="irreversible", mode=None, approval_granted=True) == "deny"


def test_check_navigation_holds_redirects_to_the_same_allowlist():
    e = _engine()
    assert e.check_navigation("http://localhost:4599/t/alpha/login").decision == "allow"
    assert e.check_navigation("http://evil.example/phish").decision == "deny"


def test_engine_satisfies_the_surface_gate_contract():
    gate: PolicyGate = _engine()
    d = gate.check(action_type="click", url=URL, risk="safe", mode="replay", approval_granted=False)
    assert d.decision == "allow" and d.reason


# ---- intersection: the artifact can only narrow --------------------------------------------


def test_declaration_cannot_widen_origins():
    e = _engine(PolicyDeclaration(origins=["http://evil.example"]))
    assert e.effective.origins == []
    assert _check(e, url="http://evil.example/t/alpha/") == "deny"
    assert _check(e, url=URL) == "deny"


def test_declaration_cannot_lower_require_approval_for():
    e = _engine(PolicyDeclaration(origins=["http://localhost:4599"], max_risk="irreversible", require_approval_for=[]))
    assert e.effective.require_approval_for == ["irreversible"]
    assert _check(e, risk="irreversible") == "require_approval"


def test_declaration_cannot_raise_max_risk():
    e = _engine(PolicyDeclaration(origins=["http://localhost:4599"], max_risk="irreversible"), max_risk="safe")
    assert e.effective.max_risk == "safe"
    assert _check(e, risk="sensitive") == "deny"


def test_declaration_can_narrow_paths_and_action_types():
    e = _engine(PolicyDeclaration(origins=["http://localhost:4599"], path_patterns=["/t/alpha/**"], action_types=["click", "extract"]))
    assert _check(e, url="http://localhost:4599/t/alpha/x") == "allow"
    assert _check(e, url="http://localhost:4599/t/beta/x") == "deny"
    assert _check(e, action_type="type") == "deny"


def test_declaration_adds_to_require_approval_for():
    e = _engine(PolicyDeclaration(origins=["http://localhost:4599"], max_risk="irreversible", require_approval_for=["sensitive"]))
    assert e.effective.require_approval_for == ["sensitive", "irreversible"]
    assert _check(e, risk="sensitive") == "require_approval"


def test_limits_come_from_the_operator_alone():
    e = _engine(PolicyDeclaration(origins=["http://localhost:4599"]), max_steps=3)
    assert (e.effective.max_steps, e.effective.replay_timeout_ms) == (3, 1000)


# ---- config file --------------------------------------------------------------------------


def test_operator_config_loads():
    op = load_operator_policy(CONFIG)
    assert "http://localhost:4599" in op.origins
    assert op.require_approval_for == ["irreversible"]
    assert op.max_steps == 60


def test_intersect_for_run_narrows_the_config_to_the_artifact():
    engine = intersect_for_run(CONFIG, member_balance().policy_declaration)
    assert _check(engine, url="http://localhost:4599/t/alpha/") == "allow"
    # The operator allows 127.0.0.1; the artifact only asked for localhost.
    assert _check(engine, url="http://127.0.0.1:4599/t/alpha/") == "deny"
    assert _check(engine, url="http://localhost:4599/t/beta/") == "deny"
    assert _check(engine, risk="sensitive") == "deny"


# ---- globs --------------------------------------------------------------------------------


@pytest.mark.parametrize("pattern,path,ok", [
    ("/t/*/**", "/t/alpha/", True),
    ("/t/*/**", "/t/alpha", True),
    ("/t/*/**", "/t/alpha/members/1", True),
    ("/t/*/**", "/admin", False),
    ("/t/*/**", "/tt/alpha/", False),
    ("/**", "/", True),
    ("/**", "/a/b/c", True),
    ("/a/*", "/a/b", True),
    ("/a/*", "/a/", True),
    ("/a/*", "/a/b/c", False),
    ("/a/*.html", "/a/x.html", True),
    ("/a/*.html", "/a/xxhtml", False),
    ("**/login", "/login", True),
    ("**/login", "/t/alpha/login", True),
    ("**/login", "/loginx", False),
    ("/t/**/confirm", "/t/confirm", True),
    ("/t/**/confirm", "/t/a/b/confirm", True),
    ("/t/**/confirm", "/t/a/confirmed", False),
])
def test_glob_edge_cases(pattern: str, path: str, ok: bool):
    assert bool(glob_to_regex(pattern).fullmatch(path)) is ok


# ---- risk ---------------------------------------------------------------------------------


@pytest.mark.parametrize("action_type,name,submit,expected", [
    ("click", "Confirm", False, "irreversible"),
    ("click", "Post Transaction", False, "irreversible"),
    ("click", "Sign off", False, "irreversible"),
    ("click", "Delete", False, "irreversible"),
    ("click", "Save", False, "sensitive"),
    ("click", "New Share", False, "sensitive"),
    ("click", "Review", False, "sensitive"),
    ("click", "Retrieve", False, "safe"),
    ("click", "Member Search", False, "safe"),
    ("click", "Postal Code", False, "safe"),       # word boundary: "post" inside "postal"
    ("click", "Retrieve", True, "irreversible"),   # a form submit is irreversible whatever it says
    ("press", "Enter", True, "irreversible"),
    ("type", "Confirm", False, "safe"),
    ("navigate", "", False, "safe"),
    ("click", None, False, "safe"),                # a control with no accessible name
])
def test_risk_classification_table(action_type: str, name: str | None, submit: bool, expected: str):
    assert classify_risk(action_type=action_type, control_name=name, is_form_submit=submit) == expected
