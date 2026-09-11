# Understudy — design write-up

## 1. Architecture

One process, files on disk, no database and no queue. A replay is one browser session doing one
short flow; there is nothing to scale yet, and building for scale before the abstractions are
right is how you end up with the wrong ones.

The seams that matter are internal, and each holds an invariant:

| Seam | Invariant |
|---|---|
| `schema/` | pure data + lint; imports nothing else in the project |
| `surface/protocol.py` | **imports no Playwright** — it is what a desktop resolver would implement |
| `replay/` | imports no model SDK and no `discover/` — *enforced by `tests/test_invariants.py`* |
| `policy/` | receives injection; knows nothing about surfaces |

The load-bearing decision: **the model never writes a locator.** It picks a control by a `ref` it
was shown in an observation; the perception layer — which can actually see the element — emits the
descriptor. Targeting quality is therefore a property of this code, testable with no model at all,
rather than a property of model output.

Discovery and replay are deliberately asymmetric. Discovery gets its own longer wall clock (a live
exploration is not a deterministic replay; sizing them the same kills real runs), risky actions are
denied outright rather than escalated, and a denied action is never written to the trace — so it
cannot be recorded into an artifact.

**Trade-off taken:** a stuck replay holds a browser for the life of the intervention. At scale that
becomes a worker with a lease. That is deployment work, and the `RunHandle`/`Escalator` protocols
are already the seam for it.

## 2. Artifact schema

Three choices did the work.

**(a) Targets are described, not selected.** A `TargetDescriptor` carries semantics — role, frame
path, inferred label, and a `rationale` sentence a reviewer can read — plus an **ordered ladder** of
redundant strategies (`role_name`, `inferred_label`, `placeholder`, `table_cell`, `text`,
`attribute`, `nth_of_role`, `css`, `coordinate`), each with a confidence and a `captured` vs
`derived` provenance flag. No CSS selector is ever the identity. `coordinate` records the viewport
it was captured at and is refused if that differs: a coordinate that quietly works at the wrong
size is worse than a clean failure. Every strategy is **verified at capture time** against the live
page; one that does not actually select the target never enters the artifact.

**(b) Checkpoints are a small predicate language, not code.** `text_present`, `control_present`,
`url_matches`, `http_status`, `all`/`any`/`not`. Not Turing-complete, so an artifact stays
reviewable and safe to store, and one evaluator serves all six assertion sites. Timing is a single
field, `wait_ms`, with a single meaning. A field that means different things depending on where it
sits is a field that silently does nothing somewhere.

**(c) Business outcomes are members of the artifact.** `MEMBER_NOT_FOUND` is declared, typed and
returned as a first-class status — not raised. If outcomes live in the error path, sooner or later
somebody catches one as a failure.

`sensitivity` on every input and output is behaviour, not documentation: it drives redaction,
screenshot masking, and whether a value reaches the caller at all. The artifact's typed inputs
*are* the agent's tool schema (`catalog tools`) — one definition, no drift. The discovery transcript
is stored beside the artifact, never inside it: the artifact is a capability, the transcript is how
it was found.

## 3. Determinism & error handling

**The evaluation order is the design.** After every action, one tick loop runs at every assertion
site and checks, in order:

1. declared **business outcomes** → terminal, returned as an answer
2. **recovery** rules → apply the declared repair, then re-check
3. **surface error** → `last_document_status() >= 500`
4. the **postcondition** → step done

Outcomes are checked before anything can be called a failure, and they are polled *alongside* the
postcondition rather than sampled once — otherwise an error page that renders a beat late is
reported as a crash. That is the brief's "most common design mistake", and it is an ordering
property, not a matter of adding more cases.

Surface errors key off transport status, with vendor error prose as fallback only. Matching known
error copy would have demoed fine and generalised to nothing.

Recovery has its **own** attempt budget, separate from transient retries — sharing one dial means
recovery silently doesn't retry whenever retries default to zero. A recovered run returns a plain
`success`; only `run.jsonl` shows `recovery.applied`, because from the caller's point of view
nothing went wrong.

