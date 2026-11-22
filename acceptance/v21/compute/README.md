# Official Root compute and isolation

The pinned Codex CLI 0.169.0 process received real dynamic tool calls through stdio. A local Responses fixture supplied the model actions. The runner, SQLite store, Job admission, Linux isolation, scientific calculation and result publication used the installed GX implementation.

The worker inspected the capability catalog, submitted and cancelled a running grid Job, then proposed `artifact-analysis@1` and parked on its returned Job ID. The acceptance closed the first App Server process and SQLite connection before admission. On cold reopen GX admitted the same proposal, woke the original attempt, and published a difference of means of **12.5** for two observations in the second sample. The worker queried status once, read the result and completed its assigned scientific product. The Job ledger contained two rows, including the cancelled Job. [Official tool sequence, kernel observations and execution manifest](official.json)

The public controls cover unsupported expressions, paths and URLs as input references, a same-project artifact outside the task scope, unbound child calls, post-revocation submission, a changed replay request, schema incompatibility, encoded output size, CPU/time and memory bounds. An exact retry by a successor attempt adopts the original Job and leaves its completion manifest and charge intact. A program that exceeds the declared resource bound terminates without publishing a scientific result. The positive Job received the named input bytes, had a hard 96 MiB address-space limit and zero regular-file write limit, and observed kernel EPERM for a harmless file-open and an unconnected local-socket probe. [Defensive outcomes](controls.json)

Use the release root, its deployed Python environment and its bundled official binary to repeat both checks.

```bash
.venv/bin/python -B acceptance/v21/compute/verify.py --output /tmp/gx-official-compute.json
.venv/bin/python -B acceptance/v21/compute/controls.py --output /tmp/gx-compute-controls.json
```

The generic method runs on Linux with procfs and libseccomp.so.2. Job IDs, timings, receipts and per-execution manifest hashes vary across fresh runs. The measurement and schema-validated output are deterministic for these fixed inputs. [Input and expression contract](../../../gx-h/adapters/local/artifact_analysis/README.md)
