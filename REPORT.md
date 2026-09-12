# Understudy — design write-up

## 1. Architecture

A single Python process writing JSON to a directory. No database, no broker, no worker pool. The
unit of work is small — one browser session, seven to nine steps, half a second — and the scaling
story belongs to whatever schedules those sessions, not to the engine inside one.

The seams that matter are internal, and each holds an invariant:

| Seam | Invariant |
|---|---|
| `schema/` | pure data + lint; imports nothing else in the project |
| `surface/protocol.py` | **imports no browser driver** — what a desktop resolver would implement |
| `replay/` | imports no model SDK and no `discover/` |
| `policy/` | receives injection; knows nothing about surfaces |

`tests/test_invariants.py` walks the import graph and fails the build on the middle two, so both
survive a refactor by somebody who never read this document.

Everything else follows from one restriction: **the model may not author a locator.** Its tools
accept a `ref` — `c1`, `c7` — and nothing else; the descriptor that reaches the artifact is emitted
by `surface/web/perception.py`, which has the element handle in front of it. So `tests/test_surface.py`
interrogates 39 targeting behaviours with no API key: none of them depend on what a model said.

The two modes are tuned apart on purpose: discovery gets a ten-minute clock where replay gets two,
and it refuses a risky action outright rather than parking on it. The refusal never reaches the
trace, so an action nobody approved cannot be compiled into a capability later.

**Trade-off taken:** a stuck replay holds a browser for the life of the intervention. At scale that
becomes a worker with a lease; the `RunHandle`/`Escalator` protocols are already that seam.

## 2. Artifact schema

**(a) Targets are described, not selected.** A `TargetDescriptor` carries semantics — role, frame
path, inferred label, and a `rationale` sentence a reviewer can read — plus an **ordered ladder** of
redundant strategies (`role_name`, `inferred_label`, `placeholder`, `table_cell`, `text`,
`attribute`, `nth_of_role`, `css`, `coordinate`), each with a confidence and a `captured` vs
`derived` provenance flag. A selector is a fallback rung, never the control's identity. `coordinate` stores
the viewport it was measured in and declines to run when the live one differs — a pixel landing on
the wrong element at the wrong window size fails silently. Capture re-queries every strategy against
the live page and counts matches, so a rung that does not select exactly the intended element is
dropped before the artifact is written.

**(b) Assertions are data, not callbacks.** Six predicates — `text_present`, `control_present`,
`url_matches`, `http_status`, and `all`/`any`/`not`. Deliberately not expressive enough to compute:
an artifact a reviewer reads end to end beats one that can express anything, and one small evaluator
then serves preconditions, postconditions, step asserts, outcome detectors, recovery triggers and
the success condition. Waiting is one field, `wait_ms`, meaning one thing everywhere; lint rejects a
poll interval anywhere but a checkpoint root, so there is no second timing dial to drift.

**(c) "No such member" is a declared answer, not an exception.** Each capability lists its
`outcomes` — code, detector checkpoint, values to extract from that screen — and replay returns one
as `status: "business_outcome"`, exit 0. Raising instead would work until somebody wraps the call in
a retry and a legitimate "not found" starts looking like an outage.

Marking a field `pii` or `secret` does something rather than describing something: the value is
registered with the redactor, its on-screen region is masked in every screenshot, and a `secret`
output never enters the exported tool schema. Those same typed inputs *are* the schema `catalog
tools` hands an agent, so the calling contract cannot drift from the executed one. The transcript
lives beside the artifact under `discovery/`, never inside it.

## 3. Determinism & error handling

**The evaluation order is the design.** One tick loop runs at every assertion site and checks, in
order: declared **business outcomes** → **recovery** rules → **surface error** → the
**postcondition**. Outcomes come first, so nothing can be called a failure before it has been
offered as an answer, and they are re-checked on every tick of the postcondition poll rather than
sampled once — a "no such member" banner that renders a beat late must not read as a crash. The
correctness here is ordering, not coverage.

A surface error is decided on the HTTP status of the documents on screen — on a frameset, the
worst of them, so a 500 in the content frame is not masked by the nav frame's 200. Matching the
target's error banner would have passed this demo and transferred nowhere, so the page's wording is
consulted only when the transport says 200 and the body still says `APPLICATION ERROR`.

Recovery has its **own** attempt budget, separate from transient retries — sharing one dial means
recovery silently doesn't retry whenever retries default to zero. A recovered run returns a plain
`success`; only `run.jsonl` shows `recovery.applied`.

