# Understudy

**An LLM works out a legacy back-office flow once. A typed artifact performs it every time after
that — with no model in the loop.**

Hence the name: the understudy watches the lead perform the role once, then plays that part
every night, identically, without the star in the building.

```
goal ──▶ LLM drives the real UI ──▶ capability artifact ──▶ deterministic replay ──▶ typed outputs
              (once, expensive)         (typed, reviewed)      (no model, ~600ms)
                     │                                              │
                     └──────────── stuck? ──▶ a human takes the live session ◀──┘
```

The problem this is for: banks and credit unions run a long tail of back-office applications
with no API, where the only way in is to drive the UI the way an operator would. Doing that with
a model on every invocation is slow, expensive and non-reproducible. So the model is used exactly
once, to *discover*; what it learned becomes a reviewable, parameterised capability that an AI
agent can call by name.

---

## Setup

Requires Python 3.12+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
```

**Everything below runs offline except the discovery run.** Only `understudy discover` needs a
model. For that, copy `.env.example` to `.env` and add a key:

```bash
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=<from https://aistudio.google.com/apikey — free, no card>
LLM_MODEL=gemini-3.5-flash
```

Any OpenAI-compatible chat-completions endpoint with tool calling works; a provider swap is a
base URL and a model name. `.env.example` carries a commented Groq block — the committed
discovery evidence was in fact recorded through it (see *Provider notes* below).

`APP_OPERATOR` / `APP_PASSWORD` are the target app's demo credentials. Artifacts reference them
by **name** only; values are read from the environment at substitution time and never written to
an artifact, a log or a screenshot.

---

## The demo path

One command runs the whole offline thread with its expected verdict printed before each step:

```bash
./scripts/demo.sh
```

Or step through it yourself. Start the target application first:

```bash
python -m understudy app            # http://localhost:4599
```

### 1. Discovery — the model drives the real UI (needs a key)

```bash
python -m understudy discover \
  --goal "Sign on to the member services console, look up member 100234, and read their name and their Regular Shares balance" \
  --url http://localhost:4599/t/alpha/ \
  --secret APP_OPERATOR --secret APP_PASSWORD \
  --input member_number=100234 \
  --name alpha.member.lookup --app harborline-cu --tenant alpha
```

→ writes `capabilities/alpha.member.lookup.v1.0.0.json` (a **draft**) and an evidence directory
holding the full model transcript. Roughly 13–17 model turns.

### 2. Replay — the same flow, no model, a member the model never saw

```bash
python -m understudy replay capabilities/alpha.member.lookup.v1.0.0.json \
  --input member_number=100987 --allow-draft
```

```
success: outputs {"member_name": "Hopper, Grace", "regular_shares_balance": 310.42}      exit 0
```

That is the whole thesis in one line: recorded against Ada Lovelace, replayed for Grace Hopper,
because the balance is anchored on *row "Regular Shares" × column "Balance"* rather than on
anything that run happened to see.

### 3. Every arm of the result contract

Using the curated `alpha.member.balance` capability (`CAP=capabilities/alpha.member.balance.v1.0.0.json`):

| Command | Expected | Exit |
|---|---|---|
| `replay $CAP --input member_number=100987` | `success: outputs {…}` | **0** |
| `replay $CAP --input member_number=999999` | `business_outcome MEMBER_NOT_FOUND (legitimate answer; exit 0)` | **0** |
| `replay $CAP --input member_number=100234 --inject maintenance` | `success` — 503 interstitial dismissed | **0** |
| `replay $CAP --input member_number=100234 --inject session_expired` | `success` — 440 timeout, signed back on, resumed | **0** |
| `replay $CAP --input member_number=100234 --inject app_error` | `failed SURFACE_ERROR at s6: expected … observed HTTP 500` | **1** |
| `replay $CAP --input member_number=100234 --inject slow` | `success` — 4s stall absorbed by the poll | **0** |
| `replay $CAP --input member_number=100234 --tenant beta` | `success` — same artifact, different tenant skin | **0** |

`--inject` arms a fault in the target app on the browser's own session, immediately before the
step that will meet it. It is a property of the *demo*, not of the engine.

### 4. A risky action, and a human in the loop

`alpha.share.open` ends in an irreversible "Confirm". With nobody attached it refuses to guess:

```bash
python -m understudy replay capabilities/alpha.share.open.v1.0.0.json \
  --input member_number=100987 --input deposit=50
# escalated RISKY_ACTION_APPROVAL at s8: intervention iv_…                                exit 2
```

Attach a human and it parks instead, holding the live session open:

```bash
python -m understudy replay capabilities/alpha.share.open.v1.0.0.json \
  --input member_number=100987 --input deposit=50 --console
