# Independent coordinate verification

Run from the checkout root with Python, NumPy and Biopython. Supply a local GX state directory containing `gx.db` and its `artifacts/` tree. Add `--run-id` when the state contains multiple runs. Choose an evidence directory outside the research workspace and keep it out of model inputs.

```sh
python acceptance/multi_workflow/independent/live_verify.py --state <gx-state-directory> --output <evidence-directory> [--run-id <id>]
```

The verifier reads successful physical Jobs and their published artifacts through local SQLite and the readers included in the checkout. It makes no GX or model calls. Biopython reads the original mmCIF and NumPy performs a proper-rotation SVD fit for label chains AB, AC and BC in model 1, entity 1. Each calculation uses the atom mask recorded by the Job.

| Completed method | What is checked |
| --- | --- |
| `deposited-chain-geometry@2` | Original residue coverage, matched atom keys, rotation angle and fitted RMSD for each whole-chain pair |
| `deposited-block-influence@1` | Baseline, omitted 20-position label intervals, retained coverage, pair and median changes, and dispersed deletion controls |
| `deposited-block-reading@1` | Projected values for the named interval and pair against the independently checked source table |
| `deposited-block-signed@1` | Pair and median changes for the registered endpoint, interval classes, block counts and the dimensionless `missed_present` value or missing reason |

Signed changes equal the value after deletion minus the value before deletion. The median change is the difference between the two stage medians across AB, AC and BC. Absolute tolerances are 0.01° for a fitted angle and 0.001 Å for fitted RMSD. For changes they are 0.02° and 0.002 Å, respectively.

The evidence directory receives `READINGS.md` for the findings and `results.json` for the original input and output hashes, method parameters, coverage, per-interval measurements and discrepancies. Only a closed Run adds the enacted close citations and matches flagged signed intervals across atom masks. A match requires the same original mmCIF hash, label interval and endpoint. The record gives the original matched and observed-union residue counts, identifies a missing second-mask Job or unevaluable interval explicitly, and retains the thresholds used by each signed Job.

## Coordinate-model dependence

Use the two original state directories and their independent verification snapshots to compare a deposited structure with a named fitting template. The command checks corresponding published Jobs in both databases, fits one common proper rotation and translation to all three chains for each possible chain mapping, and tests exact congruence of the printed decimal coordinates. It compares signed intervals only for settings actually measured in both Runs. The reports include the publication chronology on each material and full-precision margins for any classification that changes between templates.

```sh
python acceptance/multi_workflow/independent/template_compare.py \
  --template-state "$TEMPLATE_STATE" --template-evidence "$TEMPLATE_EVIDENCE/results.json" --template-entry "$TEMPLATE_ENTRY" \
  --target-state "$TARGET_STATE" --target-evidence "$TARGET_EVIDENCE/results.json" --target-entry "$TARGET_ENTRY" \
  --output "$TARGET_EVIDENCE/template-relationship.json" --report "$TARGET_EVIDENCE/TEMPLATE.md"
```
