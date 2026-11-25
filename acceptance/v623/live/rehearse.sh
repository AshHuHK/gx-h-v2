#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/../../.." && pwd -P)
case ${1:-} in
  _inside)
    out=$2
    cd "$repo"
    run_cli() { .venv/bin/python -B acceptance/v623/live/pilot.py "$@"; }
    run_cli register --host offline --work "$out/run" > "$out/register.json"
    for n in 01 02 03 04; do
      run_cli run --host offline --work "$out/run" --slot "$n" > "$out/$n.json"
    done
    .venv/bin/python -B acceptance/v623/live/rehearsal_check.py --work "$out/run" > "$out/verification.json"
    ;;
  *)
    out=$(realpath -m "${1:?supply an external new absolute output directory}")
    clash=$(realpath "${2:?supply the installed Clash binary}")
    test ! -e "$out"
    mkdir -p -m 700 "$out"
    cd "$repo"
    .venv/bin/python -B acceptance/phase2a/regression/loopback.py --clash "$clash" \
      --receipt "$out/network.json" -- bash "$repo/acceptance/v623/live/rehearse.sh" _inside "$out"
    ;;
esac