**Framesets, not drift, are where the flakiness comes from.** A `<frameset>` parent reports itself
loaded without waiting for its children, so `wait_for_load_state` returns against a child that has
not moved; read it and Playwright raises "Execution context was destroyed", and any handle taken
beforehand is stale. What works: install a frame-scoped listener *before* the click, race a URL
change against a quiet timer, then fetch the frame again **by name** — never by index, because
attach order follows the network. Which steps navigate is captured at discovery and stored on the
step, not inferred at runtime.

**Position may be used to click, never to read.** Clicking the wrong row produces a wrong screen,
which the next checkpoint catches; reading the wrong cell produces a plausible number under the
right field name, which nothing catches. So `nth_of_role`, `css` and `coordinate` are skipped when
`intent == "read"`, and a Pydantic validator refuses to *load* a read descriptor whose rungs are all
positional — rejected at parse time rather than resolving happily against whatever sits there.

The result contract has four arms: `success`, `business_outcome`, `escalated`, and `failed` with an
error class plus **`expected` and `observed` as prose**, produced by the same evaluator that made the
decision. Exit codes: `0` for success *or* a declared outcome, `1` failure, `2` escalation.

## 4. Heterogeneity & multi-tenant

`surface/protocol.py` names five operations — `observe`, `resolve`, `act`, `visible_text`,
`screenshot` — and imports nothing but `dataclasses`, `typing` and our own schema. A Win32 or macOS
implementation supplies the same five against UIAutomation or AX, which already yield role, name and
bounding box: exactly what a `TargetDescriptor` records. Nothing above the seam knows a browser was
involved, so the artifact format, the evaluator, the outcome and recovery machinery and the four-arm
result carry over untouched.

Being candid about what accessibility-first bought: on this markup `page.aria_snapshot()` yields
`textbox ""` for the member-number field, because HTML-AAM does not compute a name from an adjacent
table cell — a gap wide enough that a model shown the accessibility tree alone concluded the
sign-on form could not be filled in. Perception starts from AX and enriches it from the DOM: an
inferred-label ladder (`label[for]` → wrapping
`<label>` → preceding cell in the same row → preceding text block → nearest text left or above →
placeholder → `name`), recording *which* rule produced the name. On this application the adjacent
cell wins more often than `label[for]` does — so a single-strategy descriptor would be one that
works on modern markup and fails on the markup this project exists for. Hence a ladder, not a best
guess.

**Re-recording per institution does not scale and is not the plan.** A tenant contributes a delta
over one base artifact. An override supplies a per-tenant
entry URL, a frame-path remap, per-step partials, and vocabulary aliases consumed *inside* the
resolver. Objects deep-merge; lists replace wholesale. The merged capability must **re-pass the same
schema and lint gates** the on-disk artifact passed: what is about to execute can never be weaker
than what was reviewed.

Two drift signals come free of the ladder itself. Each run ends with a `drift.summary` naming the
rung index that resolved every step; twenty consecutive replays currently report all zeroes, and a
step that begins answering at index 3 is a UI change that has not yet broken anything but will. And
on `TARGET_NOT_FOUND`, a
second attempt by role and nearby text writes a *candidate* override into the run directory and
stops. Nothing applies it: a system that quietly repairs its own automation against a core banking
screen has deleted the one step where a person would have noticed.

## 5. Escalation & handoff

**Detecting stuck.** Replay escalates when a step marked `on_failure: escalate` has exhausted
outcomes, recovery and retries, or when policy requires approval for a risky action — the latter
*before* the action reaches the surface. Discovery escalates on `request_human_help`, the step cap,
or three observations in a row that changed nothing. Budget exhaustion on its own is not a
stuck-detector — it is a bill.

**Taking control.** Context, page, cookie jar and a `SessionControl` lease live on one
`BrowserSession`, and the console is handed the object the run is already driving. The rule that
makes the handoff real is negative: **no route in `console/` may construct a browser.** Break it and
the operator is looking at a lookalike without the state that caused the problem. The console pushes JPEG
frames of the real page over a WebSocket and forwards clicks (as viewport fractions), keystrokes and
scrolls into the same `Page`.

The lease is enforced by one line at the top of `Surface.act()`: automation's next action raises
`ControlLost` while a human holds it. Human input is **recorded before it is dispatched**, so an
action that throws is still on the record, and it passes the same policy engine.