**The frameset trap**, which is most of what "flaky replay" actually is here: child-frame navigation
is not tracked by page load state. `wait_for_load_state("networkidle")` returns while the content
frame is still on its old URL, reading it then throws "Execution context was destroyed", and frame
handles go stale. So: arm a frame-scoped wait **before** dispatching, race it against a URL change
and a quiet timer, then re-acquire the frame **by name** — with a poll, because a frameset reload
destroys and recreates its children. Whether a step navigates is *recorded at discovery*, not
guessed; guessing is wrong half the time in each direction.

**Never fall back to a position when reading business data.** Positional strategies are legal for a
click — clicking the wrong row is visible and recoverable — and refused for reading a value, where
the failure is silent. A schema validator enforces this, so it fails at compile time rather than
returning one member's balance under another member's name.

The result contract has four arms: `success`, `business_outcome`, `escalated`, and `failed` with an
error class plus **`expected` and `observed` as prose**, produced by the same evaluator that made
the decision. Exit codes: `0` for success *or* a declared outcome, `1` failure, `2` escalation.

## 4. Heterogeneity & multi-tenant

`Surface` is a Protocol importing no Playwright, on purpose. A desktop implementation maps
`observe` → the platform accessibility API, `resolve` → the same descriptor ladder against
UIAutomation/AX, `act` → synthesised input. Artifact, checkpoints, outcomes, recoveries and result
contract do not change.

Worth being candid about what accessibility-first actually bought. On this markup
`page.aria_snapshot()` yields `textbox ""` for the member-number field, because HTML-AAM does not
compute a name from an adjacent table cell — and that single gap is enough to make a model report
that a form "cannot be operated". So perception is **AX-first with a DOM enrichment pass**: an
inferred-label ladder (`label[for]` → wrapping `<label>` → preceding cell in the same row →
preceding text block → nearest text left or above → placeholder → `name`), with the *source*
recorded. On legacy markup the fallback is the common case, not the exception. That is precisely
why the descriptor carries a ladder at all.

**Multi-tenant is base + deltas, never one recording per tenant.** An override supplies a per-tenant
entry URL, a frame-path remap, per-step partials, and vocabulary aliases consumed *inside* the
resolver. Objects deep-merge; lists replace wholesale, so an overridden `strategies` list is an
explicit replacement. The merged capability must **re-pass the same schema and lint gates** the
on-disk artifact passed: what is about to execute can never be weaker than what was reviewed.

Two drift signals fall out free. Every resolution logs **which rung won** — a step that starts
resolving at rung 3 is a UI change that has not broken anything *yet*. And on `TARGET_NOT_FOUND`, a
re-resolve by role and nearby text writes a *proposed* override into evidence and **never applies
it**. Auto-healing a bank's back office without review is not a feature.

## 5. Escalation & handoff

**Detecting stuck.** Replay escalates when a step marked `on_failure: escalate` has exhausted
outcomes, recovery and retries, or when policy requires approval for a risky action — the latter
*before* the action reaches the surface. Discovery escalates on `request_human_help`, the step cap,
or **three consecutive observations with no progress**. A step cap alone just makes a stuck agent
expensive.

**Taking control.** One `BrowserSession` owns context, page, cookie jar and a two-state control
lease. The run registers *its own* session with the console; **no console route may ever launch a
browser**, and that constraint is what the live-handoff claim rests on. The console pushes JPEG
frames of the real page over a WebSocket and forwards clicks (as viewport fractions, so scaling
cannot skew them), keystrokes and scrolls into the same `Page`. The human drives the page that has
the problem, never a fresh one — a fresh session would not have the state that caused it.

The lease is enforced by one line at the top of `Surface.act()`, before resolution and before any
I/O: automation's next action raises `ControlLost` while a human holds it. Human input is **recorded
before it is dispatched**, so an action that throws is still on the record, and it passes the same
policy engine — an off-allowlist navigate is refused.

**Handing back — resume re-evaluates rather than continues.** The human may have done the step, part
of it, or nothing:

| State on resume | Branch | Action |
|---|---|---|
| postcondition now holds | `postcondition_satisfied` | advance; do **not** re-run |
| preconditions hold, postcondition does not | `preconditions_rerun` | re-run the step once |
| neither | `re_escalated` | escalate once more, then fail cleanly |

The branch is logged, so the resume decision is auditable. Time parked on a human is **excluded from
the wall clock** — a careful reviewer must not cause the next step to time out. `approve` is refused
for anything that is not a risky-action approval: approving a stuck step would just re-run it and
burn the escalation budget.

