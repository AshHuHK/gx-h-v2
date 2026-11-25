# Replaying previous GX releases

The original V6.2.1 checkout is `e5b7ee977ec33854362bc2440d8f9249464aed20`. Its indexed product candidate is `83e8f34d2886a5c4cedc966ec4a7dd7b2addf691`. The offline acceptance index and public pilot each verify their original protocol and candidate in their native release.

## Inherited tests and official peer replay

In the clean, provisioned candidate checkout, select a fresh output directory and the actual local Clash executable. The launcher creates private homes and a separate reject-only network namespace for each command. The original A1, B4 and V6.2C public peers explicitly select their original evidence policy on the normal Goal CLI. The test checks the persisted policy and the historical research classification in every fresh Run.

```bash
.venv/bin/python -B acceptance/v622/compat/regression.py \
    --work "$work/inherited" --clash "$clash"
```

The output records actual argv, return code, network receipt and log digest for every invocation, including unsuccessful checks. The `discovery/results` group includes individual test names, skip reasons and elapsed times. The `c4` group executes the fixed four peer policies. Its fixtures use the official Codex App Server and local scripted responses, with two-stage scoring and access-parity checks. V6.2.2's interpreted science and paid-readiness decisions use the separately registered new-policy cohort.

## Same-path upgrade and rollback

Run the following with `repo` set to the provisioned candidate checkout, `candidate` set to its full commit, `work` set to a fresh external directory, and `clash` set to the absolute Clash executable. Every phase needs a new process. Preserve the source clone at the same absolute path.

```bash
mkdir -m 700 "$work"
git clone --shared --no-checkout "$repo" "$work/install"
git -C "$work/install" checkout --detach e5b7ee977ec33854362bc2440d8f9249464aed20
cp -a "$repo/vendor" "$repo/.venv" "$work/install/"
cp -a "$repo/gx-h/runtime/plugin-host/node_modules" "$work/install/gx-h/runtime/plugin-host/"
py="$work/install/.venv/bin/python"
probe="$repo/acceptance/v622/compat/migration.py"
offline() {
    receipt=$1
    shift
    "$py" -B "$repo/acceptance/phase2a/regression/loopback.py" \
        --clash "$clash" --receipt "$work/$receipt-network.json" -- "$py" -B "$@"
}
offline old "$work/install/acceptance/v62c/rehearse.py" --slot 02 --work "$work/old-run"
offline old-score "$work/install/acceptance/v62c/rate.py" --work "$work/old-run"
offline capture "$probe" capture --work "$work"
git -C "$work/install" checkout --detach "$candidate"
offline check "$probe" check --work "$work" --candidate "$candidate"
git -C "$work/install" checkout --detach e5b7ee977ec33854362bc2440d8f9249464aed20
offline rollback "$probe" rollback --work "$work"
```

The old deployment creates a real delegated public project, two CPU Jobs and two accepted claims. The capture reads original artifact bytes and freezes the scientific decisions, admission and completion manifests, published report, actual native-turn records and budgets. The candidate cold-resumes the same closed Run twice, replays its exact native report twice and checks all those records. Rollback restores a SQLite backup and its original files to the same path; the candidate state remains available next to it.

Reconstructing the original published V6.2C score uses its read-only archive in a new directory and the original `acceptance/v62c/rate.py`. Compare the resulting `scores.json` byte-for-byte with `acceptance/v62c/receipts/scores.json`; report published scores and fresh peer results separately.
