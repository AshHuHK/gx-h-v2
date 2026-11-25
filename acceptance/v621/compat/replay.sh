#!/usr/bin/env bash
# A clean B installation, two candidate reopens, and full rollback at one physical path.
set -eu
acceptance_root=$(realpath "$(dirname "$0")/../../..")
trial=$(realpath -m "${1:?usage: replay.sh FRESH_EXTERNAL_DIRECTORY CANDIDATE_SHA}")
candidate=${2:?supply the exact candidate commit}
base=321b3b9542e986e749b4eab08495b288d09ba4be
if [[ -e "$trial" || "$trial" == "$acceptance_root" || "$trial" == "$acceptance_root/"* ]]; then
    echo 'Use a fresh directory outside the product checkout.' >&2
    exit 2
fi
git -C "$acceptance_root" merge-base --is-ancestor "$base" "$candidate"
baseline_assets=${GX_BASE:-"$acceptance_root/../product-v6.2-release"}
python="${GX_PYTHON:-$acceptance_root/.venv/bin/python}"
binary="${GX_CODEX_BINARY:-$baseline_assets/vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex}"
clash="${GX_CLASH:-$acceptance_root/../../../clash/clash.new}"
mkdir -m 700 -p "$trial"
git clone --no-checkout --shared --quiet "$acceptance_root" "$trial/release"
git -C "$trial/release" checkout --quiet --detach "$base"
cp -a "$baseline_assets/gx-h/runtime/plugin-host/node_modules" "$trial/release/gx-h/runtime/plugin-host/"
export GIT_OPTIONAL_LOCKS=0 PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$trial/release:$trial/release/gx-h"
loop() {
    local result=$1
    shift
    "$python" -B "$acceptance_root/acceptance/phase2a/regression/loopback.py" \
        --clash "$clash" --receipt "$trial/$result-network.json" -- "$@"
}
replay="$acceptance_root/acceptance/v621/compat/replay.py"
loop original "$python" -B "$trial/release/acceptance/v62b/engineering/run.py" \
    --work "$trial/old" --arm change --binary "$binary"
loop capture "$python" -B "$replay" capture --work "$trial"
git -C "$trial/release" checkout --quiet --detach "$candidate"
loop check "$python" -B "$replay" check --work "$trial" --candidate "$candidate"
git -C "$trial/release" checkout --quiet --detach "$base"
loop rollback "$python" -B "$replay" rollback --work "$trial"
