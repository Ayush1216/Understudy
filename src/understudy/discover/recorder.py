"""Compile a successful discovery trace into a capability artifact.

Models reliably over-fit a recording to the one record they saw, so the recorder overrules the
model mechanically and logs every rewrite as `recorder.overruled`: literal input values become
`{{inputs.x}}`, a strategy that locates a control by a data value is dropped, a postcondition or
success text that embeds an extracted value becomes control_present on the extraction target,
and a declared outcome already true on the successful final screen is dropped (replay checks
outcomes before success, so it would shadow success forever).

Recoveries are deliberately NOT synthesized. Which obstruction a flow meets and how to repair
it is a curation step: a human adds `recoveries` to the draft and the same lint re-checks it.
Guessing them from one run bakes in whatever that run happened to hit; shipping built-in
defaults is worse still, because they are one target's error wording applied to every app.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from understudy.evidence import RunLogger
from understudy.schema import (
    POSITIONAL_KINDS,
    RISK_RANK,
    Action,
    All,
    BusinessOutcome,
    Capability,
    Checkpoint,
    ControlPresent,
    ExtractAction,
    LintError,
    LintIssue,
    OutputSpec,
    ParamSpec,
    PolicyDeclaration,
    Provenance,
    Risk,
    Step,
    TargetDescriptor,
    TargetSpec,
    TextAbsent,
    TextPresent,
    Viewport,
)
from understudy.schema.lint import TEMPLATE_REF

if TYPE_CHECKING:
    from .loop import DiscoveryOptions

ACTING = frozenset({"click", "type_text", "select_option", "press_key", "navigate"})
_IDENT_KEYS = ("name", "label", "text", "row_anchor", "value", "column")


@dataclass
class TraceEntry:
    """One traced tool call. `action` is the template form: {{secrets.X}} is never resolved here."""

    seq: int
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    why: str = ""
    target: TargetDescriptor | None = None
    action: Action | None = None
    risk: Risk = "safe"
    url_before: str = ""
    url_after: str = ""
    navigated: bool = False
    text_before: str = ""
    text_after: str = ""
    sig_before: str = ""
    sig_after: str = ""


@dataclass
class Finish:
    summary: str
    success_text: str
    visible_text: str  # the whole final screen: what a declared outcome must not already match


def derive_name(goal: str) -> str:
    name = ".".join(re.findall(r"[a-z0-9]+", goal.lower())[:5]) or "capability"
    return name if name[0].isalpha() else "cap." + name


def _idents(strategy: Any) -> set[str]:
    return {v for k in _IDENT_KEYS if isinstance(v := getattr(strategy, k, None), str) and v}


def _looks_like_id(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9-]*\d[A-Za-z0-9-]*", value) is not None


def _embeds(text: str, outputs: list[tuple[OutputSpec, str]]) -> TargetDescriptor | None:
    for spec, value in outputs:
        if len(value) >= 3 and value in text and spec.source.target is not None:
            return spec.source.target
    return None


def _first_new_line(entry: TraceEntry, pinned: list[str]) -> str | None:
    before = {line.strip() for line in entry.text_before.splitlines()}
    low = [p.lower() for p in pinned]
    for raw in entry.text_after.splitlines():
        line = raw.strip()
        if len(line) < 4 or line in before or any(p in line.lower() for p in low):
            continue
        return line
    return None


def compile(
    trace: list[TraceEntry],
    *,
    declared_inputs: list[ParamSpec],
    declared_outputs: list[tuple[OutputSpec, str]],
    declared_outcomes: list[BusinessOutcome],
    finish: Finish,
    options: DiscoveryOptions,
    provenance: Provenance,
    logger: RunLogger,
) -> Capability:
    def overrule(what: str, why: str) -> None:
        logger.emit("recorder.overruled", what=what, why=why)

    inputs = list(declared_inputs)
    for name, value in options.inputs.items():
        if not any(p.name == name for p in inputs):
            inputs.append(ParamSpec(
                name=name, type="string", description=f"{name} (supplied at discovery, never declared by the model)",
                sensitivity="pii" if _looks_like_id(value) else "public", example=value,
            ))
    # (value, input name): supplied values and declared examples both become {{inputs.name}}.
    subs = [(v, n) for n, v in options.inputs.items()]
    subs += [(str(p.example), p.name) for p in inputs if p.example is not None and len(str(p.example)) >= 3]
    subs.sort(key=lambda s: -len(s[0]))
    extracted = [v for _, v in declared_outputs if len(v) >= 3]
    secrets = [v for n in options.secret_names if (v := os.environ.get(n))]
    pinned = [v for v, _ in subs] + extracted + secrets

    def param(text: str, where: str) -> str:
        for value, name in subs:
            template = "{{inputs.%s}}" % name
            if text == value:
                out = template
            elif len(value) >= 3 and value in text:
                out = text.replace(value, template)
            else:
                continue
            overrule(where, f"literal {value!r} replaced by {template}")
            text = out
        return text

    def scrub(td: TargetDescriptor, where: str) -> TargetDescriptor:
        kept = [s for s in td.strategies if not (_idents(s) & set(pinned))]
        if len(kept) == len(td.strategies):
            return td
        for s in td.strategies:
            if s not in kept:
                overrule(f"{where}.strategies[{s.kind}]",
                         f"identifies the control by a data value {sorted(_idents(s) & set(pinned))}; it would target a different element for the next record")
        if td.intent == "read" and all(s.kind in POSITIONAL_KINDS for s in kept):
            raise LintError([LintIssue(
                code="L007", path=where,
                message=f"{where}: the only semantic anchor for this read target is a data value; re-record by picking the cell next to its label or in a table with a header row",
            )])
        if not kept:
            kept = [s for s in td.strategies if s.origin == "derived"]
        if not kept:
            raise LintError([LintIssue(code="L007", path=where, message=f"{where}: no strategy survives dropping data values")])
        return td.model_copy(update={"strategies": kept})

    def param_action(action: Action, where: str) -> Action:
        if action.type == "navigate":
            return action.model_copy(update={"url": param(action.url, f"{where}.url")})
        update: dict[str, Any] = {}
        if getattr(action, "target", None) is not None:
            update["target"] = scrub(action.target, f"{where}.target")
        if action.type in ("type", "select"):
            update["value"] = param(action.value, f"{where}.value")
        return action.model_copy(update=update) if update else action

    def as_checkpoint(cps: list[Checkpoint], wait_ms: int) -> Checkpoint:
        if len(cps) == 1:
            return cps[0].model_copy(update={"wait_ms": wait_ms})
        return All(kind="all", of=cps, wait_ms=wait_ms)

    # ---- dead ends: an action immediately undone by navigating back drops both ---------------
    producing = [e for e in trace if e.tool in ACTING or e.tool == "extract"]
    dead: set[int] = set()
    for a, b in zip(producing, producing[1:]):
        if a.tool in ACTING and b.tool == "navigate" and b.action is not None and b.action.url == a.url_before:
            dead |= {a.seq, b.seq}
            overrule(f"trace[{a.seq},{b.seq}]", f"dead end: {a.tool} then navigate back to {a.url_before}")

    # ---- asserts attach to the entry before them (postcondition) or, before any, to step 1 ---
    post: dict[int, list[Checkpoint]] = {}
    pre: list[Checkpoint] = []
    last: int | None = None
    for e in trace:
        if e.tool in ACTING or e.tool == "extract":
            last = e.seq
        elif e.tool == "assert_checkpoint":
            text = param(str(e.args.get("text", "")), f"asserts[{e.seq}].text")
            cp = TextAbsent(kind="text_absent", text=text) if e.args.get("kind") == "text_absent" else TextPresent(kind="text_present", text=text)
            (pre if last is None else post.setdefault(last, [])).append(cp)

    # ---- steps ---------------------------------------------------------------------------------
    steps: list[Step] = []
    live = [e for e in producing if e.seq not in dead]
    i = 0
    while i < len(live):
        e = live[i]
        sid = f"s{len(steps) + 1}"
        where = f"steps[{sid}]"
        cps = post.get(e.seq, [])
        if e.tool == "extract":
            names = [e.args["output_name"]]
            while i + 1 < len(live) and live[i + 1].tool == "extract" and not cps:
                i += 1
                if live[i].args["output_name"] not in names:
                    names.append(live[i].args["output_name"])
                cps = post.get(live[i].seq, [])
            action: Action = ExtractAction(type="extract", outputs=names)
            postcondition = as_checkpoint(cps, 8000) if cps else None
        else:
            assert e.action is not None
            action = param_action(e.action, where)
            if cps:
                postcondition = as_checkpoint(cps, 8000)
            elif e.navigated:
                line = _first_new_line(e, pinned)
                postcondition = TextPresent(kind="text_present", text=line, wait_ms=8000) if line else None
                if line:
                    logger.emit("recorder.synthesized", what=f"{where}.postcondition", why=f"no assert after a navigation; first new screen line {line!r}")
            elif action.type in ("type", "select"):
                postcondition = ControlPresent(kind="control_present", target=action.target, wait_ms=2000)
            else:
                postcondition = None
        if isinstance(postcondition, TextPresent) and (target := _embeds(postcondition.text, declared_outputs)):
            overrule(f"{where}.postcondition", "asserted text embeds an extracted value; asserting extracted data over-fits to the record it was recorded on")
            postcondition = ControlPresent(kind="control_present", target=target, wait_ms=postcondition.wait_ms)
        steps.append(Step(
            id=sid, description=param(e.why or f"{e.tool} {e.target.label if e.target else ''}".strip(), f"{where}.description"),
            action=action, risk=e.risk, expects_navigation=e.navigated,
            preconditions=[cp.model_copy(update={"wait_ms": 8000}) for cp in pre] if not steps else [],
            postcondition=postcondition,
        ))
        i += 1

    outputs = [
        spec.model_copy(update={"source": spec.source.model_copy(update={"target": scrub(spec.source.target, f"outputs[{spec.name}]")})})
        if spec.source.target is not None else spec
        for spec, _ in declared_outputs
    ]

    outcomes: list[BusinessOutcome] = []
    for o in declared_outcomes:
        if isinstance(o.detect, TextPresent) and o.detect.text in finish.visible_text:
            overrule(f"outcomes[{o.code}]", f"detector text {o.detect.text!r} is present on the successful final screen; it would permanently shadow success")
            continue
        if isinstance(o.detect, TextPresent):
            o = o.model_copy(update={"detect": o.detect.model_copy(update={"text": param(o.detect.text, f"outcomes[{o.code}].detect.text")})})
        outcomes.append(o)

    success: Checkpoint
    if _embeds(finish.success_text, declared_outputs) is not None:
        overrule("success_checkpoint", "success_text embeds an extracted value; green once, useless after")
        success = ControlPresent(kind="control_present", target=declared_outputs[-1][0].source.target, wait_ms=5000)
    else:
        success = TextPresent(kind="text_present", text=param(finish.success_text, "success_checkpoint"), wait_ms=5000)

    secrets_required = sorted({name for kind, name in TEMPLATE_REF.findall(json.dumps([s.model_dump(mode="json") for s in steps])) if kind == "secrets"})
    parts = urlsplit(options.entry_url)
    tenant = re.match(r"^/t/([^/]+)/", parts.path)
    name = options.name or derive_name(options.goal)
    return Capability(
        id="cap_" + name.replace(".", "_"), name=name, version="1.0.0",
        title=options.goal, description=finish.summary or options.goal, approval="draft",
        target=TargetSpec(
            surface="legacy_web", app=options.app, app_version=options.app_version, tenant=options.tenant,
            entry_url=param(options.entry_url, "target.entry_url"), viewport=Viewport(),
        ),
        inputs=inputs, outputs=outputs, secrets_required=secrets_required, steps=steps, outcomes=outcomes,
        success_checkpoint=success,
        policy_declaration=PolicyDeclaration(
            origins=[f"{parts.scheme}://{parts.netloc}"],
            path_patterns=[f"/t/{tenant.group(1)}/**"] if tenant else ["/**"],
            max_risk=max((s.risk for s in steps), key=lambda r: RISK_RANK[r], default="safe"),
            require_approval_for=["irreversible"],
        ),
        provenance=provenance,
    )
