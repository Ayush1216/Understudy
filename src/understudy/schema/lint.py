"""Structural checks Pydantic can't express. Runs at record time, at catalog load, after any
tenant merge, and at the top of every replay. A capability that fails lint is never executed.

The rules that matter most encode "a recording that only passes for the row it was recorded
against is a broken recording" as a machine check.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from typing import Any

from .capability import Capability
from .checkpoint import All, AnyOf, Checkpoint, UrlMatches, iter_checkpoints
from .common import RISK_RANK, StrictModel
from .step import ExtractAction

TEMPLATE_REF = re.compile(r"\{\{\s*(inputs|secrets)\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class LintIssue(StrictModel):
    code: str
    message: str
    path: str = ""


def _refs_in(obj) -> set[tuple[str, str]]:
    return set(TEMPLATE_REF.findall(json.dumps(obj, default=str)))


def _strings(value: Any) -> Iterator[str]:
    """Every string inside a dumped model, numbers and nulls excluded."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _trivially_true(cp: Checkpoint) -> bool:
    if isinstance(cp, UrlMatches):
        return cp.pattern in ("", ".*", "^.*$", ".+", "^")
    if isinstance(cp, AnyOf):
        return any(_trivially_true(c) for c in cp.of)
    if isinstance(cp, All):
        return all(_trivially_true(c) for c in cp.of)
    return False