# operator console: http://127.0.0.1:7373
```

Open that URL. You get the run's event timeline, the approval card, and a live frame of the
**same browser page the automation was driving** — click it, type into it, then Approve, Resume
or Abort. Approving completes the run and returns `{"reference_number": "SA-100987-0001"}`; the
share really exists afterwards. Aborting leaves the transaction unperformed, which is asserted
against the live application in `tests/test_escalation.py`, not merely against a status field.

Its business outcomes are reachable too:

```bash
replay …/alpha.share.open.v1.0.0.json --input member_number=100987 --input deposit=10   # DEPOSIT_TOO_SMALL, exit 0
replay …/alpha.share.open.v1.0.0.json --input member_number=103001 --input deposit=50   # PERMISSION_DENIED, exit 0
```

### 5. The agent-facing surface

```bash
python -m understudy catalog list
python -m understudy catalog tools          # JSON-Schema tool definitions, drafts excluded
python -m understudy invoke alpha.member.balance --input member_number=100987
```

`catalog tools` emits exactly what a tool-calling agent needs, derived from the artifact's own
typed inputs — no second definition to keep in sync. `invoke` is the agent path and **refuses a
draft outright**; `replay --allow-draft` is a human deliberately testing a recording.

---

## Running without live services

Everything except step 1 is offline: no network, no API key. The target application is part of
the repo, the test suite drives a real Chromium against it on an ephemeral port, and the
committed `evidence/` shows what each command produced when it was run for real.

```bash
python -m pytest -q            # 286 tests, ~95s
```

`tests/test_invariants.py` fails the build if anything under `replay/` imports an LLM SDK or the
`discover/` package — the "no model in replay" claim is enforced by the build, not by prose.

---

## The target application

`target_app/` is a deliberately hostile stand-in for a credit-union back office, served at
`/t/{tenant}/`. Two tenants run the **same vendor product**, configured and branded differently:

- **`alpha` — HARBORLINE CU, Member Services Console v3.7.2.** A real `<frameset>`, layout
  tables, `<font>` tags, no test IDs, no ARIA, and **no `<label for>`** — a field's only
  human-readable identity is the text in the adjacent `<td>`. Form fields are named `u`, `p`, `q`.
  A hidden per-form `_token` means HTTP-level scripting cannot work; you must drive a browser.
- **`beta` — CEDARVALE FCU, Account Servicing v6.1.** Same routes, same data, no frameset, divs
  instead of tables, "Account Number" instead of "Member No.", "Search" instead of "Retrieve".

Every one of those strings is something a naive recording would bake in. One artifact plus a
~20-line override replays on both.

Fault injection covers each class of runtime condition — `not_found`, `validation`, `permission`,
`maintenance` (503), `session_expired` (440), `app_error` (500), `slow`, `error_rate` — scoped to
a session cookie, never process-global. Uninjected faults exist too: an unknown member, a
restricted member a teller may not view, a deposit below the minimum.

---

## Layout

```
src/understudy/
  schema/     the contract: capability, locator ladder, checkpoints, results, lint
  surface/    the seam — a Protocol importing no Playwright, plus the web implementation
  policy/     allowlist, risk classification, redaction
  evidence/   structured log, run directory, stability sidecar
  replay/     the production path: executor, checkpoints, extraction, tenant merge
  discover/   the model client, the tool definitions, the loop, the recorder
  console/    one FastAPI app and one HTML page: run timeline + live-session takeover
  catalog/    name@version catalog and the JSON-Schema tool export
config/policy.toml    operator-side policy — the security boundary
capabilities/         the artifacts
evidence/             committed runs; see evidence/README.md
```

~6,200 lines of source, ~900 for the target app, ~3,700 of tests.

---

## What is mocked, and what is real

**Real:** the discovery run (a genuine model driving a genuine browser — the committed transcript
is the proof), deterministic replay, the locator ladder, the policy gate, redaction, the
control-transfer model, and every result in `evidence/`.

**Mocked, deliberately:** the *target application* stands in for a bank's back office — the brief
rules out using a real one. The operator console's live view is a JPEG frame pushed over a
WebSocket with coordinate and keystroke forwarding, not a co-browsing stack; production wants CDP
screencast or WebRTC. **The control-transfer model underneath it is real and tested** — the human
drives the same `Page` object the automation was using, and automation's next action raises
`ControlLost` while they hold it.

**Not built**, with the seam left clean: a desktop surface (the `Surface` Protocol is that seam —
`observe` → platform accessibility API, `resolve` → the same descriptor ladder, `act` →
synthesised input), queues, workers, containers, and any authentication on the console, which
binds to loopback only. `REPORT.md` §7 lists the cuts and what comes next.

---

## Provider notes

Free tiers are rate-limited in ways worth knowing before a demo:

- **Gemini free tier is 20 requests per day *per model*.** A 13-turn discovery burns 13. Switching
  `LLM_MODEL` gets a fresh allowance.
- **Gemini 3.x** requires each tool call's `thought_signature` echoed back on the following turn.
  A client that rebuilds the assistant message from name and arguments alone gets a 400 on turn
  two, so `ToolCall.passthrough` carries provider fields through verbatim.
- **Some providers are text-only** and reject a multipart content array. The client detects that
  and retries without the screenshot part: perception is the accessibility-derived observation,
  so losing the screenshot degrades a run rather than ending it.

All three were found by running against real providers, not by testing. `REPORT.md` §7 covers
what else the real runs changed.
