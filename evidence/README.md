# Evidence

Sixteen committed runs: **two** genuine LLM-driven discoveries, and fourteen deterministic
replays covering every arm of the result contract. Every run here was produced by the commands in
the root `README.md`; no log, result or screenshot is hand-written.

## The contrast the design is built around

Put these two side by side. They are the reason the result contract has four arms instead of
"worked / threw":

| Run | Verdict | Exit | Who deals with it |
|---|---|---|---|
| `03-replay-outcome-member-not-found` | `business_outcome` `MEMBER_NOT_FOUND` | **0** | the calling agent — "no such member" is an answer |
| `06-replay-failed-surface-error-http-500` | `failed` `SURFACE_ERROR` | **1** | a human — the application broke |

Both "did not return a balance". Only one is a failure. Conflating them is the mistake the
brief's own glossary warns about, so the artifact *declares* its business outcomes and replay
checks them before it ever considers a step failed.

## Every run

Directories are named for what the run demonstrates, in the order the story is best read. The
original `run_id` — `replay-<timestamp>-<hex>`, as generated — is unchanged inside every
`result.json` and on every line of `run.jsonl`; only the directory name and the paths pointing at
it were rewritten, so a reviewer can browse by meaning without losing the identity.

| Directory | Capability | Verdict | Notes |
|---|---|---|---|
| `00-discovery-live-llm-member-lookup` | — | **discovery** | the real LLM run: 17 turns, 8 screenshots, full redacted transcript |
| `01-replay-discovered-artifact-new-member` | `alpha.member.lookup` | success | **the discovered artifact**, replayed for a member the model never saw |
| `02-replay-success-happy-path` | `alpha.member.balance` | success | happy path |
| `03-replay-outcome-member-not-found` | `alpha.member.balance` | `MEMBER_NOT_FOUND` | declared business outcome, exit 0 |
| `04-replay-recovered-maintenance-interstitial` | `alpha.member.balance` | success | 503 maintenance interstitial, **recovered** |
| `05-replay-recovered-session-expiry` | `alpha.member.balance` | success | 440 session timeout, **re-authenticated and resumed** |
| `06-replay-failed-surface-error-http-500` | `alpha.member.balance` | `SURFACE_ERROR` | HTTP 500, exit 1, + DOM snapshot |
| `07-replay-slow-load-absorbed` | `alpha.member.balance` | success | 4-second stall absorbed by the checkpoint poll |
| `08-replay-success-tenant-beta` | `alpha.member.balance` | success | **tenant `beta`** — same artifact, different skin |
| `09-replay-escalated-nobody-attached` | `alpha.share.open` | `escalated` | irreversible step, no human attached, exit 2 |
| `10-replay-outcome-deposit-too-small` | `alpha.share.open` | `DEPOSIT_TOO_SMALL` | the application refused the deposit |
| `11-replay-outcome-permission-denied` | `alpha.share.open` | `PERMISSION_DENIED` | restricted member, teller lacks authority |
| `12-replay-human-approved-irreversible-step` | `alpha.share.open` | success | **a human approved the irreversible step through the console** |
| `13-replay-human-took-over-live-session` | `alpha.share.open` | success | **an operator took the live session and performed the step by hand**, then resumed (scripted; see below) |
| `14-discovery-live-llm-money-market` | — | **discovery** | a second real run, 13 turns, on a goal chosen after the first: it overruled the model twice |
| `15-replay-second-discovered-artifact` | `alpha.member.moneymarket` | success | the **second** discovered artifact, replayed for a member that run never saw |

## What is in a run directory

```
run.jsonl        every event, post-redaction, one JSON object per line
result.json      the contract the caller receives
capability.json  the tenant-resolved artifact that actually executed
screenshots/     00-entry, each step's postcondition, the success frame; always on failure
dom/             DOM snapshot, written on failure and on escalation only
interventions.json + human-actions/   when a human was involved
discovery/       discovery only: transcript.jsonl (the model conversation) and trace.json
```

Event types across these runs: `run.start` `step.start` `policy.decision` `target.resolved`
`action.performed` `checkpoint.evaluated` `outcome.matched` `recovery.applied`
`escalation.raised` `human.action` `human.resolved` `recorder.overruled` `recorder.synthesized`
`drift.summary` `step.end` `run.end`.

## Six things worth opening

**1. A failure says what it expected and what it saw.** From `06-replay-failed-surface-error-http-500/result.json`:

