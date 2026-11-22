#!/usr/bin/env bash
# Fetch only the locked tag. Every Git network operation inherits the package Clash route.
set -euo pipefail
dest="${1:-vendor/codex}"
repo_url="https://github.com/openai/codex.git"
tag="rust-v0.169.0"
expected="d32063f96c2dff1dc96af839b3952abb6acfeda4"
package_root=$(cd "$(dirname "$0")/.." && pwd -P)
if [[ -e "$dest" ]]; then
    echo "Destination exists: $dest" >&2
    exit 2
fi
mkdir -p "$(dirname "$dest")"
PYTHONPATH="$package_root/gx-h" python3 -B - "$tag" "$repo_url" "$dest" <<'PY'
import subprocess
import sys
from gx_core.p0.profiles import clash_env
tag, url, dest = sys.argv[1:]
env = clash_env()
subprocess.run(["git", "-c", "http.proxy=" + env["HTTPS_PROXY"], "clone", "--depth", "1",
                "--single-branch", "--branch", tag, "--no-checkout", url, dest], env=env, check=True)
PY
actual=$(git -C "$dest" rev-parse "refs/tags/$tag^{commit}")
if [[ "$actual" != "$expected" ]]; then
    echo "Pinned tag mismatch: expected $expected, observed $actual" >&2
    exit 3
fi
git -C "$dest" checkout --detach "$actual"
echo "Codex source baseline verified at $actual"
