"""The production execution path: an artifact and typed inputs, no model in the loop.

Every ending is classified. A declared business outcome is a first-class answer, a 5xx is a
surface error, a checkpoint that never materialized is a checkpoint failure carrying
expected/observed prose, and a step that cannot proceed safely parks the live session on a
human — who may finish the step, part of it, or nothing, so a resume re-evaluates rather than
continues.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from understudy.evidence import EvidenceDir, RunLogger, new_intervention_id, new_run_id, record_stability
from understudy.policy import PolicyEngine, Redactor, effective_policy, load_operator_policy
from understudy.runs import Escalator
from understudy.schema import (
    Action,
    Aliases,
    All,
    BusinessOutcome,
    BusinessOutcomeResult,
    Capability,
    CapabilityRef,
    Checkpoint,
    Decision,
    ErrorClass,
    EscalatedResult,
    FailedResult,
    InterventionContext,
    InterventionReason,
    InterventionRequest,
    LintError,
    NavigateAction,
    ParamSpec,
    RecoveryRule,
    RunError,
    RunResult,
    Step,
    SuccessResult,
    TargetDescriptor,
)
from understudy.surface import ActContext, SurfaceError
from understudy.surface.web import BrowserSession, WebSurface

from .checkpoints import TICK_S, TickView, describe, evaluate, head, poll
from .extract import OutputMissing, extract_outputs
from .template import MissingReference, references, render, render_model
from .tenant import resolve_for_tenant

# Failures a human cannot fix by taking the page: policy said no, someone else already holds
# the session, the browser is gone, or the artifact/inputs are wrong.
_NEVER_ESCALATE: frozenset[str] = frozenset({"POLICY_BLOCKED", "CONTROL_LOST", "INTERNAL", "INPUT_INVALID"})
_MAX_INTERVENTIONS_PER_STEP = 2


@dataclass
class ReplayOptions:
    inputs: dict[str, Any]
    tenant: str | None = None
    headless: bool = True
    allow_draft: bool = False
    operator_policy_path: Path = Path("config/policy.toml")
    evidence_root: Path = Path("evidence")
    echo: bool = False
    # Awaited once, before the step's first attempt; how the CLI's --inject arms a fault mid-flow.
    before_step: dict[str, Callable[[], Awaitable[None]]] = field(default_factory=dict)
    escalation_timeout_s: float = 600


class ReplayRun:
    """Satisfies runs.RunHandle. prepare() never raises for artifact or input problems: it records
    a pending FailedResult that execute() returns, and no browser is launched in that case."""

    kind: Literal["replay"] = "replay"

    def __init__(self, capability: Capability, options: ReplayOptions) -> None:
        self.capability = capability
        self.options = options
        self.run_id = new_run_id("replay")
        self.label = capability.key
        self.session: BrowserSession | None = None
        self.escalator: Escalator | None = None
        self.redactor = Redactor()
        self.evidence = EvidenceDir(options.evidence_root, self.run_id, self.redactor)
        self.logger = RunLogger(self.run_id, self.evidence.log_path, self.redactor, echo=options.echo)
        self.cap = capability  # tenant-resolved after prepare()
        self.aliases = Aliases()
        self.frame_map: dict[str, str] = {}
        self.engine: PolicyEngine | None = None
        self.surface: WebSurface | None = None
        self.inputs: dict[str, Any] = {}
        self.outputs: dict[str, Any] = {}
        self.human_wait_ms = 0
        self._pending: FailedResult | None = None
        self._t0 = monotonic()
        self._extracted: set[str] = set()
        self._rungs: dict[str, int] = {}
        self._attempts: dict[str, int] = {}
        self._recovery_attempts: dict[str, int] = {}
        self._applied: list[str] = []
        self._steps_executed = 0
        self._current: Step | None = None
        self._violation: dict[str, str] | None = None

    # ---- lifecycle ----------------------------------------------------------------------------

    async def prepare(self) -> None:
        cap, opts = self.capability, self.options
        for p in cap.inputs:  # masked before anything can be written, including a rejection
            if p.sensitivity != "public" and p.name in opts.inputs:
                self.redactor.register_pii(opts.inputs[p.name])
        if cap.approval == "draft" and not opts.allow_draft:
            return self._pend("an approved capability", "approval is 'draft'", "draft capability; pass --allow-draft or approve it")
        try:
            self.cap, self.aliases, self.frame_map = resolve_for_tenant(cap, opts.tenant)
        except LintError as e:
            return self._pend("a capability that passes lint", "; ".join(f"{i.code}: {i.message}" for i in e.issues), "capability failed lint")
        except ValueError as e:  # unknown tenant, or a merge that fails validation
            return self._pend("a valid tenant-resolved capability", str(e), "capability could not be resolved")
        self.inputs, issues = coerce_inputs(self.cap.inputs, opts.inputs)
        if issues:
            return self._pend(input_contract(self.cap.inputs), "; ".join(issues), f"{len(issues)} input problem(s)")
        missing = [n for n in self.cap.secrets_required if n not in os.environ]
        if missing:
            return self._pend(f"environment variables {self.cap.secrets_required}", f"not set: {missing}", f"missing secrets: {', '.join(missing)}")
        for n in self.cap.secrets_required:
            self.redactor.register_secret(os.environ[n])
        self.logger.emit("run.start", capability_id=self.cap.id, capability_key=self.cap.key, version=self.cap.version,
                         tenant=opts.tenant, inputs=self.inputs)
        try:
            self.engine = PolicyEngine(effective_policy(load_operator_policy(opts.operator_policy_path), self.cap.policy_declaration))
            self.session = await BrowserSession.launch(
                headless=opts.headless, viewport=self.cap.target.viewport, policy_gate=self.engine, on_violation=self._on_violation,
            )
            self.surface = WebSurface(self.session, policy_gate=self.engine)
            self.surface.set_tenant_vocabulary(self.aliases, self.frame_map)
            for td in sensitive_targets(self.cap):
                self.surface.register_sensitive_target(td)
            self.evidence.write_capability(self.cap)
        except Exception as e:  # noqa: BLE001 — a bad policy file or a browser that will not start
            await self.close()
            self.session = self.surface = None
            self._pend("the operator policy to load and a browser to launch", repr(e), f"could not prepare the run: {e!r}", kind="INTERNAL")

    async def execute(self) -> RunResult:
        if self._pending is None and self.surface is None:
            raise RuntimeError("prepare() before execute()")
        self._t0 = monotonic()
        result: RunResult
        if self._pending is not None:
            result = self._pending
        else:
            try:
                result = await self._run()
            except Exception as e:  # noqa: BLE001 — anything unclassified ends the run as INTERNAL, never as a traceback
                result = self._fail(self._error("INTERNAL", self._current, expected="the run to complete", observed=repr(e), message=f"internal error: {e!r}"))
        self.logger.emit("drift.summary", steps=dict(self._rungs))
        self.logger.emit("run.end", status=result.status, duration_ms=result.duration_ms, human_wait_ms=self.human_wait_ms)
        self.evidence.write_result(result)
        record_stability(self.options.evidence_root, self.cap.key, self.options.tenant, result.status, self._rungs)
        return result

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()

    # ---- the run ------------------------------------------------------------------------------

    async def _run(self) -> RunResult:
        cap, limits = self.cap, self.engine.effective
        try:
            entry = render(cap.target.entry_url, self.inputs)
        except MissingReference as e:
            return self._fail(self._error("INPUT_INVALID", None, expected="entry_url template to resolve", observed=str(e), message=str(e)))
        if err := await self._act(None, NavigateAction(type="navigate", url=entry), ActContext(risk="safe")):
            return self._fail(err)
        await self._capture("entry", dom=False)
        for i, step in enumerate(cap.steps):
            if i >= limits.max_steps:
                return self._fail(self._error("POLICY_BLOCKED", step, expected=f"at most {limits.max_steps} steps", observed=f"step {i + 1} of {len(cap.steps)}", message="policy max_steps exceeded"))
            if err := self._over_budget(step):
                return self._fail(err)
            if (result := await self._step(step)) is not None:
                return result
        if err := self._over_budget(None):
            return self._fail(err)
        if (r := await self._settle(None, None, ActContext(risk="safe"), cap.success_checkpoint, "success")) is not None:
            if not isinstance(r, RunError):
                return r
            await self._capture("success-failure")
            return self._fail(r)
        # url_capture outputs need no extract step (lint exempts them): the final URL is read here.
        late = [o for o in cap.outputs if o.source.kind == "url_capture" and o.name not in self._extracted]
        try:
            self.outputs.update(await extract_outputs([render_model(o, self.inputs) for o in late], self.surface))
        except OutputMissing as e:
            return self._fail(self._error("CHECKPOINT_FAILED", None, expected=e.expected, observed=e.observed, message=str(e)))
        self._extracted.update(o.name for o in late)
        missing = [o.name for o in cap.outputs if o.required and o.name not in self._extracted]
        if missing:
            return self._fail(self._error("CHECKPOINT_FAILED", None, expected=f"values for required outputs {missing}", observed="never extracted by any step", message=f"required outputs not extracted: {missing}"))
        await self._capture("success", dom=False)
        return SuccessResult(status="success", outputs=dict(self.outputs), steps_executed=self._steps_executed, **self._base())

    async def _step(self, step: Step) -> RunResult | None:
        """None when the step is done. Failures escalate (when the step says so) at most twice."""
        self._current = step
        interventions = 0
        out = await self._attempt(step)
        while isinstance(out, RunError):
            if step.on_failure != "escalate" or out.kind in _NEVER_ESCALATE:
                await self._capture(f"{step.id}-failure")
                return self._fail(out)
            if interventions == _MAX_INTERVENTIONS_PER_STEP:
                await self._capture(f"{step.id}-failure")
                return self._fail(self._error("CHECKPOINT_FAILED", step, expected=out.expected, observed=out.observed, message="step remained unsatisfied after human intervention"))
            interventions += 1
            req, decision = await self._escalate(step, "UNRECOVERABLE", detail=out.message, expected=out.expected, observed=out.observed)
            if decision is None:
                return self._escalated(req, step)
            if decision != "resume":  # abort; an approve here answers a question nobody asked
                self._human_resolved(req, decision, None)
                return self._fail(out)
            # Resume re-evaluates rather than continues: the human may have done the step, part of it, or nothing.
            if (cp := _postcondition(step)) is not None and await self._holds(cp):
                self._human_resolved(req, decision, "postcondition_satisfied")
                return await self._done(step)
            if all([await self._holds(pc) for pc in step.preconditions]):
                self._human_resolved(req, decision, "preconditions_rerun")
                out = await self._attempt(step)
            else:
                self._human_resolved(req, decision, "re_escalated")
        return out

    async def _attempt(self, step: Step) -> RunResult | RunError | None:
        n = self._attempts[step.id] = self._attempts.get(step.id, 0) + 1
        self.logger.emit("step.start", step_id=step.id, description=step.description, attempt=n)
        if hook := self.options.before_step.pop(step.id, None):  # a fault is armed once, not on every re-run
            await hook()
        for pc in step.preconditions:
            pc = render_model(pc, self.inputs)
            ok, observed = await poll(pc, self.surface, aliases=self.aliases)
            self.logger.emit("checkpoint.evaluated", step_id=step.id, site="precondition", ok=ok, expected=describe(pc), observed=observed)
            if not ok:
                kind: ErrorClass = "TARGET_NOT_FOUND" if pc.kind == "control_present" else "PRECONDITION_FAILED"
                return self._error(kind, step, expected=describe(pc), observed=observed, message=f"precondition failed: {describe(pc)}")
        try:
            action = render_model(step.action, self.inputs)
        except MissingReference as e:
            return self._fail(self._error("INPUT_INVALID", step, expected="every template reference to resolve", observed=str(e), message=str(e)))
        ctx = ActContext(risk=step.risk, timeout_ms=step.timeout_ms, expects_navigation=step.expects_navigation, step_id=step.id)
        if action.type not in ("wait", "assert", "extract"):
            url = action.url if action.type == "navigate" else await self.surface.url()
            d = self.engine.check(action_type=action.type, url=url, risk=step.risk, mode="replay")
            self.logger.emit("policy.decision", step_id=step.id, action_type=action.type, url=url, risk=step.risk,
                             decision=d.decision, reason=d.reason, preflight=True)
            if d.decision == "deny":
                return self._fail(self._error("POLICY_BLOCKED", step, expected=f"policy to allow {action.type} on {url}", observed=d.reason, message=f"policy denied {action.type}: {d.reason}"))
            if d.decision == "require_approval":
                req, decision = await self._escalate(step, "RISKY_ACTION_APPROVAL", detail=d.reason, expected="a human to approve this action before it is performed",
                                                     observed=d.reason, proposed_action=action)
                if decision is None:
                    return self._escalated(req, step)
                # The human may have performed the irreversible action by hand and handed back with
                # `resume`: advance, do not re-run and do not report a performed transaction as
                # failed. `postcondition is not None` matters: _holds(None) is True and would skip
                # an approval step whose action never ran.
                if decision == "resume" and step.postcondition is not None and await self._holds(step.postcondition):
                    self._human_resolved(req, decision, "postcondition_satisfied")
                    return await self._done(step)
                self._human_resolved(req, decision, None)
                if decision != "approve":
                    return self._fail(self._error("POLICY_BLOCKED", step, expected="human approval of the irreversible action", observed=f"decision {decision!r}", message="irreversible action was not approved"))
                ctx = ctx.model_copy(update={"approval_granted": True})
            if err := await self._act(step, action, ctx):
                return err
        site = action.type if action.type in ("wait", "assert") else "postcondition"
        if (r := await self._settle(step, action, ctx, _postcondition(step), site)) is not None:
            return r
        if action.type == "extract":  # after settling: an outcome page at extraction time is the answer, not a missing output
            specs = [render_model(self.cap.output_by_name(n), self.inputs) for n in action.outputs]
            try:
                self.outputs.update(await extract_outputs(specs, self.surface))
            except OutputMissing as e:
                return self._error("CHECKPOINT_FAILED", step, expected=e.expected, observed=e.observed, message=str(e))
            self._extracted.update(action.outputs)
            self.logger.emit("action.performed", step_id=step.id, action_type="extract", outputs=action.outputs)
        return await self._done(step)

    async def _act(self, step: Step | None, action: Action, ctx: ActContext) -> RunError | None:
        retries = step.retries if step else None
        budget = retries.max if retries and not _single_dispatch(ctx) else 0
        for attempt in range(budget + 1):
            try:
                res = await self.surface.act(action, ctx)
                break
            except SurfaceError as e:
                # Only a plain SurfaceError (a timeout, a dispatch failure) is transient; the subclasses are verdicts.
                transient = type(e) is SurfaceError
                if transient and attempt < budget:
                    self.logger.emit("retry", step_id=ctx.step_id, attempt=attempt + 1, reason=e.message)
                    await asyncio.sleep(retries.backoff_ms / 1000)
                    continue
                return self._error("TIMEOUT" if transient else e.error_class, step, expected=e.expected, observed=e.observed, message=e.message)
        if res.resolved is not None:
            r = res.resolved
            if step is not None:  # a recovery's rung is not the step's drift signal
                self._rungs[step.id] = r.rung_index
            self.logger.emit("target.resolved", step_id=ctx.step_id, won=r.won, rung_index=r.rung_index, attempts=r.attempts)
        self.logger.emit("action.performed", step_id=ctx.step_id, action=action, navigated=res.navigated,
                         url_after=res.url_after, document_status=res.document_status)
        return None

    async def _settle(self, step: Step | None, action: Action | None, ctx: ActContext, cp: Checkpoint | None, site: str) -> RunResult | RunError | None:
        """The tick loop after an action — and after the last step, for the success checkpoint.
        Each tick reads the page once and evaluates, in order: declared outcomes, recovery
        triggers, a surface error, the checkpoint. None once the checkpoint holds."""
        cp = render_model(cp, self.inputs) if cp else None
        outcomes = [(o, render_model(o.detect, self.inputs)) for o in self.cap.outcomes]
        rules = [(r, render_model(r.when, self.inputs)) for r in self.cap.recoveries]
        deadline = monotonic() + (cp.wait_ms if cp else 0) / 1000
        while True:
            view = TickView(self.surface)
            if self._violation:
                return self._error("POLICY_BLOCKED", step, expected="every navigation to stay within policy",
                                   observed=f"{self._violation['url']}: {self._violation['reason']}", message="a navigation left the allowlist")
            for outcome, detect in outcomes:
                if (await evaluate(detect, view, aliases=self.aliases))[0]:
                    return await self._outcome(step, outcome)
            recovered = await self._recover(step, action, ctx, rules, view, cp)
            if isinstance(recovered, RunError):
                return recovered
            if recovered:
                deadline = monotonic() + (cp.wait_ms if cp else 0) / 1000
                continue
            text = await view.visible_text(None)
            doc = await view.last_document_status()
            if doc is not None and doc.status >= 500:
                return self._error("SURFACE_ERROR", step, expected="the application to return a usable page",
                                   observed=f"HTTP {doc.status} from {doc.url} — {head(text, 200)}", message=f"the application returned HTTP {doc.status}")
            if "APPLICATION ERROR" in text:  # prose fallback only, for a surface that hides its status
                return self._error("SURFACE_ERROR", step, expected="the application to return a usable page",
                                   observed=f"application error page at {await view.url()} — {head(text, 200)}", message="the application showed its error page")
            if cp is None:
                return None
            ok, observed = await evaluate(cp, view, aliases=self.aliases)
            if ok or monotonic() >= deadline:
                self.logger.emit("checkpoint.evaluated", step_id=ctx.step_id, site=site, ok=ok, expected=describe(cp), observed=observed)
                if ok:
                    return None
                return self._error("CHECKPOINT_FAILED", step, expected=describe(cp), observed=observed, message=f"{site} checkpoint did not hold within {cp.wait_ms}ms")
            await asyncio.sleep(TICK_S)

    async def _recover(self, step: Step | None, action: Action | None, ctx: ActContext, rules: list[tuple[RecoveryRule, Checkpoint]],
                       view: TickView, cp: Checkpoint | None) -> RunError | bool:
        """Apply the first triggered rule with budget left. Applying it consumes one attempt of the
        rule's OWN budget; then_retry_step re-runs the step's action afterwards."""
        for rule, when in rules:
            used = self._recovery_attempts.get(rule.id, 0)
            if used >= rule.max_attempts or not (await evaluate(when, view, aliases=self.aliases))[0]:
                continue
            self._recovery_attempts[rule.id] = used + 1
            self._applied.append(rule.id)
            self.logger.emit("recovery.applied", step_id=ctx.step_id, rule_id=rule.id, attempt=used + 1, then_retry_step=rule.then_retry_step)
            for a in rule.do:
                a = render_model(a, self.inputs)
                fix = ActContext(risk="safe", timeout_ms=ctx.timeout_ms, expects_navigation=a.type in ("click", "navigate"), step_id=ctx.step_id)
                if err := await self._act(None, a, fix):
                    return err
            # wait/assert/extract have nothing to re-dispatch: the loop's next tick is their retry.
            if rule.then_retry_step and action is not None and action.type not in ("wait", "assert", "extract") and not await self._holds(cp):
                if _single_dispatch(ctx):
                    return self._error("POLICY_BLOCKED", step, expected=f"the {ctx.risk} {action.type} to take effect on its one dispatch",
                                       observed=f"recovery {rule.id!r} repaired the page but the action's effect is absent; a second dispatch could perform it twice",
                                       message=f"{ctx.risk} action not re-dispatched after recovery {rule.id!r}: one approval, one dispatch")
                if err := await self._act(step, action, ctx):
                    return err
            return True
        return False

    async def _outcome(self, step: Step | None, outcome: BusinessOutcome) -> RunResult | RunError:
        try:
            outputs = await extract_outputs([render_model(o, self.inputs) for o in outcome.outputs], self.surface)
        except OutputMissing as e:
            return self._error("CHECKPOINT_FAILED", step, expected=e.expected, observed=e.observed, message=str(e))
        step_id = step.id if step else None
        await self._capture(f"{step_id or 'success'}-outcome-{outcome.code}", dom=False)
        message = next((v for v in outputs.values() if isinstance(v, str) and v.strip()), outcome.description)
        self.logger.emit("outcome.matched", step_id=step_id, code=outcome.code, message=message, outputs=outputs)
        return BusinessOutcomeResult(status="business_outcome", code=outcome.code, message=message, outputs=outputs, at_step_id=step_id, **self._base())

    async def _done(self, step: Step) -> None:
        self._steps_executed += 1
        if _postcondition(step) is not None:
            await self._capture(f"{step.id}-postcondition", dom=False)
        self.logger.emit("step.end", step_id=step.id, summary="postcondition ok" if _postcondition(step) else "done")
        return None

    async def _holds(self, cp: Checkpoint | None) -> bool:
        return cp is None or (await evaluate(render_model(cp, self.inputs), self.surface, aliases=self.aliases))[0]

    # ---- escalation -----------------------------------------------------------------------------

    async def _escalate(self, step: Step, reason: InterventionReason, *, detail: str, expected: str, observed: str,
                        proposed_action: Action | None = None) -> tuple[InterventionRequest, Decision | None]:
        """Raise and park. A None decision means nobody answered: no escalator, or it timed out."""
        shot, dom = await self._capture(f"{step.id}-escalation")
        req = InterventionRequest(
            id=new_intervention_id(), created_at=datetime.now(timezone.utc), origin="replay", run_id=self.run_id,
            capability=self._ref(), reason=reason, detail=detail, at_step_id=step.id, step_description=step.description,
            expected=expected, observed=observed, proposed_action=proposed_action,
            context=InterventionContext(
                url=await self._quiet(self.surface.url, ""), title=await self._quiet(self.surface.title, ""),
                screenshot_path=self.evidence.relative(shot) if shot else None,
                dom_path=self.evidence.relative(dom) if dom else None, recent_events=self.logger.tail(20),
            ),
        )
        self.logger.emit("escalation.raised", intervention_id=req.id, reason=reason, step_id=step.id, detail=detail,
                         expected=expected, observed=observed, url=req.context.url, screenshot_path=req.context.screenshot_path)
        if self.escalator is None:
            return req, None
        t0 = monotonic()
        try:
            req = await asyncio.wait_for(self.escalator.raise_intervention(req), timeout=self.options.escalation_timeout_s)
        except asyncio.TimeoutError:
            self.human_wait_ms += int((monotonic() - t0) * 1000)
            self.logger.emit("escalation.timeout", intervention_id=req.id, waited_s=self.options.escalation_timeout_s)
            return req, None
        self.human_wait_ms += int((monotonic() - t0) * 1000)
        return req, (req.resolution.decision if req.resolution else "abort")

    def _human_resolved(self, req: InterventionRequest, decision: Decision, branch: str | None) -> None:
        self.logger.emit("human.resolved", intervention_id=req.id, decision=decision, human_actions=len(req.human_actions),
                         resume_branch=branch, human_wait_ms=self.human_wait_ms)

    def _escalated(self, req: InterventionRequest, step: Step) -> EscalatedResult:
        return EscalatedResult(status="escalated", intervention_id=req.id, reason=req.reason, at_step_id=step.id, **self._base())

    # ---- results, evidence, bookkeeping --------------------------------------------------------

    def _pend(self, expected: str, observed: str, message: str, kind: ErrorClass = "INPUT_INVALID") -> None:
        self._pending = self._fail(self._error(kind, None, expected=expected, observed=observed, message=message))
        self.evidence.write_result(self._pending)

    def _error(self, kind: ErrorClass, step: Step | None, *, expected: str, observed: str, message: str) -> RunError:
        return RunError(kind=kind, step_id=step.id if step else None, step_description=step.description if step else None,
                        expected=expected, observed=observed, message=message, recovery_attempts=list(self._applied))

    def _fail(self, err: RunError) -> FailedResult:
        if self._violation and err.kind != "POLICY_BLOCKED":
            # The browser was closed by the redirect gate; whatever failed afterwards failed because of that.
            err = err.model_copy(update={"kind": "POLICY_BLOCKED", "observed": f"{self._violation['url']}: {self._violation['reason']}"})
        return FailedResult(status="failed", error=err, **self._base())

    def _base(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "capability": self._ref(), "tenant": self.options.tenant,
                "duration_ms": int((monotonic() - self._t0) * 1000), "evidence_dir": self.evidence.relative(self.evidence.run_dir)}

    def _ref(self) -> CapabilityRef:
        return CapabilityRef(id=self.cap.id, name=self.cap.name, version=self.cap.version)

    def _over_budget(self, step: Step | None) -> RunError | None:
        budget = self.engine.effective.replay_timeout_ms
        spent = int((monotonic() - self._t0) * 1000) - self.human_wait_ms
        if spent <= budget:
            return None
        return self._error("TIMEOUT", step, expected=f"the run to finish within {budget}ms",
                           observed=f"{spent}ms spent (excluding {self.human_wait_ms}ms waiting on a human)", message="replay wall clock exceeded")

    async def _capture(self, label: str, *, dom: bool = True) -> tuple[Path | None, Path | None]:
        """Masked screenshot (+ DOM snapshot): the richer signal on failure. Best effort — the
        browser may already be gone, and evidence must never take the run down."""
        shot = dom_path = None
        try:
            shot = self.evidence.write_bytes(self.evidence.screenshot_path(label), await self.surface.screenshot(mask_sensitive=True))
            if dom:
                dom_path = self.evidence.write_text(self.evidence.dom_path(label), await self.surface.dom_snapshot())
        except Exception:  # noqa: BLE001
            pass
        return shot, dom_path

    @staticmethod
    async def _quiet(fn: Callable[[], Awaitable[str]], default: str) -> str:
        try:
            return await fn()
        except Exception:  # noqa: BLE001
            return default

    def _on_violation(self, url: str, reason: str) -> None:
        self._violation = {"url": url, "reason": reason}
        self.logger.emit("policy.decision", action_type="navigate", url=url, risk="safe", decision="deny", reason=reason, preflight=False)


async def replay(capability: Capability, options: ReplayOptions, *, escalator: Escalator | None = None) -> RunResult:
    run = ReplayRun(capability, options)
    await run.prepare()
    run.escalator = escalator
    try:
        return await run.execute()
    finally:
        await run.close()


def _postcondition(step: Step) -> Checkpoint | None:
    """A wait/assert step's body is its postcondition (both, when it also declares one)."""
    a = step.action
    if a.type not in ("wait", "assert"):
        return step.postcondition
    if step.postcondition is None:
        return a.checkpoint
    return All(kind="all", wait_ms=a.checkpoint.wait_ms, of=[a.checkpoint, step.postcondition])