def _strategy_identifiers(cap: Capability) -> Iterable[tuple[str, str]]:
    """(path, identifying string) for every strategy in the artifact."""
    def walk(obj, path):
        if isinstance(obj, dict):
            if "strategies" in obj:
                for i, s in enumerate(obj["strategies"]):
                    for k in ("name", "label", "text", "row_anchor", "value", "column"):
                        v = s.get(k)
                        if isinstance(v, str) and v:
                            yield f"{path}.strategies[{i}].{k}", v
            for k, v in obj.items():
                yield from walk(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from walk(v, f"{path}[{i}]")
    yield from walk(cap.model_dump(mode="json"), "$")


def lint_capability(cap: Capability, known_values: Iterable[str] = ()) -> list[LintIssue]:
    """`known_values`: concrete data values seen during recording (inputs supplied, outputs
    extracted). A strategy whose identifying string IS one of them located the element by the
    data it happened to contain, and will silently target a different element for the next
    record. The recorder passes these; at catalog load only input examples are known."""
    issues: list[LintIssue] = []

    # L001 step ids unique
    ids = [s.id for s in cap.steps]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        issues.append(LintIssue(code="L001", message=f"duplicate step ids: {sorted(dupes)}", path="$.steps"))

    # L002 every template reference is declared
    declared_inputs = {p.name for p in cap.inputs}
    declared_secrets = set(cap.secrets_required)
    refs = _refs_in(cap.model_dump(mode="json", exclude={"provenance"}))
    for kind, name in sorted(refs):
        if kind == "inputs" and name not in declared_inputs:
            issues.append(LintIssue(code="L002", message=f"{{{{inputs.{name}}}}} is not a declared input"))
        if kind == "secrets" and name not in declared_secrets:
            issues.append(LintIssue(code="L002", message=f"{{{{secrets.{name}}}}} is not in secrets_required"))

    # L003 extract names exist in outputs
    output_names = {o.name for o in cap.outputs}
    for s in cap.steps:
        if isinstance(s.action, ExtractAction):
            for n in s.action.outputs:
                if n not in output_names:
                    issues.append(LintIssue(code="L003", message=f"step {s.id} extracts undeclared output {n!r}", path=f"$.steps[{s.id}]"))
    # ... and every required output is extracted by some step
    extracted = {n for s in cap.steps if isinstance(s.action, ExtractAction) for n in s.action.outputs}
    for o in cap.outputs:
        if o.required and o.source.kind != "url_capture" and o.name not in extracted:
            issues.append(LintIssue(code="L003", message=f"required output {o.name!r} is never extracted by any step", path="$.outputs"))

    # L004 success checkpoint is not trivially true
    if _trivially_true(cap.success_checkpoint):
        issues.append(LintIssue(code="L004", message="success_checkpoint is trivially true", path="$.success_checkpoint"))

    # L006 no input example literal inside the success checkpoint — green once, useless after.
    # Only the checkpoint's TEXT is searched: against the serialized JSON, a deposit of 500 matches
    # the digits of `wait_ms: 5000` and kills a recording whose success text is a screen heading.
    success_text = list(_strings(cap.success_checkpoint.model_dump(mode="json")))
    for p in cap.inputs:
        ex = str(p.example) if p.example is not None else ""
        if len(ex) >= 3 and any(ex in t for t in success_text):
            issues.append(LintIssue(code="L006", message=f"success_checkpoint embeds example value of input {p.name!r} ({ex!r}); use {{{{inputs.{p.name}}}}} or a value-independent condition", path="$.success_checkpoint"))

    # L007 no value-as-locator
    known = {v for v in (str(x) for x in known_values) if len(v) >= 3}
    for p in cap.inputs:
        if p.example is not None and len(str(p.example)) >= 3:
            known.add(str(p.example))
    for path, ident in _strategy_identifiers(cap):
        if ident in known and "{{" not in ident:
            issues.append(LintIssue(code="L007", message=f"strategy identifies the control by a data value ({ident!r}); it will target a different element for the next record", path=path))

    # L008 only a root checkpoint may poll; outcome detectors and recovery triggers are per-tick
    def check_nested(root: Checkpoint, where: str, root_may_wait: bool):
        for i, cp in enumerate(iter_checkpoints(root)):
            if i == 0 and root_may_wait:
                continue
            if cp.wait_ms:
                issues.append(LintIssue(code="L008", message=f"{where}: nested/per-tick checkpoint has wait_ms={cp.wait_ms}; only a root postcondition/precondition/success checkpoint may poll", path=where))
    for s in cap.steps:
        for j, pc in enumerate(s.preconditions):
            check_nested(pc, f"$.steps[{s.id}].preconditions[{j}]", True)
        if s.postcondition is not None:
            check_nested(s.postcondition, f"$.steps[{s.id}].postcondition", True)
        a = s.action
        if a.type in ("wait", "assert"):
            check_nested(a.checkpoint, f"$.steps[{s.id}].action.checkpoint", True)
    check_nested(cap.success_checkpoint, "$.success_checkpoint", True)
    for o in cap.outcomes:
        check_nested(o.detect, f"$.outcomes[{o.code}].detect", False)
    for r in cap.recoveries:
        check_nested(r.when, f"$.recoveries[{r.id}].when", False)

    # L009 step risk within the artifact's own declared ceiling
    ceiling = RISK_RANK[cap.policy_declaration.max_risk]
    for s in cap.steps:
        if RISK_RANK[s.risk] > ceiling:
            issues.append(LintIssue(code="L009", message=f"step {s.id} risk {s.risk!r} exceeds policy_declaration.max_risk {cap.policy_declaration.max_risk!r}", path=f"$.steps[{s.id}]"))

    # L012 every declared input must actually be used. A required field nothing references is
    # dead weight in the agent-facing contract: the caller must supply it and it changes nothing.
    used = {name for kind, name in refs if kind == "inputs"}
    for p in cap.inputs:
        if p.name not in used:
            issues.append(LintIssue(code="L012", message=f"input {p.name!r} is declared but never referenced by any step, checkpoint or output", path="$.inputs"))

    # L011 a numeric output needs a transform that can actually produce a number. Caught here
    # because it type-checks perfectly at record time and then fails on every single replay.
    for o in cap.outputs:
        if o.type in ("currency", "number") and o.source.transform not in ("currency_to_number", "digits_only"):
            issues.append(LintIssue(code="L011", message=f"output {o.name!r} is a {o.type} but transform is {o.source.transform!r}, which cannot produce a number", path=f"$.outputs[{o.name}]"))

    # L010 an outcome detector must not be satisfiable by the success state alone: we cannot
    # evaluate it here, but we can catch the trivial case of an empty/always-true detector.
    for o in cap.outcomes:
        if _trivially_true(o.detect):
            issues.append(LintIssue(code="L010", message=f"outcome {o.code} detector is trivially true and would shadow success", path=f"$.outcomes[{o.code}]"))

    return issues


class LintError(ValueError):
    def __init__(self, issues: list[LintIssue]):
        self.issues = issues
        super().__init__("; ".join(f"{i.code}: {i.message}" for i in issues))


def assert_lints_clean(cap: Capability, known_values: Iterable[str] = ()) -> None:
    issues = lint_capability(cap, known_values)
    if issues:
        raise LintError(issues)
