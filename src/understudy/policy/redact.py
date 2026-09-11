"""Redaction, applied at the sinks (event log, evidence writer, intervention store) so nothing
sensitive can be written by construction. Deny-list shaped: registered literals plus a few
regex sweeps. An undeclared PII field can still reach evidence; that limit is named in the
report rather than papered over."""

from __future__ import annotations

import re
from typing import Any

# A demo password of "password" would otherwise eat the word out of every description.
MIN_LITERAL = 4
SECRET_MARK = "«redacted:secret»"

_SWEEPS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "«redacted:ssn»"),
    (re.compile(r"\b\d{13,16}\b"), "«redacted:card»"),
    # No leading \b: APP_PASSWORD=x must match on PASSWORD. Keeps the key, drops the value.
    (re.compile(r"(?i)(password|passwd|pwd|token|secret|api[_-]?key)(\s*[=:]\s*)\S+"), r"\1\2«redacted»"),
]


def _mask(value: str) -> str:
    return "***" if len(value) <= 2 else value[0] + "***" + value[-1]


class Redactor:
    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._pii: set[str] = set()
        self._literals: re.Pattern[str] | None = None

    # ParamSpec allows type=number with sensitivity=pii, so a member number can arrive as an int.
    def register_secret(self, value: object) -> None:
        self._secrets.add(str(value))
        self._rebuild()

    def register_pii(self, value: object) -> None:
        self._pii.add(str(value))
        self._rebuild()

    def _rebuild(self) -> None:
        # Longest first: alternation takes the first branch that matches, not the longest, so
        # a shorter secret must not shadow a longer one that starts at the same offset.
        # re.escape covers the full metacharacter set; a secret containing '*' is a literal.
        lits = sorted((s for s in self._secrets | self._pii if len(s) >= MIN_LITERAL), key=len, reverse=True)
        self._literals = re.compile("|".join(map(re.escape, lits))) if lits else None

    def redact_text(self, s: str) -> str:
        if self._literals is not None:
            s = self._literals.sub(lambda m: SECRET_MARK if m.group() in self._secrets else _mask(m.group()), s)
        for rx, repl in _SWEEPS:
            s = rx.sub(repl, s)
        return s

    def redact(self, obj: Any) -> Any:
        """Recursive over dict/list/tuple/str and scalars; dict keys are left alone. Anything
        else is refused: a sink must normalize (evidence.jsonable) first, or json.dumps would
        stringify a model AFTER redaction and carry a secret to disk."""
        if isinstance(obj, str):
            return self.redact_text(obj)
        if isinstance(obj, bool) or obj is None:
            return obj
        if isinstance(obj, (int, float)):
            # A balance or member id genuinely arrives as a number; a step seq or duration
            # must stay a number. Redact the text form and keep the original only if untouched.
            text = self.redact_text(str(obj))
            return obj if text == str(obj) else text
        if isinstance(obj, dict):
            return {k: self.redact(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self.redact(v) for v in obj)
        raise TypeError(f"redact: unsupported type {type(obj).__name__}; pass JSON-native data")
