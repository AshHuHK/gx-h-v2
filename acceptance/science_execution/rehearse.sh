#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd -P)
if [[ ${1:-} == _inside ]]; then
    out=$2
    cd "$repo"
    cli() { .venv/bin/python -B acceptance/science_execution/diagnostic.py "$@" --host offline; }
    cli preflight > "$out/preflight.json"
    cli register --work "$out/run" > "$out/register.json"
    for slot in 01 02; do
        cli run --work "$out/run" --slot "$slot" > "$out/$slot-run.json"
        cli reconcile --work "$out/run" --slot "$slot" > "$out/$slot-reconcile.json"
        cli resume --work "$out/run" --slot "$slot" > "$out/$slot-resume.json"
    done
    cli status --work "$out/run" > "$out/status.json"
    cli export --work "$out/run" > "$out/export.json"
    .venv/bin/python -B acceptance/science_execution/recovery_check.py --work "$out/cold-run" > "$out/recovery.json"
    .venv/bin/python -B acceptance/science_execution/recovery_check.py --expired --work "$out/expired-run" > "$out/expired.json"
    .venv/bin/python -B -m unittest -v \
      acceptance.v623.test_live_funding.Physical.test_missing_bills_retain_exposure_linked_late_receipt_no_duplicate \
      acceptance.v623.test_oauth_usage.OauthUsage.test_real_ledger_parent_child_late_terminal_then_cold_replay \
      acceptance.v623.test_oauth_usage.OauthUsage.test_interrupted_after_last_keyed_response_repeated_count_cannot_close_bill \
      acceptance.v622.test_close.CloseFreshness.test_report_after_preparation_is_delivered_on_cold_restart \
      acceptance.v622.test_public_score.PublicActionScore.test_direct_maintain_and_authenticated_report \
      > "$out/controls.log" 2>&1
else
    out=$(realpath -m "${1:?supply a new external output directory}")
    clash=$(realpath "${2:?supply the installed Clash binary}")
    test ! -e "$out"
    mkdir -m 700 -p "$out"
    cd "$repo"
    .venv/bin/python -B acceptance/phase2a/regression/loopback.py --clash "$clash" \
        --receipt "$out/network.json" -- bash "$repo/acceptance/science_execution/rehearse.sh" _inside "$out"
    .venv/bin/python -B acceptance/science_execution/replay_receipt.py "$out" > "$out/verification.json"
fi