Both halves are in `/evidence/`: one run where the operator *approved* the irreversible step, and
one where the operator *took the session and performed it by hand* — `human_actions` non-empty,
`resume_branch: postcondition_satisfied`, and no `s8` in that run's `drift.summary`, because the
step was never re-run. The second operator is a script (`scripts/operator_takeover.py`) so the run
reproduces unattended; it drives the real console routes and leaves the real record.

**Cut, and named:** the live view is a screenshot poll with input forwarding, not a co-browsing
stack, and the console has no authentication — it binds to loopback, an accepted single-operator
trade-off. The control-transfer model is real and tested.

## 6. Safety

**The security boundary is operator-side.** `config/policy.toml` is the grant; an artifact's
`policy_declaration` is a *request*; the effective policy is the **intersection**. An artifact can
only narrow. Reading the gate from the artifact would put the security boundary inside the thing
being reviewed — an artifact shipping `require_approval_for: []` would post an irreversible
transaction with no gate at all.

Four independent dimensions, all failing closed (no resolvable URL, unparseable URL, or unknown
action type → deny): action-type allowlist, origin allowlist, path patterns, and a risk ceiling
(`safe < sensitive < irreversible`). Above `max_risk` → deny. Inside `require_approval_for` → replay
requires a human, **discovery denies outright**. Stalling beats posting a transaction.

Risk is classified at record time and written into the artifact where a reviewer sees it. The gate
sits inside `Surface.act()`, before resolution and before any I/O, with no bypass — not in the
prompt, where a model could argue with it. Defence in depth: an executor preflight, a
`framenavigated` listener re-checking redirects the artifact never asked for, the discovery-time
check, and the human-input check.

**Secrets are never stored.** Artifacts name environment variables; values are read at substitution
time, so a rotated credential takes effect next run and no evidence file holds one. Redaction is
applied at the *sinks* — event logger, evidence writer, intervention store — so nothing sensitive
can be written by construction. A live viewer is fed the same post-redaction object that hit disk,
making it structurally impossible for the console to show what the log masked.

**Limits, honestly.** The allowlist is origin- and path-level, not semantic: it cannot express "may
read balances, may not move money" — that lives in risk classification instead. Redaction is
deny-list shaped, so an undeclared PII field could still reach evidence. And nothing defends against
a malicious artifact; artifacts are trusted input, which is why `approval` exists and why the agent
path refuses drafts.

## 7. Cuts

**Not built**, seams left clean: a desktop surface (the Protocol is that seam, §4), real
co-browsing, queues/workers/containers, console authentication, a database, and bounded
model-assisted recovery on replay failure — top of the next list, because the escalation seam
already exists. **Thin on purpose:** the console is one HTML file, the catalog is a directory,
fault injection is a query parameter. Of the stretch goals I took two — the capability catalog with
its tool export, and cross-tenant reuse via `tenant_overrides`; the draft→approved gate and the
stability sidecar fell out of the `approval` field and the `drift.summary` event.

**What real runs exposed, and tests could not.** Three provider defects: a model whose tool call
loses its `thought_signature` is rejected on the next turn; a provider that refuses multipart
content ended a run over a screenshot that was only a supplement; screenshots written to a
double-rooted path were on disk but invisible in the directory a reviewer opens. And two recording
defects: a model declared `transform: "none"` on a currency output — type-correct at record time,
broken on *every* replay — and a required input no step referenced. Lint **L011** and **L012** now
reject both, and every override is logged as `recorder.overruled`. The lesson is not that models
behave badly; it is that **a recording pipeline must be able to overrule the model**, and where it
must is only visible against a real one.

**Next:** promotion gated on measured replay stability instead of a human flipping a field; a second
real surface to prove the Protocol rather than assert it; semantic policy ("may read balances, may
not move money"); bounded single-step model recovery; cross-tenant drift aggregation.

**Known weaknesses:** discovery is the one path tests cannot cover and the one most exposed to
provider behaviour; perception is tuned to legacy table/frameset markup and a modern div skin, so a
canvas-rendered app would need the screenshot channel to do real work; and `error_rate` flakiness is
simulated — real transient failure has a shape I have not measured.