**Handing back is where this gets subtle.** An operator who has been inside the session may have
completed the step, made a partial mess of it, or simply looked and given up, and automation cannot
tell which from the fact that control came back. So resume asks the page instead of assuming:

| State on resume | Branch | Action |
|---|---|---|
| postcondition now holds | `postcondition_satisfied` | advance; do **not** re-run |
| preconditions hold, postcondition does not | `preconditions_rerun` | re-run the step once |
| neither | `re_escalated` | escalate once more, then fail cleanly |

Time parked on a human is **excluded from the wall clock**. `approve` is refused for anything that
is not a risky-action approval. Both halves are in `/evidence/`: one run where the operator
*approved*, and one where the operator *took the session and performed the step by hand* —
`human_actions` non-empty, `resume_branch: postcondition_satisfied`, and no `s8` in that run's
`drift.summary`, because the step was never re-run.

**Cut, and named:** the live view is a screenshot poll with input forwarding, not a co-browsing
stack, and the console has no authentication — it binds to loopback.

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

`policy/risk.py` grades each action as it is recorded, so the label a reviewer reads is the label
the gate enforces. Grading is blunt on purpose — a form submit is irreversible, so is a control
named "Transfer" or "Confirm" — because over-grading costs an operator one flag and under-grading
posts a transaction. The check runs on the first line of `Surface.act()`, ahead of resolution and
any I/O; in the system prompt it would be a request. Four more places re-check: an executor
preflight, a `framenavigated` listener on redirects, the discovery gate, and human input.

**Credentials are referenced, never carried.** An artifact holds `{{secrets.APP_PASSWORD}}`; the
value is read from the environment at the moment of typing and is never an attribute of anything
serialised, so rotating it needs no re-record. Redaction sits at the three places bytes leave the
process — event logger, evidence writer, intervention store — not at the call sites, so a new
caller cannot forget it. The console is fed the same redacted dictionary that reached disk.

**Where it stops.** Origins and paths are the wrong vocabulary for "read balances but do not move
money"; that lives in which capabilities exist and who approved them, and a semantic layer is the
honest next step. Redaction covers what was declared plus what the sweeps catch, so PII nobody
marked can still reach a screenshot. And the threat model trusts the artifact — a hostile one would
declare itself safe. `approval` and the catalog's refusal to expose drafts stand in for that, and
neither replaces a person reading the diff.

## 7. Cuts

**Not built**, seams left clean: a desktop surface (§4 is that seam), real co-browsing,
queues/workers/containers, console authentication, a database. **Thin on purpose:** the console is
one HTML file, the catalog is a directory, fault injection is a query parameter. Of the stretch
goals I took two — the catalog with its tool export, and cross-tenant reuse; the draft→approved gate
and the stability sidecar fell out of the `approval` field and the `drift.summary` event.

**What real runs exposed, and tests could not.** Three provider-level defects, detailed under
*Provider notes* in `README.md`. Two recording defects: a model declared `transform: "none"` on a
currency output — type-correct at record time, broken on *every* replay — and a required input no
step referenced. Lint **L011** and **L012** reject both now, and every override is logged as
`recorder.overruled`; both fire in the second discovery run in `/evidence/`.

And one defect in a gate itself. A run that reached its screen correctly, `success_text` a plain
heading, was rejected by lint **L006** — "embeds the example value of input `deposit` ('500')". L006
searched the *serialized* checkpoint, where `500` matches the digits of `"wait_ms": 5000`: a correct
recording killed by the rule meant to catch over-fitting. It searches the checkpoint's strings only
now. The lesson is not that models behave badly — it is that **every mechanical gate is itself code
that can be wrong**, and one that only ever sees hand-written fixtures never gets the chance to
prove it.

**Next:** bounded single-step model recovery on replay failure, policy-checked and recorded; a
second real surface, to prove the Protocol rather than assert it; promotion gated on measured
stability rather than a human flipping a field.

**Known weaknesses**, each marked in the code with a `ponytail:` comment naming its ceiling.
Recovery rules are capability-scoped, so one written for a later screen can fire early and overshoot
— it fails cleanly and names the rule in `recovery_attempts`, but `applies_to` would be better. Row
scoping uses `closest('tr')`, so a div-grid tenant needs a `row_selector` the resolver does not
consult. The stability sidecar has no cross-process lock, so it is telemetry, not an audit trail.
Beyond those: discovery is the single path tests cannot cover; perception is tuned to table/frameset
and div markup, so a canvas-rendered app would need the screenshot channel to do real work; and
`error_rate` flakiness is simulated.
