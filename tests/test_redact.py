import json

import pytest

from understudy.policy import MIN_LITERAL, SECRET_MARK, Redactor


def test_no_secret_survives_in_a_nested_structure():
    r = Redactor()
    r.register_secret("s3cr3t-P@ss")
    obj = {"a": ["x s3cr3t-P@ss y", {"b": ("s3cr3t-P@ss",), "c": 1}], "k": "s3cr3t-P@ss", "s3cr3t-P@ss": True}
    out = r.redact(obj)
    assert "s3cr3t-P@ss" not in json.dumps({k: v for k, v in out.items() if k != "s3cr3t-P@ss"})
    assert out["k"] == SECRET_MARK
    assert out["a"][0] == f"x {SECRET_MARK} y"
    assert out["a"][1]["b"] == (SECRET_MARK,)
    assert list(out) == list(obj)  # keys untouched


def test_pii_is_masked_not_replaced():
    r = Redactor()
    r.register_pii("100234")
    r.register_pii("Ada Lovelace")
    assert r.redact_text("member 100234 is Ada Lovelace") == "member 1***4 is A***e"


def test_longest_literal_wins():
    r = Redactor()
    r.register_secret("abcd")
    r.register_secret("abcdefgh")
    assert r.redact_text("abcdefgh") == SECRET_MARK
    assert r.redact_text("abcd!") == f"{SECRET_MARK}!"


def test_metacharacter_secret_is_matched_literally():
    r = Redactor()
    r.register_secret("a*b+c?")
    assert r.redact_text("x a*b+c? y") == f"x {SECRET_MARK} y"
    assert r.redact_text("aab") == "aab"
    r.register_secret(".*.*")
    assert r.redact_text("hello") == "hello"


def test_numeric_values_are_redacted_but_untouched_numbers_keep_their_type():
    r = Redactor()
    r.register_pii(100234)  # a number-typed PII input arrives as an int
    out = r.redact({"member": 100234, "seq": 7, "duration_ms": 1234, "ratio": 0.5, "ok": True, "none": None})
    assert out == {"member": "1***4", "seq": 7, "duration_ms": 1234, "ratio": 0.5, "ok": True, "none": None}
    assert type(out["seq"]) is int and type(out["ratio"]) is float and out["ok"] is True


def test_ssn_and_card_sweeps():
    r = Redactor()
    assert r.redact_text("ssn 123-45-6789 card 4111111111111111") == "ssn «redacted:ssn» card «redacted:card»"
    assert r.redact_text("member 100234 phone 5551234567") == "member 100234 phone 5551234567"
    assert r.redact({"card": 4111111111111111}) == {"card": "«redacted:card»"}


def test_key_value_sweep_keeps_the_key_and_drops_the_value():
    r = Redactor()
    assert r.redact_text("password=hunter2 Token: abc APP_PASSWORD=zz api_key=k9") == (
        "password=«redacted» Token: «redacted» APP_PASSWORD=«redacted» api_key=«redacted»"
    )
    assert r.redact_text("the password field") == "the password field"


def test_short_literals_are_ignored():
    r = Redactor()
    r.register_secret("abc")
    r.register_pii("Jo")
    r.register_secret("")
    assert len("abc") < MIN_LITERAL
    assert r.redact_text("abc Jo abcabc") == "abc Jo abcabc"


def test_secrets_match_case_sensitively():
    r = Redactor()
    r.register_secret("Hunter22")
    assert r.redact_text("hunter22 Hunter22") == f"hunter22 {SECRET_MARK}"


def test_literal_registered_as_both_is_treated_as_a_secret():
    r = Redactor()
    r.register_pii("100234")
    r.register_secret("100234")
    assert r.redact_text("100234") == SECRET_MARK


def test_unsupported_types_are_refused_rather_than_passed_through():
    r = Redactor()
    r.register_secret("hunter22")
    with pytest.raises(TypeError):
        r.redact({"hunter22"})
