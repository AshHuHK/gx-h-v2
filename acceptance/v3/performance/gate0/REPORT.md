# Gate 0 real-model baseline

**PASS.** On 22 November 2026, the first official worker Turn started at 17:42:40.536 UTC and the last completed at 19:44:42.252 UTC, a span of **7,321.716 seconds**. The full open-loop acquisition lasted 7,327.373 monotonic seconds. All 26 Runs completed. Each of the 13 scheduled arrivals produced real model work on a pair of isolated numerical studies. The acquisition, 40 public GX CLI invocations and the final audit all exited 0. [Machine-readable result](summary.json) · [GX command receipts](commands.jsonl) · [Audit command, exit and program digest](audit-command.json)

Native Roots were active for **1,583.510 seconds** in total, or 21.61% of acquisition wall time. Two distinct Roots ran simultaneously for 1,438.667 seconds. The sum of occupied time across individual Roots was 3,022.177 seconds. The remaining 5,743.863 seconds of wall time had no active model Root. The native rollout timestamps and GX reservation timestamps have independent overlap measurements. [Per-arrival occupancy and reservation exposure](summary.json) · [Official rollout hashes](native-session-hashes.json)

| Measured outcome | Observed |
|---|---:|
| Completed Runs and accepted numerical decisions | 26 |
| Accepted reviewer and planner products | 52 |
| Official worker Turns / provider Responses | 78 / 369 |
| Provider-confirmed native tokens / cached input tokens | 8,647,052 / 6,995,712 |
| Open reservations / signed token adjustments | 0 / 0 |
| Successful CPU Jobs / failed Jobs / duplicated operation keys | 52 / 0 / 0 |
| Recomputed coordinates / rehashed artifacts | 156 / 819 |
| Frozen numerical evaluations above / below criterion | 26 / 26 |
| Refused initial reviewer products / successful scientific retries | 26 / 26 |
| Failed native terminal receipts / observed provider backoffs / transport recoveries | 0 / 0 / 0 |
| Completed Runs / official Turns / successful Jobs per elapsed hour | 12.774 / 38.322 / 25.548 |

Each initial reviewer submission omitted required route evidence. The workflow rejected it, retained the original failed Attempt, and accepted the next review. Independent planners delivered 26 final decisions. All 13 SQLite stores passed integrity and foreign-key checks. The GX budget ledgers, native provider response receipts and official per-response token sums agree. The 52 CPU Jobs consumed 2.65 measured CPU seconds and had distinct Job keys and scheduler receipts. [Run and budget receipts](workspace-receipts.json) · [Scoring, claim, Job, Turn and artifact receipts](receipts.jsonl.gz)

| Latency, ms | Samples | p50 | p95 | p99 | Maximum |
|---|---:|---:|---:|---:|---:|
| GX dispatch | 7,577 | 4.221 | 6.961 | 95.563 | 2,884.701 |
| GX callback | 916 | 4.433 | 44.801 | 65.136 | 74.788 |
| SQLite write transaction | 38,086 | 4.076 | 6.303 | 22.346 | 207.068 |
| SQLite write-lock acquisition | 38,086 | 0.176 | 0.233 | 0.391 | 19.349 |
| Task READY to claim | 78 | 10,529 | 27,111 | 31,428 | 31,428 |
| Official native Turn duration | 78 | 38,253 | 53,079 | 56,183 | 56,183 |

Of 7,577 dispatch calls, **7,499 returned while the associated task was READY and waiting to be claimed**. There were 78 actual claims and no censored READY episodes. The dispatch event times measure the public dispatch operation. The per-wave, per-tool and SQLite distributions retain their original observations. [GX telemetry](gx-metrics.jsonl.gz) · [Measurement and comparison method](../PROTOCOL.md)

The staging installation matched all 418 Git blobs from v2.1 `61ed0067448658e02d181b3cb85d7f68e39b6616`. The official Codex 0.169.0 source commit was `d32063f96c2dff1dc96af839b3952abb6acfeda4`, with observed ELF SHA-256 `b452ca20f838a8855a88a41050ee4a694033fff4ea3ecdad5633e6e0e8d13f2b`. The isolated App Servers used ChatGPT OAuth, OpenAI `gpt-6.2-astra`, and `xhigh`. The host used Ubuntu 22.04.4, Python 3.11.9, SQLite 3.45.3, Intel Xeon Platinum 8480+ and an affinity of 224 logical CPUs. One live socket sample found both official processes connected only to the local Clash HTTP proxy. Minute-level host-load sampling began approximately eight minutes into the acquisition. [Installation and upstream hashes](runtime-details.json) · [Environment and budgets](environment.json) · [Proxy observation](proxy-observation.json) · [Minute samples](progress-load.jsonl)

The 26 original bounded specifications are available alongside the numerical and budget input hashes. The preflight configuration failures created zero Runs, model Turns or CPU Jobs. The candidate comparison uses this real-model baseline and the stricter predeclared 20% plus 5 ms latency threshold. [Original project specifications](project-specifications.jsonl.gz) · [Input hashes](input-hashes.json) · [Separate preflights](../preflight.json) · [Reproduction commands](../PROTOCOL.md)
