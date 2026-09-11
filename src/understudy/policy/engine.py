"""The allowlist engine: four independent dimensions, all failing closed.

The security boundary is the operator-side config/policy.toml. A capability's
`policy_declaration` is a REQUEST; the policy a run actually executes under is the
intersection of the two (see `effective_policy`).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from understudy.schema import RISK_RANK, ActionType, PolicyDeclaration, Risk, StrictModel

# Everything except wait/assert/extract touches the surface, so it must carry a URL.
_URL_ACTIONS: frozenset[str] = frozenset({"navigate", "click", "type", "select", "press"})


class OperatorPolicy(StrictModel):
    """What the operator grants. Loaded from config/policy.toml and never from an artifact."""

    origins: list[str]
    path_patterns: list[str]
    action_types: list[ActionType]
    max_risk: Risk
    require_approval_for: list[Risk]
    replay_timeout_ms: int
    discovery_timeout_ms: int
    max_steps: int


class EffectivePolicy(StrictModel):
    origins: list[str]
    # One allowlist per party. A path must match a pattern from EVERY inner list: the
    # intersection of two glob languages is not itself a glob, so both are kept and ANDed.
    path_patterns: list[list[str]]
    action_types: list[ActionType]
    max_risk: Risk
    require_approval_for: list[Risk]
    replay_timeout_ms: int
    discovery_timeout_ms: int
    max_steps: int


class Decision(StrictModel):
    decision: Literal["allow", "deny", "require_approval"]
    reason: str


def load_operator_policy(path: Path) -> OperatorPolicy:
    with path.open("rb") as f:
        t = tomllib.load(f)
    return OperatorPolicy.model_validate({**t["allow"], **t["risk"], **t["limits"]})


def effective_policy(operator: OperatorPolicy, declaration: PolicyDeclaration | None) -> EffectivePolicy:
    """Operator grant ∩ artifact declaration. The artifact can only ever narrow: origins and
    action types intersect, both path lists are kept and ANDed, `max_risk` is the lower of the
    two, `require_approval_for` is the union, limits are the operator's alone.

    Building the engine from the artifact alone would put the security boundary inside the
    thing being reviewed: an artifact shipping `require_approval_for: []` would post an
    irreversible transaction with no gate at all. So the artifact's block is a request, and
    this is where it is granted — never more than the operator already allows.
    """
    fields = operator.model_dump()
    fields["path_patterns"] = [operator.path_patterns]
    if declaration is not None:
        fields.update(
            origins=[o for o in operator.origins if o in declaration.origins],
            path_patterns=[operator.path_patterns, declaration.path_patterns],
            action_types=[a for a in operator.action_types if a in declaration.action_types],
            max_risk=min(operator.max_risk, declaration.max_risk, key=lambda r: RISK_RANK[r]),
            require_approval_for=sorted(
                {*operator.require_approval_for, *declaration.require_approval_for},
                key=lambda r: RISK_RANK[r],
            ),
        )
    return EffectivePolicy.model_validate(fields)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Path glob → regex for `fullmatch`. `*` stays within one segment; `**` spans segments;
    a trailing `/**` also matches nothing, so `/t/*/**` covers `/t/alpha` as well as everything
    under it. Applied to the path only; query and fragment are never consulted."""

    def piece(m: re.Match[str]) -> str:
        tok = m.group()
        if tok == "/**":
            return "(?:/.*)?"
        if tok == "**/":
            return "(?:.*/)?"
        if tok == "**":
            return ".*"
        if tok == "*":
            return "[^/]*"
        return re.escape(tok)

    # `/` is its own token so `/**` can be recognised before a literal run swallows the slash.
    return re.compile(re.sub(r"/\*\*|\*\*/|\*\*|\*|/|[^*/]+", piece, pattern))


def _deny(reason: str) -> Decision:
    return Decision(decision="deny", reason=reason)


class PolicyEngine:
    """Satisfies surface.protocol.PolicyGate structurally (no import: policy sits below the
    surface, and the surface receives its gate by injection)."""

    def __init__(self, effective: EffectivePolicy) -> None:
        self.effective = effective
        self._path_regexes = [[glob_to_regex(p) for p in pats] for pats in effective.path_patterns]

    def check(
        self,
        *,
        action_type: str,
        url: str | None,
        risk: Risk,
        mode: Literal["discovery", "replay"],
        approval_granted: bool = False,
    ) -> Decision:
        e = self.effective
        if action_type not in e.action_types:
            return _deny(f"action type {action_type!r} is not allowed")
        if mode not in ("discovery", "replay"):
            return _deny(f"unknown mode {mode!r}")
        if action_type in _URL_ACTIONS:
            if url is None:
                return _deny(f"no resolvable URL for {action_type!r}; failing closed")
            # The browser strips tab/newline, reads `\` as `/` and resolves `..` (even %2e-encoded)
            # BEFORE requesting, so the gate must refuse anything it cannot match as-served.
            if re.search(r"[\x00-\x1f\x7f]", url):
                return _deny(f"control character in URL {url!r}")
            try:
                parts = urlsplit(url)
            except ValueError:
                return _deny(f"unparseable URL {url!r}")
            if not parts.scheme or not parts.netloc:
                return _deny(f"unparseable URL {url!r}")
            origin = f"{parts.scheme}://{parts.netloc}"
            if origin not in e.origins:
                return _deny(f"origin {origin!r} is not allowed")
            path = parts.path.replace("\\", "/") or "/"
            if any(seg.lower().replace("%2e", ".") in (".", "..") for seg in path.split("/")):
                return _deny(f"path {parts.path!r} contains dot-segments; failing closed")
            for patterns, regexes in zip(e.path_patterns, self._path_regexes):
                if not any(r.fullmatch(path) for r in regexes):
                    return _deny(f"path {path!r} matches none of {patterns}")
        if risk not in RISK_RANK or RISK_RANK[risk] > RISK_RANK[e.max_risk]:
            return _deny(f"risk {risk!r} exceeds the ceiling {e.max_risk!r}")
        if risk in e.require_approval_for:
            if mode == "discovery":
                return _deny(f"{risk} actions are never performed during discovery; stalling beats posting a transaction")
            if not approval_granted:
                return Decision(decision="require_approval", reason=f"{risk} action needs human approval")
            return Decision(decision="allow", reason=f"{risk} action approved by a human")
        return Decision(decision="allow", reason="within policy")

    def check_navigation(self, url: str) -> Decision:
        """For the framenavigated listener: every navigation, including redirects the
        artifact never asked for, is held to the same origin and path allowlists."""
        return self.check(action_type="navigate", url=url, risk="safe", mode="replay")


def intersect_for_run(operator_path: Path, declaration: PolicyDeclaration | None) -> PolicyEngine:
    return PolicyEngine(effective_policy(load_operator_policy(operator_path), declaration))
