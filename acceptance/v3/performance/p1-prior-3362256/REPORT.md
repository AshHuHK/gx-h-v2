# Prior P1 candidate, stopped after two arrivals

At 20:10:52.713 UTC on 22 November 2026 the acquisition stopped between arrivals to correct a measured delay after a Team slot became free. No public GX command was running at that boundary. This immutable candidate, `336225603dec3a1bc5efd6eccb401ccd25e63287`, completed four of the 26 planned Runs. Its official native span was 729.198 seconds, with 241.546 seconds of occupied model work and 208.329 seconds at two concurrent real Roots. The two-hour gate remains **incomplete** on this revision. [Stop receipt](partial-stop.json) · [Audited counts and timings](summary.json)

The four completed Runs produced eight successful CPU Jobs and 24 recomputed numerical observations, with four passing and four failing frozen route evaluations. Four first reviewer submissions were rejected for incomplete route coverage, four reviewer retries and four planners succeeded. All 12 official Turns completed and were provider-confirmed. All 56 Responses reconciled to **1,330,448 tokens** across the two finite workspace ledgers. There were zero open native reservations, duplicate Jobs, accounting adjustments, transport recoveries or failed native Turns. The audit verified 126 artifacts. All four Runs bound the reviewed plugin, its verified graph adapter and the original scientific capability. Native Roots made 20 measurement tool calls, 12 succeeded and 8 were refused after the originating Root no longer owned the Task. [Native and GX receipts](receipts.jsonl.gz) · [Per-Run plugin and Job receipts](treatment.json) · [Workspace ledgers](workspace-receipts.json)

| Matched first two arrival pairs | v2.1 | Prior P1 |
|---|---:|---:|
| Successful claims and unadmitted READY probes | 12 / 1,118 | 12 / 76 |
| Raw dispatch p95 | 6.961 ms | 899.537 ms |
| Successful claim p95 | 2,310.111 ms | 1,524.593 ms |
| READY to claim p95 | 27.107 s | 24.308 s |
| READY with previous Team slot free, median | 119 ms | 899 ms |
| READY with previous Team slot free, p95 | 128 ms | 2,019 ms |

The original P1 driver reduced the number of denied capacity probes and backed off for up to two seconds after a denial. The eligibility measurements account separately for the preceding lease, native Turn and budget settlement. The delay persisted on an isolated control with the same fixed native reply sequence. Both the partial acquisition and the control remain attributable to the original immutable revision. [Matched timings](dispatch-comparison.json) · [Individual GX and lease intervals](dispatch-samples.jsonl.gz) · [Controlled comparison](../dispatch-control/REPORT.md)

The next candidate begins with **10,369,552 tokens** of headroom under the 11,700,000 combined candidate token allowance. Its 13-wave profile grants the same 900,000 tokens, six Turns and four CPU Jobs per workspace as Gate 0. Cumulative reporting retains these 12 paid Turns and eight Jobs separately from the new arm’s measurements. [Frozen workload and spending method](../PROTOCOL.md)
