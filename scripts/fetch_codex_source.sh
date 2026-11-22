#!/usr/bin/env bash
set -euo pipefail

DEST="${1:-vendor/codex}"
REPO="https://github.com/openai/codex.git"
TAG="rust-v0.169.0"
EXPECTED_PREFIX="d32063f9"

if [[ -e "$DEST/.git" ]]; then
  echo "Destination already contains a git checkout: $DEST" >&2
  exit 2
fi

mkdir -p "$(dirname "$DEST")"
git clone --filter=blob:none --no-checkout "$REPO" "$DEST"
git -C "$DEST" fetch --depth 1 origin "refs/tags/$TAG:refs/tags/$TAG"
git -C "$DEST" checkout --detach "$TAG"

ACTUAL="$(git -C "$DEST" rev-parse HEAD)"
echo "Codex checkout: $ACTUAL"
if [[ "$ACTUAL" != "$EXPECTED_PREFIX"* ]]; then
  echo "ERROR: expected commit prefix $EXPECTED_PREFIX for $TAG, got $ACTUAL" >&2
  echo "Do not silently proceed. Compare with the supplied architecture baseline." >&2
  exit 3
fi

echo "Codex source baseline verified."
