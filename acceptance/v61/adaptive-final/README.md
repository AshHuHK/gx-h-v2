# Final adaptive rehearsal

Run `gxr_01m88de88dj6b2vk9s` used release commit `48af72c3861299350047dd3bae25acfe6a0bc1bd`. GX started three fresh threads through the official Codex App Server. A local scripted peer supplied their responses. Two CPU subprocesses computed measurements from the original deposited coordinates. The fixture exercised one branch and made zero paid model calls.

The first Job observed 49 of 164 positions under the ordered-backbone mask in 2RE9. This met the preregistered insufficient-coverage condition. The second thread received the actual artifact, cited it and amended graph v1 to v2. Its new Job measured 152 of 153 positions in 2O0O with the common-Cα mask. A third thread closed the fixture. These two nonmatched measurements leave cross-deposition robustness undetermined. After restart, the independent scorer reproduced the original report byte for byte. Six chain-pair fits from those Jobs and 18 fits in a separate all-mask run matched independent SVDs within the registered tolerances.

The [receipt](receipt.json) records both measured artifact hashes, exact graph hashes, event order, cost attribution, independent-check differences, raw result paths and their digests. The [preregistration](../adaptive/study-evidence/preregistration.json) binds the [neutral goal](../adaptive/study-goal.md), [numerical and causal rules](../adaptive/study-rubric.json), [data provenance](../adaptive/data/provenance.json) and scorer. The primary live autonomous-study slot remains open.

For a fresh rehearsal, run the following from the release root with its pinned runtime and environment. Use two fresh output paths.

```bash
registration=acceptance/v61/adaptive/study-evidence/preregistration.json
script=acceptance/v61/adaptive/study.py
output=../final-adaptive-rerun
private=../final-adaptive-rerun-private
.venv/bin/python -B "$script" loopback --registration "$registration" \
  --out "$output" --private-dir "$private" \
  --binary vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex --seconds 45
.venv/bin/python -B "$script" score --registration "$registration" \
  --private-dir "$private" --out "$output/rescored.json" --source-class SCRIPTED_OFFICIAL_LOOPBACK
cmp "$output/result.json" "$output/rescored.json"
.venv/bin/python -B "$script" numerics --registration "$registration" --out "$output/numerics.json"
```
