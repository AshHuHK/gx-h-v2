# Matched native Root claim diagnostic

P1 added 1.78 ms to the median cost of granting a new grid reviewer lease. It saved 0.43 ms on a reviewer denied its team's single slot. Returning an existing live lease took 0.16 ms in both arms. The new-lease median increased in all eight alternating blocks. [Raw observations](raw.jsonl)

| State at entry to `Roots._claim` | Calls per arm | v2.1 p50 ms | P1 p50 ms | v2.1 p95 ms | P1 p95 ms | P1 minus v2.1 p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| READY reviewer, one team slot already occupied | 128 | 1.073 | 0.638 | 1.873 | 0.811 | −1.061 |
| READY reviewer, free slot, prepare and issue first lease | 128 | 4.719 | 6.502 | 7.250 | 8.330 | +1.080 |
| CLAIMED reviewer, return original live lease | 128 | 0.158 | 0.157 | 0.265 | 0.210 | −0.054 |

The runner measured elapsed wall time and process CPU time. CPU p95 for granting a lease rose from 6.223 to 7.634 ms. One eligible call in each arm exceeded 50 ms of wall time. The maxima were 188.75 and 231.78 ms respectively. The run treated all these observations as samples. It used the median for p50 and the nearest rank for p95. [Full summary](summary.json)

## Relation to the real first wave

The real first wave recorded 550 v2.1 dispatch calls and 43 P1 calls. Their aggregate p95s were 6.734 ms and 794.238 ms, and their medians were 4.246 ms and 2.278 ms. A successful full dispatch also materializes the native thread and delivers the turn after the lease is granted. Task-specific backoff changes the share of cheap capacity denials in the caller's measurements. The +1.080 ms change observed here for eligible claims is much smaller than the full-dispatch +787.504 ms. This experiment cannot assign that full difference to a cause or establish end-to-end performance. The recorded 13-wave treatment retains its separate acceptance role. [First-wave counter and latency record](../../performance/p1/first-wave-latency.json)

## Matched conditions

- Both arms used the Gate 0 first-wave east specification. Each opened a fresh keyed Run through the installed start implementation, completed its two real local CPU Jobs on their first attempts, and advanced the pinned graph to its first READY independent reviewer. The grid input hashes, six measured coordinates and scores, method, scientific outcomes and graph digest matched. Per-Run identifiers and operation IDs are retained for each arm. P1 selected `org.gx.science-grid@2.0.0`. The legacy Run used the original graph alone. [Inputs, results and pins](environment.json)
- Each fixture had two Tasks. An added planner occupied the reviewer's team slot through an actual claim in the denial case. Each grant started from the same READY, attempt-zero SQLite image for its arm. Each resumption returned its original live lease and Root before thread materialization. Every observation checked its outcome, final Task and attempt, counts of leases, Roots, Jobs and deliveries, and zero native turn usage. Scientific preparation used the original criterion with no intervening amendment. [Worker and endpoint checks](sample.py)
- Each export occupied a separate process. After three warmups per state, each arm ran eight blocks of 16 calls per state, with a common shuffled ordering inside each matched block. The first arm alternated between blocks. Both processes ran on CPU 112, an Intel Xeon Platinum 8480+, with Python 3.11.9, SQLite 3.45.3 and local overlay storage. Connections stayed open with WAL and FULL synchronous writes. Restoring the READY snapshot and truncating its WAL occurred before the timer. The denial and resumption lanes reused their open connections and original states. [Configuration and timing clocks](environment.json)
- The 768 measured records and 18 warmups identify the build, case, outcome, block, sequence and both clocks. The benchmark used private stores and temporary Git exports. Its timer enclosed `Roots._claim(task_id)`. No Codex transport or model call was made. [Raw measurements](raw.jsonl)

## Reproduction

From `gx-h-v-3`, use the two local repositories and the same release interpreter.

```bash
./.venv/bin/python -B acceptance/v3/p1/dispatch-comparison/run.py \
  --v21 ../gx-h-2-1 --p1 . --blocks 8 --samples-per-block 16 --seed 6140
```

The runner exports `bin`, `gx-h` and `examples` directly from v2.1 commit `61ed0067448658e02d181b3cb85d7f68e39b6616` and P1 commit `336225603dec3a1bc5efd6eccb401ccd25e63287`. The environment receipt records Git tree and archive hashes, benchmark source hashes, the interpreter digest and the physical fixture hashes. Supply `--out DIRECTORY` to retain a separate repeat. [Runner](run.py)
