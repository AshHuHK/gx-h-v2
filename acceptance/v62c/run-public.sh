#!/usr/bin/env bash
# Arguments: a fresh absolute result directory, optional path to the local Clash binary.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd -P)
work=$(realpath -m "${1:?pass a fresh directory outside the checkout}")
clash=$(realpath "${2:-$repo/../../../clash/clash.new}")
cd "$repo"
test ! -e "$work"
mkdir -m 700 -p "$work"
.venv/bin/python -B acceptance/phase2a/regression/loopback.py --clash "$clash" \
    --receipt "$work/network.json" -- "$repo/.venv/bin/python" -B acceptance/v62c/rehearse.py --work "$work/run"
.venv/bin/python -B acceptance/v62c/rate.py --work "$work/run" --stage opportunity
.venv/bin/python -B acceptance/v62c/rate.py --work "$work/run" --stage outcome
.venv/bin/python -B acceptance/v62c/parity.py --work "$work/run"
