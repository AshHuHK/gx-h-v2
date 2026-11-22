# Gate P1 real-model measurement

**PASS for acquisition.** The first official worker Turn started at 20:21:30.035 UTC on 22 November 2026 and the last completed at 22:23:54.479 UTC, a native-work span of **7,344.444 seconds**. Acquisition wall time was 7,352.542 seconds. Each of the 13 scheduled arrivals produced real work on two isolated numerical studies. All 26 Runs completed. The comparative performance thresholds and the remaining attribution limits are reported separately. [Acquisition result](summary.json) · [Public CLI receipts](commands.jsonl) · [Comparison](../COMPARISON.md)

At least one worker Root was active for **1,639.801 seconds**, 22.30% of acquisition wall time. Both Roots were active together for 1,452.524 seconds. Across individual Roots the workers accumulated 3,092.325 seconds. There was no active model Root during 5,712.741 elapsed seconds. GX budget reservations and independently timestamped official worker Turns both reached concurrency 2. [Per-arrival native occupancy and GX reservation exposure](summary.json) · [Official session hashes](native-session-hashes.json)

| Measured outcome | Observed |
|---|---:|
| Completed Runs and independent planner decisions | 26 |
| Accepted reviewer and planner products | 52 |
| Paid official Turns / provider Responses | 78 / 364 |
| Settled provider-confirmed tokens / cached input tokens | 8,638,327 / 7,083,904 |
| Open native reservations / signed token adjustments | 0 / 0 |
| Successful scientific CPU Jobs / retries / duplicate Job keys | 52 / 0 / 0 |
| Recomputed coordinates / rehashed artifacts | 156 / 819 |
| Frozen route evaluations above / below criterion | 26 / 26 |
| Rejected first reviewer products / successful scientific retries | 26 / 26 |
| Failed native Turns / provider backoffs / transport recoveries | 0 / 0 / 0 |
| Completed Runs / official Turns / successful Jobs per elapsed hour | 12.730 / 38.191 / 25.461 |

The original invalid reviewer products missed required route evidence. Their Attempts and rejection reasons remain recorded. The independent reviewer retries and planners supplied the accepted products. The 13 stores passed SQLite integrity and foreign-key checks. The official provider response totals matched the native usage receipts and GX ledgers. Scientific Jobs consumed 2.669 CPU seconds and their frozen outcomes were recomputed from the returned coordinates. [Workspace ledgers and Runs](workspace-receipts.json) · [Individual scientific, Turn and artifact receipts](receipts.jsonl.gz) · [Paired scientific outputs](../grid-equivalence.json)

| Latency, ms | Samples | p50 | p95 | p99 | Maximum |
|---|---:|---:|---:|---:|---:|
| GX dispatch | 609 | 2.938 | 1,213.652 | 3,511.728 | 6,746.804 |
| GX callback | 1,019 | 6.016 | 68.240 | 93.957 | 439.803 |
| SQLite outer writer | 30,464 | 4.086 | 8.878 | 53.571 | 609.938 |
| SQLite writer-lock acquisition | 30,464 | 0.173 | 0.251 | 0.426 | 27.041 |
| Task READY to claim | 78 | 10,193.000 | 28,954.000 | 33,830.000 | 33,830.000 |
| Official native Turn | 78 | 38,492.000 | 55,771.000 | 56,176.000 | 56,176.000 |

All 26 public Run specifications selected `org.gx.science-grid@2.0.0`. Their Run statuses, durable bindings, native Root tool catalogues and actual CPU Job methods agreed. The graph was `science_grid@2` and the capability was `fixture-grid@1`. The reviewed package SHA-256 was `171e6f12272b3d6cd3a8ff2055c9e092c2512113bfdc0684da48a834a6c8d0ef`. Real workers spontaneously requested `gx_grid_measurement` **118 times**. There were 68 successful reads and 50 refusals after the originating worker had submitted and relinquished its lease. The unchanged prompts allowed each worker to decide whether to use this tool. [All 26 per-Run bindings, native tool outcomes and Job methods](treatment.json) · [Original paid first-arrival calls and timing](first-wave-grid-tool-order.json)

All 448 staged product blobs matched corrected commit `2476f2bdd74b53e3833ce35d08287120e4fdf0c8`. Official Codex 0.169.0 at `d32063f96c2dff1dc96af839b3952abb6acfeda4` had executable SHA-256 `b452ca20f838a8855a88a41050ee4a694033fff4ea3ecdad5633e6e0e8d13f2b`. Both official Roots used ChatGPT OAuth, OpenAI `gpt-6.2-astra` and `xhigh`. Two observed worker connections reached the local Clash HTTP proxy. The per-workspace caps remained 900,000 tokens, six native Turns and four scientific Jobs. [Immutable staged installation](runtime-details.json) · [Native profile and environment](environment.json) · [Clash observation](proxy-observation.json) · [Host activity](progress-load.jsonl)

Corrected P1 used 8,638,327 tokens, 78 Turns and 52 Jobs. The stopped previous revision separately used 1,330,448 tokens, 12 Turns and 8 Jobs. Together the candidates used **9,968,775 tokens**, 90 Turns and 60 Jobs, leaving 1,731,225 tokens below the combined 11,700,000 planning ceiling. Full remaining exposure was checked before admitting every arrival. [Cumulative admission receipts](cumulative-candidate-tokens.jsonl) · [Prior candidate results](../p1-prior-3362256/REPORT.md) · [Reproduction and limits](../PROTOCOL.md)
