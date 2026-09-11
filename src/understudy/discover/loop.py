"""The discovery loop: observe → decide → act, one tool call executed per model turn, until the
model finishes on a screen that shows its success text, asks for a human, or stalls.

Every proposed action passes the policy gate in discovery mode (risky actions hard-deny) before
it is executed, and a refused or failed action is never written to the trace, so it cannot be
recorded into an artifact. The model never authors a locator: it names a ref from the latest
observation and perception emits the descriptor.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import ValidationError

from understudy.evidence import EvidenceDir, RunLogger, new_intervention_id, new_run_id
from understudy.policy import PolicyEngine, Redactor, classify_risk, effective_policy, load_operator_policy
from understudy.runs import Escalator
from understudy.schema import (
    Action,
    BusinessOutcome,
    Capability,
    ClickAction,
    InterventionContext,
    InterventionReason,
    InterventionRequest,
    NavigateAction,
    Observation,
    OutputSource,
    OutputSpec,
    ParamSpec,
    PolicyDeclaration,
    PressAction,
    Provenance,
    Risk,
    SelectAction,
    TextPresent,
    TypeAction,
    assert_lints_clean,
)
from understudy.schema.lint import TEMPLATE_REF
from understudy.surface import ActContext, PolicyBlocked, SurfaceError
from understudy.surface.web import BrowserSession, WebSurface

from .client import ModelClient, ModelTurn, OpenAICompatClient, ToolCall
from .prompt import build_system, render_observation
from .recorder import Finish, TraceEntry, compile
from .tools import TOOLS

_ACTING: dict[str, str] = {"click": "click", "type_text": "type", "select_option": "select", "press_key": "press", "navigate": "navigate"}
_REFUSED = "Not executed: actions are applied one at a time because each one can change the page. Observe and decide again."
_UNKNOWN_REF = "Unknown ref {ref}; re-observe and choose a current ref. Do not guess."
_NO_ANCHOR = "no semantic anchor for that cell; pick the cell next to its label or in a table with a header row"
_RESUMED = "A human operator intervened and handed control back. Here is the current screen."


class _ToolError(Exception):
    """Fed back to the model as an error tool result; never traced."""


@dataclass
class DiscoveryOptions:
    goal: str
    entry_url: str
    inputs: dict[str, str] = field(default_factory=dict)
    # Environment variable NAMES whose values the model may type as {{secrets.NAME}}.
    secret_names: list[str] = field(default_factory=list)
    name: str | None = None
    tenant: str | None = None
    app: str = "target"
    app_version: str | None = None
    headless: bool = True
    operator_policy_path: Path = Path("config/policy.toml")
    evidence_root: Path = Path("evidence")
    capabilities_dir: Path = Path("capabilities")
    max_steps: int = 30
    # Discovery has its own clock, longer than replay's. None = the operator policy's.
    timeout_s: float | None = None
    max_risk: Risk = "safe"
    echo: bool = False
    model: ModelClient | None = None


@dataclass
class DiscoveryResult:
    status: Literal["completed", "stopped", "escalated"]
    reason: str
    capability: Capability | None
    capability_path: Path | None
    llm_steps: int
    run_id: str
    evidence_dir: Path


class DiscoveryRun:
    """Satisfies runs.RunHandle once prepare() has run."""

    kind: Literal["discover", "replay"] = "discover"

    def __init__(self, options: DiscoveryOptions, *, escalator: Escalator | None = None) -> None:
        self.options = options
        self.escalator = escalator
        self.run_id = new_run_id("discover")
        self.label = options.goal
        self.llm_steps = 0
        self.trace: list[TraceEntry] = []
        self.inputs: dict[str, ParamSpec] = {}
        self.outputs: dict[str, tuple[OutputSpec, str]] = {}
        self.outcomes: dict[str, BusinessOutcome] = {}
        self.messages: list[dict[str, Any]] = []
        self.obs: Observation | None = None
        self._pending_shot: str | None = None
        self._stalled = 0
        self._deadline = 0.0

    # ---- lifecycle -----------------------------------------------------------------------------

    async def prepare(self) -> None:
        o = self.options
        parts = urlsplit(o.entry_url)
        declaration = PolicyDeclaration(origins=[f"{parts.scheme}://{parts.netloc}"], max_risk=o.max_risk)
        self.policy = PolicyEngine(effective_policy(load_operator_policy(o.operator_policy_path), declaration))
        self.max_steps = min(o.max_steps, self.policy.effective.max_steps)
        self.timeout_s = o.timeout_s if o.timeout_s is not None else self.policy.effective.discovery_timeout_ms / 1000
        self.model = o.model or OpenAICompatClient.from_env()  # a missing key fails before a browser opens
        self.redactor = Redactor()
        for name in o.secret_names:
            if value := os.environ.get(name):
                self.redactor.register_secret(value)
        for value in o.inputs.values():
            self.redactor.register_pii(value)
        self.evidence = EvidenceDir(o.evidence_root, self.run_id, self.redactor)
        self.logger = RunLogger(self.run_id, self.evidence.log_path, self.redactor, echo=o.echo)
        self.session = await BrowserSession.launch(
            headless=o.headless, policy_gate=self.policy,
            on_violation=lambda url, reason: self.logger.emit("policy.decision", action_type="navigate", url=url, decision="deny", reason=reason),
        )
        self.surface = WebSurface(self.session, policy_gate=self.policy)

    async def close(self) -> None:
        await self.session.close()

    async def execute(self) -> DiscoveryResult:
        o = self.options
        clock = asyncio.get_running_loop()
        self._deadline = clock.time() + self.timeout_s
        self.logger.emit("run.start", kind="discover", goal=o.goal, entry_url=o.entry_url, inputs=sorted(o.inputs),
                         secrets=o.secret_names, model=self._model_name(), max_steps=self.max_steps, timeout_s=self.timeout_s)
        system = build_system(o.goal, o.entry_url, o.inputs, o.secret_names, self.max_steps)
        self.evidence.write_transcript_line({"system": system, "tools": [t["function"]["name"] for t in TOOLS]})
        try:
            await self.surface.act(NavigateAction(type="navigate", url=o.entry_url), ActContext(mode="discovery"))
            await self._snap("entry")
            obs = await self._observe(include_screenshot=True)
            self._push_user(render_observation(obs))
            budget = self.max_steps
            idle = 0
            written = 0
            while True:
                if clock.time() > self._deadline:
                    return self._end("stopped", f"discovery clock of {self.timeout_s:.0f}s exceeded after {self.llm_steps} turns")
                if budget <= 0:
                    if ended := await self._escalate("MAX_STEPS", f"{self.llm_steps} model turns without finishing"):
                        return ended
                    budget = self.max_steps
                    continue
                turn = await self.model.complete(system=system, messages=self.messages, tools=TOOLS)
                self.llm_steps += 1
                budget -= 1
                self.evidence.write_transcript_line({
                    "turn": self.llm_steps, "request": _without_images(self.messages[written:]),
                    "response": {"text": turn.text, "tool_calls": [vars(tc) for tc in turn.tool_calls],
                                 "usage": {"input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens}, "raw": turn.raw},
                })
                self.messages.append(_assistant(turn))
                written = len(self.messages)
                if not turn.tool_calls:
                    idle += 1
                    if idle >= 2:
                        if ended := await self._escalate("AGENT_REQUESTED", "model stopped calling tools"):
                            return ended
                        idle = 0
                        continue
                    self.messages.append({"role": "user", "content": "Reply with exactly one tool call."})
                    continue
                idle = 0
                first, *rest = turn.tool_calls
                content, signal = await self._call(first)
                self.messages.append({"role": "tool", "tool_call_id": first.id, "content": content})
                for tc in rest:
                    self.messages.append({"role": "tool", "tool_call_id": tc.id, "content": "ERROR: " + _REFUSED})
                if signal == "finish":
                    return await self._complete(first.arguments)
                if signal == "help":
                    detail = f"{first.arguments.get('reason', '')} (was trying: {first.arguments.get('what_i_was_trying', '')})"
                    if ended := await self._escalate("AGENT_REQUESTED", detail):
                        return ended
                elif self._stalled >= 3:
                    if ended := await self._escalate("NO_PROGRESS", f"three actions in a row changed nothing on {self.obs.url}"):
                        return ended
                    self._stalled = 0
                else:
                    self._push_screenshot()
                    continue
                self._push_user(_RESUMED + "\n\n" + render_observation(await self._observe(include_screenshot=True)))
        except SurfaceError as e:
            return self._end("stopped", f"{e.error_class}: {e.message}")
        except Exception as e:  # noqa: BLE001 — evidence must be complete even when the run crashes
            self._end("stopped", f"{type(e).__name__}: {e}")
            raise

    # ---- tools ---------------------------------------------------------------------------------

    async def _call(self, tc: ToolCall) -> tuple[str, str | None]:
        args = tc.arguments
        try:
            if "_parse_error" in args:
                raise _ToolError("arguments were not valid JSON; send a JSON object")
            if tc.name in _ACTING:
                return await self._act(tc.name, args), None
            if tc.name == "observe":
                return render_observation(await self._observe(include_screenshot=bool(args.get("include_screenshot")))), None
            if tc.name == "extract":
                return await self._extract(args), None
            if tc.name == "declare_input":
                return self._declare_input(args), None
            if tc.name == "declare_outcome":
                return self._declare_outcome(args), None
            if tc.name == "assert_checkpoint":
                return await self._assert(args), None
            if tc.name == "finish":
                if _need(args, "success_text") not in await self.surface.visible_text():
                    raise _ToolError("success_text is not visible; you cannot finish on a screen that does not show it")
                return "Finished.", "finish"
            if tc.name == "request_human_help":
                return "Escalated to a human operator.", "help"
            raise _ToolError(f"unknown tool {tc.name!r}")
        except _ToolError as e:
            self.logger.emit("tool.call", tool=tc.name, ok=False, error=str(e))
            return f"ERROR: {e}", None

    async def _act(self, tool: str, args: dict[str, Any]) -> str:
        obs = self.obs
        action_type = _ACTING[tool]
        ref = args.get("ref")
        control = target = None
        if action_type != "navigate" and (action_type != "press" or ref):
            control = obs.control(ref) if isinstance(ref, str) else None
            if control is None:
                raise _ToolError(_UNKNOWN_REF.format(ref=ref))
            try:
                target = await self.surface.capture_descriptor(ref, obs, intent="act")
            except LookupError:
                raise _ToolError(_UNKNOWN_REF.format(ref=ref))
        template, action = self._actions(action_type, args, target)
        # Submit buttons are classified by NAME, so sign-on and search stay safe while confirm/post/transfer do not.
        risk = classify_risk(action_type=action_type, control_name=control.display_name() if control else "", is_form_submit=False)
        url_before = await self.surface.url()
        decision = self.policy.check(
            action_type=action_type, url=action.url if action.type == "navigate" else url_before, risk=risk, mode="discovery",
        )
        self.logger.emit("policy.decision", tool=tool, action_type=action_type, risk=risk, decision=decision.decision, reason=decision.reason)
        if decision.decision != "allow":
            raise _ToolError(f"Refused by policy: {decision.reason}")
        text_before = await self.surface.visible_text()
        try:
            result = await self.surface.act(action, ActContext(risk=risk, mode="discovery"))
        except PolicyBlocked as e:
            raise _ToolError(f"Refused by policy: {e.reason}")
        except SurfaceError as e:
            raise _ToolError(f"{e.message}; expected {e.expected}; observed {e.observed}")
        # ponytail: a fixed settle. A click that starts a child-frame navigation may not have
        # committed when act() returns; upgrade to a bounded framenavigated wait if this flakes.
        await asyncio.sleep(0.3)
        after = await self._observe(include_screenshot=self._stalled > 0)
        navigated = result.navigated or after.url != url_before
        self._stalled = self._stalled + 1 if _progress_key(after) == _progress_key(obs) else 0
        entry = TraceEntry(
            seq=len(self.trace) + 1, tool=tool, args=dict(args), why=str(args.get("why", "")), target=target,
            action=template, risk=risk, url_before=url_before, url_after=after.url, navigated=navigated,
            text_before=text_before, text_after=await self.surface.visible_text(),
            sig_before=obs.signature(), sig_after=after.signature(),
        )
        self.trace.append(entry)
        self.logger.emit("action.performed", trace_seq=entry.seq, tool=tool, action=template, risk=risk, navigated=navigated,
                         url_after=after.url, resolved_by=result.resolved.won if result.resolved else None)
        await self._snap(entry.why or tool)
        return f"OK: {tool} done; {'navigated to ' + after.url if navigated else 'no navigation'}.\n\n" + render_observation(after)

    def _actions(self, action_type: str, args: dict[str, Any], target) -> tuple[Action, Action]:
        """(template form for the trace, resolved form for the surface)."""
        if action_type == "navigate":
            url = _need(args, "url")
            return NavigateAction(type="navigate", url=url), NavigateAction(type="navigate", url=self._resolve(url))
        if action_type == "click":
            a = ClickAction(type="click", target=target)
            return a, a
        if action_type == "type":
            text = _need(args, "text")
            value = self._resolve(text)
            if value != text:  # a credential typed into a plain textbox is masked in screenshots
                self.surface.register_sensitive_target(target)
            return TypeAction(type="type", target=target, value=text), TypeAction(type="type", target=target, value=value)
        if action_type == "select":
            value = _need(args, "value")
            return SelectAction(type="select", target=target, value=value), SelectAction(type="select", target=target, value=self._resolve(value))
        a = PressAction(type="press", key=_need(args, "key"), target=target)
        return a, a

    def _resolve(self, template: str) -> str:
        """{{secrets.X}} from the environment at act time (only listed names); {{inputs.x}} from
        the supplied inputs. The trace keeps the template; the value never reaches a file."""
        o = self.options

        def sub(m):
            kind, name = m.group(1), m.group(2)
            if kind == "inputs":
                if name not in o.inputs:
                    raise _ToolError(f"{{{{inputs.{name}}}}} is not a supplied input; type the value literally")
                return o.inputs[name]
            if name not in o.secret_names:
                raise _ToolError(f"{{{{secrets.{name}}}}} is not an available credential; available: {', '.join(o.secret_names) or 'none'}")
            if not (value := os.environ.get(name)):
                raise _ToolError(f"credential {name} is not set in the environment; request human help")
            return value

        return TEMPLATE_REF.sub(sub, template)

    async def _extract(self, args: dict[str, Any]) -> str:
        ref, name = _need(args, "ref"), _need(args, "output_name")
        try:
            target = await self.surface.capture_descriptor(ref, self.obs, intent="read")
        except LookupError as e:
            raise _ToolError(_UNKNOWN_REF.format(ref=ref) if "re-observe" in str(e) else _NO_ANCHOR)
        kind = args.get("type", "string")
        try:
            spec = OutputSpec(
                name=name, type=kind, description=str(args.get("description", "")), sensitivity=args.get("sensitivity") or "public",
                source=OutputSource(kind="text_of", target=target, transform=args.get("transform") or ("currency_to_number" if kind == "currency" else "trim")),
            )
        except ValidationError as e:
            raise _ToolError(f"invalid output: {_errors(e)}")
        try:
            value = (await self.surface.read_text(target)) or ""
        except SurfaceError as e:
            raise _ToolError(e.message)
        if spec.sensitivity == "secret":
            self.redactor.register_secret(value)
            self.surface.register_sensitive_target(target)
        elif spec.sensitivity == "pii":
            self.redactor.register_pii(value)
        self.outputs[name] = (spec, value)
        self.trace.append(TraceEntry(seq=len(self.trace) + 1, tool="extract", args=dict(args), why=str(args.get("why", "")), target=target))
        self.logger.emit("tool.call", tool="extract", ok=True, output=name, value=value, resolved_by=target.strategies[0].kind)
        return f"OK: {name} = {value!r} (read by {target.strategies[0].kind}: {target.label or target.role})"

    def _declare_input(self, args: dict[str, Any]) -> str:
        fields = {k: v for k, v in args.items() if k in ParamSpec.model_fields}
        if fields.get("example") is not None:
            fields["example"] = str(fields["example"])
        try:
            spec = ParamSpec(**fields)
        except ValidationError as e:
            raise _ToolError(f"invalid input declaration: {_errors(e)}")
        if spec.example is None and spec.name in self.options.inputs:
            spec = spec.model_copy(update={"example": self.options.inputs[spec.name]})
        self.inputs[spec.name] = spec
        self.logger.emit("tool.call", tool="declare_input", ok=True, name=spec.name)
        return f"OK: input {spec.name} declared."

    def _declare_outcome(self, args: dict[str, Any]) -> str:
        try:
            outcome = BusinessOutcome(
                code=_need(args, "code"), description=str(args.get("description", "")),
                detect=TextPresent(kind="text_present", text=_need(args, "detect_text")),
            )
        except ValidationError as e:
            raise _ToolError(f"invalid outcome: {_errors(e)}")
        self.outcomes[outcome.code] = outcome
        self.logger.emit("tool.call", tool="declare_outcome", ok=True, code=outcome.code)
        return f"OK: outcome {outcome.code} declared; detected by {outcome.detect.text!r}."

    async def _assert(self, args: dict[str, Any]) -> str:
        kind, text = args.get("kind"), _need(args, "text")
        if kind not in ("text_present", "text_absent"):
            raise _ToolError("kind must be text_present or text_absent")
        holds = (text in await self.surface.visible_text()) == (kind == "text_present")
        self.logger.emit("checkpoint.evaluated", kind=kind, text=text, holds=holds)
        if not holds:
            raise _ToolError("that text is not on the screen; assert only what is visible" if kind == "text_present"
                             else "that text is on the screen; text_absent must hold now")
        self.trace.append(TraceEntry(seq=len(self.trace) + 1, tool="assert_checkpoint", args={"kind": kind, "text": text}, why=str(args.get("why", ""))))
        return f"OK: {kind} {text!r} holds now; recorded as the postcondition of the previous action."

    # ---- endings -------------------------------------------------------------------------------

    async def _complete(self, args: dict[str, Any]) -> DiscoveryResult:
        o = self.options
        finish = Finish(summary=str(args.get("summary", "")), success_text=str(args["success_text"]), visible_text=await self.surface.visible_text())
        await self._snap("success")
        provenance = Provenance(
            recorded_at=datetime.now(timezone.utc), goal=o.goal, model=self._model_name(), discovery_run_id=self.run_id,
            surface_signature={"browser": "chromium", "surface": "legacy_web"}, llm_step_count=self.llm_steps,
        )
        try:
            cap = compile(
                self.trace, declared_inputs=list(self.inputs.values()), declared_outputs=list(self.outputs.values()),
                declared_outcomes=list(self.outcomes.values()), finish=finish, options=o, provenance=provenance, logger=self.logger,
            )
            assert_lints_clean(cap, known_values=[*o.inputs.values(), *(v for _, v in self.outputs.values())])
        except ValueError as e:  # LintError and pydantic ValidationError; the raw trace is still written
            return self._end("stopped", f"the recording did not compile into a valid capability: {e}")
        o.capabilities_dir.mkdir(parents=True, exist_ok=True)
        path = o.capabilities_dir / f"{cap.name}.v{cap.version}.json"
        path.write_text(cap.model_dump_json(indent=2) + "\n", encoding="utf-8")
        self.evidence.write_capability(cap)
        return self._end("completed", f"capability written to {path}", capability=cap, capability_path=path)

    async def _escalate(self, reason: InterventionReason, detail: str) -> DiscoveryResult | None:
        """None = a human resumed the run; otherwise the result that ends it."""
        shot = await self._snap("escalation")
        dom = self.evidence.write_text(self.evidence.dom_path("escalation"), await self.surface.dom_snapshot())
        request = InterventionRequest(
            id=new_intervention_id(), created_at=datetime.now(timezone.utc), origin="discovery", run_id=self.run_id,
            goal=self.options.goal, reason=reason, detail=detail,
            context=InterventionContext(
                url=await self.surface.url(), title=await self.surface.title(), screenshot_path=self.evidence.relative(shot),
                dom_path=self.evidence.relative(dom), recent_events=self.logger.tail(20),
            ),
        )
        self.logger.emit("escalation.raised", intervention_id=request.id, reason=reason, detail=detail)
        if self.escalator is None:
            return self._end("escalated", f"{reason}: {detail}")
        parked = asyncio.get_running_loop().time()
        resolved = await self.escalator.raise_intervention(request)
        self._deadline += asyncio.get_running_loop().time() - parked  # time on a human is not the run's
        decision = resolved.resolution.decision if resolved.resolution else "abort"
        self.logger.emit("human.resolved", intervention_id=request.id, decision=decision, human_actions=len(resolved.human_actions))
        if decision == "abort":
            return self._end("stopped", f"aborted by the operator after {reason}: {detail}")
        return None

    def _end(self, status: Literal["completed", "stopped", "escalated"], reason: str, *,
             capability: Capability | None = None, capability_path: Path | None = None) -> DiscoveryResult:
        self.evidence.write_json("discovery/trace.json", self.trace)
        self.logger.emit("run.end", status=status, reason=reason, llm_steps=self.llm_steps,
                         capability=str(capability_path) if capability_path else None)
        return DiscoveryResult(status=status, reason=reason, capability=capability, capability_path=capability_path,
                               llm_steps=self.llm_steps, run_id=self.run_id, evidence_dir=self.evidence.run_dir)

    # ---- helpers -------------------------------------------------------------------------------

    async def _observe(self, *, include_screenshot: bool = False) -> Observation:
        """The one observe path: refs are valid only for the observation held in self.obs."""
        self.obs = await self.surface.observe(include_screenshot=include_screenshot)
        self._pending_shot = self.obs.screenshot_b64
        if notify := getattr(self.model, "observed", None):
            notify(self.obs)
        return self.obs

    async def _snap(self, label: str) -> Path:
        return self.evidence.write_bytes(self.evidence.screenshot_path(label), await self.surface.screenshot(mask_sensitive=True))

    def _push_user(self, text: str) -> None:
        shot, self._pending_shot = self._pending_shot, None
        if shot:
            self.messages.append({"role": "user", "content": [
                {"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{shot}"}},
            ]})
        else:
            self.messages.append({"role": "user", "content": text})

    def _push_screenshot(self) -> None:
        if self._pending_shot:
            self._push_user("Screenshot of the current screen.")

    def _model_name(self) -> str:
        return str(getattr(self.model, "model", type(self.model).__name__))


def _need(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise _ToolError(f"missing argument {key!r}")
    return value


def _errors(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())


def _progress_key(obs: Observation) -> str:
    # Typing into a field is progress even though the control set is unchanged.
    return obs.signature() + "|" + ",".join(c.value or "" for c in obs.all_controls())


def _assistant(turn: ModelTurn) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": turn.text or ""}
    if turn.tool_calls:
        message["tool_calls"] = [
            {"id": tc.id, "type": "function", "function": {
                "name": tc.name,
                "arguments": tc.arguments["_parse_error"] if "_parse_error" in tc.arguments else json.dumps(tc.arguments),
            }}
            for tc in turn.tool_calls
        ]
    return message


def _without_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Transcript copy: screenshots live in screenshots/, not as base64 in every turn's request."""
    out = []
    for m in messages:
        if isinstance(m.get("content"), list):
            m = {**m, "content": [
                {"type": "image_url", "image_url": {"url": "<png omitted; see screenshots/>"}} if p.get("type") == "image_url" else p
                for p in m["content"]
            ]}
        out.append(m)
    return out
