#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "$0")/../../.." && pwd -P)
work=$(realpath -m "${1:?supply a fresh external directory}")
clash=$(realpath "${2:?supply the installed Clash executable}")
suite=${3:-standard}
cd "$repo"
test ! -e "$work"
mkdir -m 700 -p "$work"
.venv/bin/python -B acceptance/phase2a/regression/loopback.py --clash "$clash" \
    --receipt "$work/network.json" -- "$repo/.venv/bin/python" -B acceptance/v622/public/batch.py \
    --work "$work/run" --suite "$suite" --host offline
.venv/bin/python -B acceptance/v622/public/rate.py --work "$work/run" --stage opportunity
.venv/bin/python -B acceptance/v622/public/rate.py --work "$work/run" --stage outcome
.venv/bin/python -B acceptance/v622/public/check.py --work "$work/run"
