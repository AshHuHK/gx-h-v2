# V6.2.1 public acceptance

The [release index](receipts/index.md) identifies the tested source commit, the result of each check, and its underlying command log and trace. Its [machine-readable counterpart](receipts/index.json) includes the relevant Run and event IDs, artifact and manifest digests, exact command groups, and any unverified scope. Both are generated after the source commit is frozen. Receipt-only commits can therefore verify the same source.

## Verify a release

From the repository root, with Git history through `321b3b9542e986e749b4eab08495b288d09ba4be`, run:

```bash
python3 -B acceptance/v621/index.py verify
python3 -B acceptance/v621/index.py verify --require-complete
```

The first command re-reads the published files, verifies the candidate contract and protocol against Git, checks the old protocol against the baseline, checks the scientific trace links, and compares both index files byte for byte. The second additionally requires every public engineering row to pass. Paid public and private study rows remain prospective. A failed engineering trial or missing trace remains visible in both formats.

## Repeat a focused check

Use the release's locked Python environment, Cordis dependency and installed scientific packages. The following commands assume the repository root and a fresh external scratch directory. The test fixtures use local scripted replies. The official-host check below also requires the locked Codex binary and the release's loopback-only network runner.

```bash
export PYTHONPATH="$PWD:$PWD/gx-h"
py="$PWD/.venv/bin/python"
work=$(mktemp -d)
export GX_V621_CLAIM_RECEIPTS="$work/claim-receipts"
export GX_GAP_REGRESSION_RECEIPT="$work/gap-after.json"
export GX_GAP_REFUSAL_RECEIPT="$work/gap-mixed-refusal.json"
export GX_GAP_MATCH_RECEIPT="$work/gap-two-matches.json"
export GX_V621_ANALYSIS_GAP_RECEIPT="$work/gap-acquired-analysis.json"
export GX_V621_METRICS_TRACE="$work/metrics-trace.json"
export GX_NAMED_OUTPUT_RECEIPT="$work/named-receipt.json"
export GX_V621_ASSIGNMENT_RECEIPT="$work/assigned-job.json"
export GX621_GOAL_TRACE="$work/goal-trace.json"
export GX_GATE_TRACE="$work/gate-trace.json"
```

Run the modules of interest individually or together. Each path corresponds to a numbered work package in the index.

```bash
"$py" -B -m unittest -v acceptance.v621.test_claim_publication gx-h/tests/test_v621_coordinate_sources.py
"$py" -B -m unittest -v acceptance.v621.test_gap_resolution acceptance.v621.test_verified_metrics gx-h/tests/test_v621_named_outputs.py
"$py" -B -m unittest -v acceptance.v621.test_material_assignment
"$py" -B acceptance/v621/lead_capabilities.py --work "$work/arms"
"$py" -B -m unittest -v acceptance.v621.test_goal_versions acceptance.v621.test_evidence_gate acceptance.v621.test_contract
"$py" -B -m unittest -v acceptance.v62c.test_scoring acceptance.v62c.test_spending
"$py" -B acceptance/v62c/spending.py > "$work/spend-preflight-probe.json"
```

The [upgrade and rollback instructions](compat/README.md) recreate the original public Run under its released code. Use the exact tested candidate SHA for the second argument to `replay.sh`. The documented V6.2A and V6.2B regression commands exercise their original scientific checks. [V6.2A](../../V62A_ACCEPTANCE.md) · [V6.2B](../../V62B_ACCEPTANCE.md)

To run the four public slots through the official App Server, reuse the release's locked Codex executable and local Clash binary. Set `GX_CODEX_BINARY` and `GX_CLASH` to their local paths.

```bash
"$py" -B acceptance/phase2a/regression/loopback.py \
  --clash "$GX_CLASH" --receipt "$work/network.json" -- \
  "$py" -B acceptance/v62c/rehearse.py --work "$work/four-slots" --binary "$GX_CODEX_BINARY"
"$py" -B acceptance/v62c/rate.py --work "$work/four-slots" --output "$work/ratings"
```

The frozen raw-input manifest, two-stage rating and expected scope are defined by the [public protocol](../v62c/protocol.json) and [scoring instructions](../v62c/SCORING.md). Four scripted completions exercise the execution and evidence paths. A behavioral claim about a real model requires independently enrolled model opportunities.

## Publish a new receipt set

Archive each completed command's output and capture its actual argv, numeric exit code, and the candidate SHA. Put these facts in `receipts/commands.json`. Each group has an `argv` array, `exit_code`, and repository-relative `log` path. Include relevant environment overrides in `env` and any recorded skip impact in `limitations`. A group can cover additional group names or individual case IDs via `covers`. The groups are `repairs`, `catalog`, `scoring`, `upgrade`, `v6`, `v62a`, `v62b`, `pilot` and `rating`. A top-level `paths` object can map a logical receipt filename to its release-relative published path. Never put credentials, private study inputs, or live transcripts in the published record.

```bash
"$py" -B acceptance/v621/index.py build --source "$candidate"
"$py" -B acceptance/v621/index.py verify --require-complete
```

If a receipt or invocation is unavailable, keep the row as `NOT_RUN`. A command error or scientific mismatch records `FAIL`. Per-run IDs change on re-execution. Acceptance compares a fresh run's events and hashes within that run.
