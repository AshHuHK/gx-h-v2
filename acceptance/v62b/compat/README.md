# Released-project compatibility and A regression

The V6.1 inputs are a paired-voltage CSV project and two public synthetic grid projects from the original A7 upgrade probe. The A input is a new instance of the accepted A1 module study on the two deposited coordinate files. The fixtures use a credential-free scripted local peer and real CPU Jobs. Restoring a saved deployment and state at their original physical paths exercises the ordinary native state marker, plugin pins and cold CLI.

`upgrade.py` reads each original admission and completion through the installed manifest reader, checks output bytes against the original artifact records, and runs two independent B CLI resumes. The paused V6.1 grid has two already launched Jobs whose completions await collection. For A, it takes the operator-supplied source files offline and compares the stored inputs, module, report, wake, graph, decision, Job, reservation and turn history across each resume. It restores the supplied files, original checkout bytes, SQLite backup, artifact directory and local loopback home, then reopens the project with the original release.

Run the following from a B checkout using its locked Python and the workspace Clash environment. `GX_A`, `GX_V61` and `GX_CLASH` point to the accepted A checkout, published V6.1 checkout and local Mihomo executable. `A_REVISION` and `B_REVISION` are full frozen Git commit IDs. `WORK` is an absolute path to a new directory outside the product checkouts. The original A tests use their installed runtime and retain raw evidence under `WORK`.

```bash
set -eu
B=$PWD
PY="$B/.venv/bin/python"
BIN="$B/vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
VERIFY="$B/acceptance/v62b/compat"
export PATH="$B/.venv/bin:$PATH" GX_CLASH GIT_OPTIONAL_LOCKS=0 PYTHONDONTWRITEBYTECODE=1
mkdir -pm 700 "$WORK"
loop() {
  receipt=$1
  shift
  "$PY" -B "$B/acceptance/phase2a/regression/loopback.py" \
    --clash "$GX_CLASH" --receipt "$receipt" -- "$@"
}
loop "$WORK/v61-seed-network.json" "$PY" -B "$GX_A/acceptance/v62a/upgrade/probe.py" seed \
  --baseline "$GX_V61" --binary "$BIN" --work "$WORK/v61-seed" --receipt "$WORK/v61-seed.json"
loop "$WORK/a-seed-network.json" "$PY" -B "$VERIFY/upgrade.py" seed-a \
  --release "$GX_A" --revision "$A_REVISION" --binary "$BIN" --work "$WORK/a-seed"
loop "$WORK/v61-check-network.json" "$PY" -B "$VERIFY/upgrade.py" check-v61 \
  --baseline "$GX_V61" --revision "$B_REVISION" --binary "$BIN" \
  --work "$WORK/v61-seed" --receipt "$WORK/v61-upgrade.json"
loop "$WORK/a-check-network.json" "$PY" -B "$VERIFY/upgrade.py" check-a \
  --revision "$B_REVISION" --work "$WORK/a-seed" --receipt "$WORK/a-upgrade.json"
loop "$WORK/a1-network.json" "$PY" -B "$B/acceptance/v62a/official_inventory.py" \
  --work "$WORK/a1" --binary "$BIN"
loop "$WORK/interleavings-network.json" "$PY" -B "$B/acceptance/v62a/interleavings.py" \
  --work "$WORK/interleavings" --case A2 --case A3 --case A4 --case A5 --case A6
"$PY" -B "$B/acceptance/v62a/interleavings.py" --work "$WORK/isolation" --case A7
"$PY" -B "$VERIFY/audit.py" --work "$WORK" --revision "$B_REVISION" \
  --a-verifier "$GX_A/acceptance/v62a/verify.py" --receipt "$WORK/a-regression.json"
```

A7 creates its own isolated localhost fixture. `audit.py` runs the released A acceptance verifier on the B receipts, then verifies its saved index in another process. It also compares each A runner's observed runtime fingerprint with an independent Git archive of the B commit and rechecks report artifact bytes, Job outcomes, budget settlements, A3's concurrent revisions, A4's successor lease and both A5 process exits against the stored projects. Provider tokens are unobserved. All reported native fixture usage comes from the scripted peer.
