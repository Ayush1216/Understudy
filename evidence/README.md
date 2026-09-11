# Evidence

Thirteen committed runs: one genuine LLM-driven discovery, and twelve deterministic replays
covering every arm of the result contract. Every run here was produced by the commands in the
root `README.md`; nothing is hand-written.

## The contrast the design is built around

Put these two side by side. They are the reason the result contract has four arms instead of
"worked / threw":

| Run | Verdict | Exit | Who deals with it |
|---|---|---|---|
| `replay-20260911-035314-1af3cc` | `business_outcome` `MEMBER_NOT_FOUND` | **0** | the calling agent — "no such member" is an answer |
| `replay-20260911-035318-f0884f` | `failed` `SURFACE_ERROR` | **1** | a human — the application broke |

Both "did not return a balance". Only one is a failure. Conflating them is the mistake the
brief's own glossary warns about, so the artifact *declares* its business outcomes and replay
checks them before it ever considers a step failed.

## Every run

| Directory | Capability | Verdict | Notes |
|---|---|---|---|
| `discover-20260911-033837-f203df` | — | **discovery** | the real LLM run: 17 turns, 8 screenshots, full redacted transcript |
| `replay-20260911-035310-b5e06f` | `alpha.member.lookup` | success | **the discovered artifact**, replayed for a member the model never saw |
| `replay-20260911-035312-4d5fa8` | `alpha.member.balance` | success | happy path |
| `replay-20260911-035314-1af3cc` | `alpha.member.balance` | `MEMBER_NOT_FOUND` | declared business outcome, exit 0 |
| `replay-20260911-035315-ae152a` | `alpha.member.balance` | success | 503 maintenance interstitial, **recovered** |
| `replay-20260911-035317-89683d` | `alpha.member.balance` | success | 440 session timeout, **re-authenticated and resumed** |
| `replay-20260911-035318-f0884f` | `alpha.member.balance` | `SURFACE_ERROR` | HTTP 500, exit 1, + DOM snapshot |
| `replay-20260911-035320-bddae8` | `alpha.member.balance` | success | 4-second stall absorbed by the checkpoint poll |
| `replay-20260911-035325-9d9a69` | `alpha.member.balance` | success | **tenant `beta`** — same artifact, different skin |
| `replay-20260911-035327-bb9f33` | `alpha.share.open` | `escalated` | irreversible step, no human attached, exit 2 |
| `replay-20260911-035328-b971e6` | `alpha.share.open` | `DEPOSIT_TOO_SMALL` | the application refused the deposit |
| `replay-20260911-035329-1935e5` | `alpha.share.open` | `PERMISSION_DENIED` | restricted member, teller lacks authority |
| `replay-20260911-035330-b2d2bc` | `alpha.share.open` | success | **a human approved the irreversible step through the console** |

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
`escalation.raised` `human.resolved` `recorder.overruled` `recorder.synthesized`
`drift.summary` `step.end` `run.end`.

## Four things worth opening

**1. A failure says what it expected and what it saw.** From `…-f0884f/result.json`:

```json
{ "kind": "SURFACE_ERROR", "step_id": "s6",
  "step_description": "Retrieve the member record",
  "expected": "the application to return a usable page",
  "observed": "HTTP 500 from http://localhost:4599/t/alpha/search?q=1***7 — … APPLICATION ERROR - REF 0x5A2 …" }
```

Produced by the same evaluator that made the decision, so triage needs no screenshot. Note the
member number is already masked inside the URL.

**2. A recovered run looks like an ordinary success.** `…-89683d/result.json` is a plain
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

## Reproducing

Every replay above runs offline with no API key. Only the discovery run needs one.
See the root `README.md` for the exact commands and their expected verdicts and exit codes.
