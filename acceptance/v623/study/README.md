# Public coordinate study

This series uses one GX Goal, two consecutively assigned deposition files, and actual local CPU Jobs. The engineering investigator is an official Codex host connected to a local scripted Responses provider. Its decisions and token usage have their own actor class. A live research study requires an independently funded and authorized production enrollment.

The operator first freezes the study in a fresh directory outside the checkout. Use the installed official Codex binary, pinned Python environment and the local Clash binary. A synthetic seal produces two artificial coordinate files and a separate series ID.

```bash
repo=$(pwd -P)
results=/absolute/path/to/fresh/study
trial="$results/run"
clash=/absolute/path/to/clash.new
.venv/bin/python -B acceptance/v623/study/operate.py seal --synthetic --work "$trial"
.venv/bin/python -B acceptance/phase2a/regression/loopback.py \
  --clash "$clash" --receipt "$results/network.json" -- \
  "$repo/.venv/bin/python" -B acceptance/v623/study/operate.py operate --work "$trial"
```

Omit `--synthetic` at seal time to enroll the public archived materials in the frozen retrospective CPU study. Sealing hashes the original bytes, descriptors, model, protocol, visible goal, method card and implementation. It computes no new scientific endpoint. Repeat `operate` on the same work directory and source commit to reopen its saved state. A closed Goal is checked against its original Job and resource receipts.

The investigator initially receives the exact goal in `lead/SCIENCE_GOAL.md`, the common method card and the granted GX catalog. The operator holds both coordinate files and the reference protocol outside the Lead directory. The first accepted plan freezes competing expectations and reporting criteria. GX then admits the first file. After the two mask-specific measurements, a second accepted decision records its evidence, prediction, and potential consequences before the second file is released. The scripted interpretation uses actually received, unrounded metrics.

`study-receipt.json` identifies each Run decision and official turn, material-opening event, Job input and mask, named output and digest, table publication, Job qualification, formal Lead delivery, adopted metric references and cold state. `lead/state/gx.db` and `lead/codex/sessions/` hold the underlying GX and official host records. The independent recomputation, portable replay and table export scripts consume these original files. Both deposited coordinate sets and earlier whole-chain analyses were public. This experiment can support bounded findings about archived coordinates; it cannot establish independent samples, a causal biological mechanism or live autonomous judgment.
