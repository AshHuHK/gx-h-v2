# Frozen GX-H v2.0 control

The source for the control is Git commit `2314eed72822387e03378e8e676ad5fa9228a8d0`. All tests ran from a separate local checkout populated from those Git objects. The new probes were copied into that checkout for fault reproduction. No package under the previous release was changed. The baseline ran with the released Python wheels and Cordis dependency archives without an external model.

| Control | Observed |
| --- | --- |
| Python full discovery | 277 cases, 273 passed, 4 optional DSH cases skipped, 0 failures or errors. |
| P0 discovery | 106/106 passed. |
| Node and host generation | 2/2 Node tests passed, generator check exited 0. |
| Multi-Root hard limit | Two worker turns with grants of 40 reported 30 and 73 against the 100-token cap. Both remained active and no native budget interrupt arrived during the two-second observation. |
| Missing final receipt | One complete metered response followed by a response without usage. After cold reopen the billing reservation remained held and the follow-up made zero model requests. |
| Native opt-out control | Three streamed text deltas without opt-out, zero with the text-delta method disabled. Both clients observed the dynamic tool request, two raw usage receipts and cumulative usage of 70 including 22 cached tokens. |
| Detached Job late wake replay | The actual CPU Job and wake succeeded. Replaying the existing acknowledgment after the lease ended returned `ILLEGAL_TRANSITION`; the inherited test exited 1 on its final assertion. |

The full invocation arguments, working directories, UTC times and numeric process exits for the regression commands are in `summary.json`. The source manifest has 437 files. Its 376 product, test, example and script files have combined SHA-256 `4cb339b8800fca0c298bd4d1e559fb30013cc76551b6fd69d24c54dffe2705a9`. The pinned Codex CLI 0.169.0 executable has SHA-256 `b452ca20f838a8855a88a41050ee4a694033fff4ea3ecdad5633e6e0e8d13f2b`.

### Native reproduction

Run from a clean baseline checkout with the 2.1 `native_budget.py` and `native_events.py` probes in `acceptance/v21/`. Each command requires the standard Clash environment, a reachable loopback proxy and the locked executable on PATH. These tests use a loopback scripted Responses server.

```sh
python -B acceptance/v21/native_budget.py --baseline --out native-budget.json
python -B acceptance/acc1_cold_meter.py --case missing_last_usage --out official-missing-final.json
python -B acceptance/v21/native_events.py --out native-wire.json
python -B acceptance/native_probe.py --out job-wait gx_job_wait
```

For `--baseline`, exit 0 records successful reproduction of the specified pre-change behavior. The original `gx_job_wait` test records exit 1. Each probe has separate JSON data, stdout/stderr and an exit file.
