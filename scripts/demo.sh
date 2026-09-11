#!/usr/bin/env bash
# The manual demo path from the README, end to end, offline (no model, no API key).
# Each command's expected verdict and exit code is stated before it runs; exit 1 and 2 are
# expected where a failure or an escalation is the point being demonstrated.
set -euo pipefail
cd "$(dirname "$0")/.."

CAP=capabilities/alpha.member.balance.v1.0.0.json
SHARE=capabilities/alpha.share.open.v1.0.0.json
BASE=http://localhost:4599

if ! curl -fsS "$BASE/" >/dev/null 2>&1; then
  python -m understudy app &
  APP=$!
  trap 'kill $APP 2>/dev/null || true' EXIT
  for _ in $(seq 50); do curl -fsS "$BASE/" >/dev/null 2>&1 && break; sleep 0.2; done
fi

# Fresh data: an approved share-open (the interactive step) gives 100234 a second "Regular
# Shares" row, and a balance read that then matches two rows refuses rather than guesses.
curl -fsS -X POST "$BASE/_reset" >/dev/null

run() {  # run "<expected verdict>" <expected exit> <command...>
  echo; echo "== expect: $1  (exit $2)"; shift 2
  echo "   $*"
  code=0; "$@" >/dev/null || code=$?
  echo "-- exit $code"
}

run "success: outputs {member_name, primary_share_balance}"                     0 python -m understudy replay $CAP --input member_number=100987
run "business_outcome MEMBER_NOT_FOUND (legitimate answer; exit 0)"            0 python -m understudy replay $CAP --input member_number=999999
run "success — maintenance interstitial dismissed (recovery.applied in run.jsonl)" 0 python -m understudy replay $CAP --input member_number=100234 --inject maintenance
run "success — session expiry re-authenticated (recovery.applied in run.jsonl)"   0 python -m understudy replay $CAP --input member_number=100234 --inject session_expired
run "failed SURFACE_ERROR at s6: expected ... observed HTTP 500 (exit 1)"       1 python -m understudy replay $CAP --input member_number=100234 --inject app_error
run "success on tenant beta — same artifact, other skin"                        0 python -m understudy replay $CAP --input member_number=100234 --tenant beta
run "escalated RISKY_ACTION_APPROVAL at s8 — nobody there to approve Confirm"   2 python -m understudy replay $SHARE --input member_number=100234 --input deposit=50
run "business_outcome DEPOSIT_TOO_SMALL (legitimate answer; exit 0)"           0 python -m understudy replay $SHARE --input member_number=100234 --input deposit=10
run "business_outcome PERMISSION_DENIED (legitimate answer; exit 0)"           0 python -m understudy replay $SHARE --input member_number=103001 --input deposit=50

echo
echo "== interactive: escalation with a human takeover. --console mounts the operator UI on the"
echo "   run's own event loop and prints its URL; the run parks on the irreversible Confirm until"
echo "   you claim the intervention and approve (or abort) it there. Exit 2 if nobody answers."
echo "   python -m understudy replay $SHARE --input member_number=100234 --input deposit=50 --console"