```json
{ "kind": "SURFACE_ERROR", "step_id": "s6",
  "step_description": "Retrieve the member record",
  "expected": "the application to return a usable page",
  "observed": "HTTP 500 from http://localhost:4599/t/alpha/search?q=1***7 — … APPLICATION ERROR - REF 0x5A2 …" }
```

Produced by the same evaluator that made the decision, so triage needs no screenshot. Note the
member number is already masked inside the URL.

**2. A recovered run looks like an ordinary success.** `05-replay-recovered-session-expiry/result.json` is a plain
`success`; only `run.jsonl` shows what happened:

```json
{"type": "recovery.applied", "step_id": "s6", "rule_id": "reauthenticate",
 "attempt": 1, "then_retry_step": true}
```

The session expired mid-flow, the artifact's declared recovery signed back on and returned to
the interrupted screen, and the caller was never told — because nothing went wrong from the
caller's point of view.

**3. Which locator rung won, on every step.** Each run ends with:

```json
{"type": "drift.summary", "steps": {"s1": 0, "s2": 0, "s3": 0, "s4": 0, "s5": 0, "s6": 0}}
```

`0` means the preferred strategy held. A step that starts resolving at rung 3 is a UI change
that has not broken anything *yet* — a leading indicator, free from the design.

**4. Redaction is structural, not cosmetic.** The approval run returned
`"reference_number": "SA-100987-0001"` to its caller, and the copy on disk reads
`"SA-1***7-0001"`: the reference number embeds the member number, which is a `pii` input. The
caller gets the real value; evidence never holds it. Grepping these directories for the demo
password finds nothing — redaction happens at the sinks, so nothing sensitive reaches disk by
construction.

**5. An operator took the session, and the run resumed without redoing the work.**
`13-replay-human-took-over-live-session` is the other half of the escalation story: instead of approving, the operator claimed
the intervention, clicked **Confirm** on the live page through the console, and handed control
back with `resume`. The operator here is `scripts/operator_takeover.py`, so that this run needs
nobody at a keyboard — the control lease, the console's HTTP and WebSocket routes and the action
record it goes through are the same ones a person uses. `human-actions/iv_abc9a788f2.json`:

```json
{"intervention_id": "iv_abc9a788f2",
 "human_actions": [{"kind": "click", "at": "2026-09-11T07:45:57.364804Z", "x": 35.3, "y": 218.5}],
 "resolution": {"decision": "resume", "note": "opened the share by hand on the live session"}}
```

and in `run.jsonl`:

```json
{"type": "human.resolved", "decision": "resume", "human_actions": 1,
 "resume_branch": "postcondition_satisfied", "human_wait_ms": 447}
```

`postcondition_satisfied` is the whole point: resume **re-evaluates** rather than continues, sees
the share already exists, and advances — so an irreversible step a human performed is never
performed twice. `drift.summary` for this run has no entry for `s8`, because `s8` never ran.

`stability.json` beside these directories is per-capability telemetry, not an index: it counts
every replay ever executed against the evidence root. It happens to sum to exactly the fourteen
replays committed here.

**6. The recorder overruling the model, in a committed run.**
`14-discovery-live-llm-money-market` is the whole thesis of `REPORT.md` §7 as evidence rather than
assertion. Thirteen turns against the same hostile frameset, on a goal picked after the first
recording existed. Two lines from its `run.jsonl`:

```json
{"type": "recorder.overruled", "what": "outputs.money_market_balance.transform",
 "why": "'none' cannot produce a currency; using currency_to_number"}
{"type": "recorder.overruled", "what": "steps[s5].value",
 "why": "literal '1***7' replaced by {{inputs.member_number}}"}
```

The model declared a `currency` output with no transform — type-correct at record time, broken on
*every* replay — and baked the member number it was handed into a step as a literal. Both were
rewritten mechanically; lint **L011** and **L012** would have rejected the artifact otherwise.

`capabilities/alpha.member.moneymarket.v1.0.0.json` is deliberately committed **as it came out**:
`approval: "draft"`, `curated: false`, and therefore **no declared business outcomes and no tenant
overrides** — one happy-path run never saw a "RECORD NOT FOUND" screen or a second tenant. Replay
it with `member_number=999999` and it fails a checkpoint where `alpha.member.balance` returns
`MEMBER_NOT_FOUND` with exit 0. That difference *is* the curation step, and it is why `invoke`
refuses a draft and `replay --allow-draft` exists as a separate flag.

## Reproducing

Every replay above runs offline with no API key. Only the discovery run needs one.
See the root `README.md` for the exact commands and their expected verdicts and exit codes.
