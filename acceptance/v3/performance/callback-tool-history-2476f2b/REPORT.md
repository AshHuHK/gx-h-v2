# Effect of the observed grid-tool history on common callbacks

Within corrected P1, formal-submit p95 was **120.173 ms with the tool available and 69.106 ms with the original paid measurement sequence**. The absolute change was -51.067 ms (-42.49%). Across the 36 adjacent matched submissions, the median paired change was -2.782 ms. All aggregate and within-role submit, writer, common-read and overall callback p95 increases in active versus quiet P1 stayed below the paid study’s combined 20% and 5 ms threshold. [Validated counts and exposure](validity.json) · [Case-matched product, lane and measurement effects](effect.json)

The first condition used v2.1. The second used the corrected installation with the reviewed tool available. The third used that identical installation and replayed the original paid first arrival's tool order within its six native Turns. Six adjacent three-workspace blocks covered every ordering of the conditions. All 18 trials used the public CLI, official Codex 0.169.0 and a credential-free numerical Responses fixture. They ran between 22:14:01 and 22:26:21 UTC, with zero overlap against the paid command windows. Every condition completed 12 Runs, 24 CPU Jobs and 36 official Turns. The three conditions delivered 276, 276 and 336 fixture Responses. [Commands, official executable, order and peer actions](control.json) · [Original paid call IDs and phases](../p1/first-wave-grid-tool-order.json)

The active condition issued **36 successful measurement reads before submission** and **24 scoped refusals after submission**. The original paid order, paths and success flags agreed on every affected Turn. The common submission bodies had identical canonical argument lengths, complete reasons and scientific results after replacing only store-generated artifact, Job and evaluation identities with their explicit roles. The six portable SHA-256 values, one per lane and role/Attempt, matched in all three conditions. Other response actions and verdicts matched. [Body and order validation](validity.json) · [Individual callback data](callback-samples.jsonl.gz)

| Component | Samples, v2.1 / quiet / active | v2.1 p50 / p95, ms | Quiet corrected P1 p50 / p95, ms | Active corrected P1 p50 / p95, ms |
|---|---:|---:|---:|---:|
| All callbacks | 240 / 240 / 300 | 4.982 / 53.736 | 5.481 / 63.778 | 5.042 / 53.051 |
| Ordinary artifact read | 204 / 204 / 204 | 4.739 / 8.531 | 4.917 / 11.248 | 4.719 / 7.705 |
| Formal submission | 36 / 36 / 36 | 46.823 / 81.650 | 53.401 / 120.173 | 49.000 / 69.106 |
| Writer within formal submission | 36 / 36 / 36 | 46.394 / 81.187 | 53.082 / 119.727 | 48.593 / 68.646 |
| Writer lock within formal submission | 36 / 36 / 36 | 0.157 / 0.205 | 0.156 / 0.197 | 0.156 / 0.179 |
| Outside writer, formal submission | 36 / 36 / 36 | 0.406 / 0.588 | 0.448 / 0.652 | 0.408 / 0.534 |
| Unrelated GX outer writer during callback epoch | 3198 / 3011 / 3349 | 3.025 / 7.387 | 3.530 / 8.896 | 3.180 / 6.778 |
| Ambient warm file read | 24 / 24 / 24 | 0.136 / 1.105 | 0.107 / 1.291 | 0.130 / 1.722 |
| Ambient fresh 4 KiB write and fsync | 24 / 24 / 24 | 3.695 / 5.555 | 3.348 / 4.582 | 3.892 / 5.958 |
| First reviewer, rejected | 12 / 12 / 12 | 50.260 / 76.424 | 49.130 / 195.266 | 55.402 / 78.905 |
| Second reviewer, accepted | 12 / 12 / 12 | 46.619 / 83.999 | 57.329 / 120.173 | 44.944 / 64.504 |
| Planner, accepted | 12 / 12 / 12 | 41.441 / 81.650 | 53.401 / 92.828 | 49.000 / 69.106 |

| Lane | Scientific submission | v2.1 p95, ms | Quiet P1 p95, ms | Active P1 p95, ms | Active minus quiet, median of six matched changes, ms |
|---|---|---:|---:|---:|---:|
| east | evidence reviewer, accepted | 83.999 | 73.157 | 64.504 | +1.002 |
| east | evidence reviewer, rejected | 54.374 | 195.266 | 58.283 | +7.016 |
| east | planner, accepted | 66.380 | 92.270 | 69.106 | -2.782 |
| west | evidence reviewer, accepted | 64.581 | 120.173 | 63.248 | -12.386 |
| west | evidence reviewer, rejected | 76.424 | 81.608 | 78.905 | +4.783 |
| west | planner, accepted | 81.650 | 92.828 | 67.046 | -11.627 |

Each of the 60 grid calls, including all 24 scope refusals, contained one measured outer writer. The first reviewer on both lanes selected the two measurements after its submission. Its own submit timing therefore precedes those calls. The second reviewer on each lane, and the west planner, selected two successful reads before submitting. The east planner made no measurement call. Earlier calls on a workspace can affect later submissions. The six matched blocks permit this history effect while retaining a quiet corrected arm on identical code. Each lane/role p95 uses six samples and equals its maximum. Outer-writer timers include disk and SQLite waiting and do not assign causality to a particular sync or SQL statement. [Per-role writer times and payload sizes](callbacks.json) · [Independent shared-filesystem measurements](ambient-disk.jsonl) · [Identical-request owner stack](../../p1/callback-owner-audit/REPORT.md)

## Variation across the two quiet controls

The six-workspace quiet P1 arm had a 120.173 ms submit p95, compared with 81.650 ms on v2.1. This exceeds the 20% and 5 ms reference within this smaller replication. Its two slowest submits took 195.266 and 120.173 ms. Their individual outer writers took 183.281 and 119.727 ms and their lock acquisitions took 0.148 and 0.139 ms. The earlier 12-workspace control observed a 224.728 ms v2.1 submit with one 212.966 ms writer and 0.168 ms lock acquisition. Both conditions therefore have observed high individual writer tails, with too little per-operation timing to identify a cause for each outlier. [Matched trials, callback IDs and highest observations](quiet-replications.json)

Combining both quiet control replications as an exploratory sensitivity check gives 108 formal products per revision. The fixed action, outcome and complete argument lengths matched on every lane and role in all 36 quiet workspaces. Both original study distributions remain separately available.

| Common submission | Samples per revision | v2.1 p50 / p95, ms | Quiet P1 p50 / p95, ms |
|---|---:|---:|---:|
| All formal products | 108 | 55.399 / 87.601 | 57.639 / 95.221 |
| First reviewer, rejected | 36 | 51.283 / 76.424 | 50.881 / 91.530 |
| Second reviewer, accepted | 36 | 58.932 / 95.265 | 62.833 / 106.201 |
| Planner, accepted | 36 | 59.308 / 89.908 | 60.697 / 92.828 |

The combined formal-submit p95 difference is +7.620 ms (+8.70%). The largest relative increase among the three scientific-role p95s was +19.77% for the rejected reviewer. The repeated samples gave different tail rankings. The six active workspaces leave the general effect of selecting the tool on submit latency uncertain. The larger paid callback gap retains its review status. [All original and combined sample distributions](quiet-replications.json) · [Reproduction](../quiet_callback_replications.py)
