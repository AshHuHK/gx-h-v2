# Frozen GX-H 2.1 regression

The frozen product commit is `a05dcef64522fbbbc12120b1d78da518e6886b10`. The additional protocol fixtures are at `3b3459421a32dcddbe5765152534d987de94917d`. Both runs used isolated Git archives and offline dependencies. The 423 product, test, example and script files had aggregate SHA-256 `2101a99e9fc48e2c4c3c680c211413aeb2d91ef033b85001a6b9c8020bed63c4`. A Git comparison also verified the release assets and product directories across both commits.

| Suite | Final result |
| --- | --- |
| Python full discovery | 344 discovered, 340 passed, four optional DSH SDK/runtime cases skipped and no failures or errors. |
| P0-focused Python discovery | 124/124 passed. These are part of the full suite. |
| Node plugin host | 2/2 passed. |
| Host role generator | `generate.py --check` exited 0. |
| Official Codex 0.169.0 protocol | 17 inherited scenarios and 13 distinct focused scenarios passed. |

The official tests use the actual locked ELF with a deterministic Responses service on loopback. The child, turn IDs, usage notifications, interrupts, callback transport and crash recovery are native. The detached Job tests also run local CPU processes. This suite made no external model calls. The four Python skips and individual evidence links are in [aggregate.json](aggregate.json).

### Why two immutable runs are retained

The first run's [summary](summary.json) records two failed expectations. It sampled two-Root risk only at `thread/tokenUsage/updated`. The runtime had already crossed its threshold on `rawResponse/completed`. It also expected an exact 35-token wake charge three seconds after a crash. The new store had no raw final receipt at that time and was still inside its 30-second reconciliation grace. The [supplement](supplement/summary.json) retains both scenarios' original requirements for input identity, unique charging and terminal receipts and adds the corresponding observation points. Five supplemental runs cover four previously run scenarios and one additional rapid-child case.

- Two grants reserved 40 tokens each. The second raw response raised observed use to 103 while both Roots were active. Both original turns were interrupted and charged once, at 30 and 73 respectively. GX refused another paid start. The provider imposes no general token bound during notification or interrupt latency. [Two-Root evidence](supplement/native/native-multi-budget.json)
- With rollout access unavailable, GX confirmed a completed child’s 183 tokens and its parent’s 70 as one charge of 253. A live verified child with missing usage kept the original charge provisional. A child that ended before live verification retained its candidate association with unverified metadata. Both missing-usage cases kept a lower bound and charged 100 provisionally. [Complete child](supplement/native/native-late-child.json) · [Missing usage](supplement/native/native-incomplete-child.json) · [Unloaded child](supplement/native/native-unloaded-child.json)
- The crash relay killed GX after the official wake acknowledgment. The native turn completed, and public recovery found one input, one Job and one wake reservation under the original lease. After the real 30-second grace, two public driver starts on the paused project made zero further model requests. Total model usage in the ledger was 2,035, consisting of the prior confirmed 35 and the wake's **estimated 2,000 with observed lower bound 35**. The original worker produced no product after waking and its lease was released. A requested same-thread follow-up was refused, leaving the wake's per-turn cost unresolved. [Wake and accounting evidence](supplement/native/native-wake-ack.json)
- Independent cases with a legitimate same-thread successor recovered a cumulative 70 through one −65 adjustment. The cold turn had no raw receipt, and GX preserved unknown per-turn attribution. In another cold case, a repeated cumulative 70 stayed provisional until a later distinct cumulative 105 justified the adjustments. [Warm follow-up](native/native-meter-warm.json) · [Cold follow-up](native/native-meter-cold.json) · [Repeated-count control](native/native-meter-repeated.json)

### Reproduction

Use the runner stored in each specified checkout, with the locally provisioned locked Codex binary and standard Clash environment.

```sh
# In the a05dcef product checkout
python -B acceptance/v21/regression.py --commit a05dcef64522fbbbc12120b1d78da518e6886b10 --native --out /tmp/gx-v21-original
# In the 3b34594 probes checkout
python -B acceptance/v21/regression.py --commit 3b3459421a32dcddbe5765152534d987de94917d --product-commit a05dcef64522fbbbc12120b1d78da518e6886b10 --native-case native-multi-budget --native-case native-incomplete-child --native-case native-late-child --native-case native-unloaded-child --native-case native-wake-ack --out /tmp/gx-v21-supplement
```

Each output directory contains exact commands, exit codes, UTC timestamps, file hashes and results.