def _single_dispatch(ctx: ActContext) -> bool:
    """An irreversible or approved action is dispatched exactly once: a retry after a timeout, or a
    re-run after a repair, could perform it twice under one approval."""
    return ctx.risk == "irreversible" or ctx.approval_granted


# ---- inputs -------------------------------------------------------------------------------------


def coerce_inputs(specs: list[ParamSpec], given: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate and coerce against the contract, collecting EVERY issue rather than failing on the first."""
    issues = [f"unknown input {k!r}" for k in given if not any(p.name == k for p in specs)]
    out: dict[str, Any] = {}
    for p in specs:
        if p.name in given:
            value = given[p.name]
        elif p.default is not None:
            value = p.default
        elif p.required:
            issues.append(f"missing required input {p.name!r}")
            continue
        else:
            continue
        try:
            value = _coerce(p, value)
        except (TypeError, ValueError) as e:
            issues.append(f"input {p.name!r}: {e}")
            continue
        if p.pattern and not re.search(p.pattern, str(value)):
            issues.append(f"input {p.name!r}: {value!r} does not match {p.pattern}")
            continue
        out[p.name] = value
    return out, issues


def _coerce(p: ParamSpec, v: Any) -> Any:
    if p.type == "number":
        if isinstance(v, bool):
            raise ValueError(f"{v!r} is not a number")
        f = float(v)
        if not math.isfinite(f):
            raise ValueError(f"{v!r} is not finite")
        return int(f) if f.is_integer() and "." not in str(v) else f
    if p.type == "boolean":
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in ("true", "1"):
            return True
        if s in ("false", "0"):
            return False
        raise ValueError(f"{v!r} is not a boolean (true/false/1/0)")
    if p.type == "enum":
        if str(v) not in p.enum_values:
            raise ValueError(f"{v!r} is not one of {p.enum_values}")
        return str(v)
    return str(v)


def input_contract(specs: list[ParamSpec]) -> str:
    bits = []
    for p in specs:
        b = f"{p.name}: {p.type}"
        if p.enum_values:
            b += f" in {p.enum_values}"
        if p.pattern:
            b += f" matching {p.pattern}"
        b += " (required)" if p.required and p.default is None else f" (default {p.default!r})"
        bits.append(b)
    return "inputs {" + "; ".join(bits) + "}"


def sensitive_targets(cap: Capability) -> list[TargetDescriptor]:
    """Controls to mask in screenshots: pii/secret output cells, and any field a pii input or a
    secret is typed into."""
    private = {p.name for p in cap.inputs if p.sensitivity != "public"}
    out = [o.source.target for o in (*cap.outputs, *(x for oc in cap.outcomes for x in oc.outputs))
           if o.sensitivity != "public" and o.source.target is not None]
    for a in (*(s.action for s in cap.steps), *(a for r in cap.recoveries for a in r.do)):
        if a.type == "type" and any(kind == "secrets" or name in private for kind, name in references(a.value)):
            out.append(a.target)
    return out
