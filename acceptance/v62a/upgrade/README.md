# Restoring released V6.1 projects

The A7 upgrade probe generates a paired-voltage CSV study through the published Goal CLI. Its official app-server receives two scripted answers from a local Responses fixture and executes a single CPU Job. It also seeds two public, synthetic `science_grid@3` projects with two real CPU Jobs each. One starts at V6.1. The other starts at V6, resumes under V6.1, and pauses before its second upgrade. No historical private project state or model account is used.

Each release is installed at the same physical application path. The probe retains the run-specific operator marker, plugin and workflow pins, artifact contents, original Jobs, reservations and usage. It checks multiple store migration receipts in chronological order for the V6 project. Each candidate project undergoes two writable resumes in distinct processes. The V6.1 code, complete old states and the original local native home are then restored at their original paths and read again. A separate, newly opened default quick Goal runs through the same published V6.1 CSV peer with the new code. The peer asserts the historical native tool catalog, empty initial observations and conditional next turn.

Run in the product root with a V6.1 checkout at `c4c5b66a55739b38b34684360810d05f18f9a21a`, including its locked offline dependencies. Set `GX_BASE` to that checkout and `GX_CLASH` to the deployed Clash binary. Export the standard workspace proxy environment. The localhost fixture runs inside an isolated network namespace with the same proxy addresses. The fresh private directory must sit outside both product checkouts.

```bash
source .venv/bin/activate
trial=$(mktemp -d)
binary="$GX_BASE/vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
python -B "$GX_BASE/acceptance/phase2a/regression/loopback.py" \
  --clash "$GX_CLASH" --receipt "$trial/network.json" -- \
  python -B acceptance/v62a/upgrade/probe.py all --baseline "$GX_BASE" \
  --binary "$binary" --work "$trial/work" --receipt "$trial/upgrade.json"
python -B acceptance/v62a/upgrade/test_pin_chain.py -v
```

The `seed` phase can run ahead of candidate freeze; `check` reopens a seeded trial at its unchanged absolute path. A second check uses new execution directories and the same verified old states. Source checkout identity and any relevant local code changes are recorded. The compact receipt retains artifact digests, identity and charge fields for every Job and turn. Provider tokens are unobserved. The 70 reported tokens per CSV execution originate entirely from the scripted loopback. [Original V6.1 suite case IDs and log hashes](v61-regression.json)
