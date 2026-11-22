# Direct callback control

The uninstrumented official-Codex control measured `gx_submit_product` p95 at **91.571 ms on v2.1 and 92.723 ms on corrected P1**. The difference was 1.151 ms, or 1.26%. Overall callback p95 was 67.248 and 70.354 ms, below the predeclared growth threshold. All 24 local trials completed between 21:43:41 and 21:58:26 UTC, with a pause for the intervening paid arrival. There was zero overlap with any paid CLI active window. [Trial commands and peer outcomes](control.json) · [Action, payload and timing checks](validity.json)

| Local interleaved measurement | Samples per arm | v2.1 p50 / p95, ms | Corrected P1 p50 / p95, ms |
|---|---:|---:|---:|
| All callbacks | 480 | 5.652 / 67.248 | 5.527 / 70.354 |
| `gx_submit_product` | 72 | 60.308 / 91.571 | 59.218 / 92.723 |
| Writer within `gx_submit_product` | 72 | 59.767 / 91.070 | 58.780 / 92.241 |
| Accepted reviewer | 24 | 59.807 / 95.265 | 65.715 / 95.447 |
| Rejected reviewer | 24 | 51.484 / 73.665 | 52.464 / 81.988 |
| Accepted planner | 24 | 65.379 / 89.908 | 62.024 / 88.285 |
| `gx_read_artifact` | 408 | 5.211 / 10.697 | 5.149 / 10.827 |
| Fixed warm file read | 48 | 0.136 / 1.462 | 0.138 / 1.519 |
| Fresh 4 KiB create, write, fsync and close | 48 | 3.839 / 6.821 | 4.133 / 8.566 |

Each revision completed 24 fresh Runs, 48 real local numerical Jobs and 72 official native Turns with 552 credential-free loopback Responses. All trials used the public CLI and its existing GX timers. Within each pair, the same script produced the same scientific actions, role and verdict counts, and distributions of canonical UTF-8 argument lengths. Each fresh workspace generated its own object identities. Unrelated outer SQL transactions during callback epochs had p95 of 8.750 ms and 8.925 ms. No paid model calls were made by this control. [Matched callback samples](callback-samples.jsonl.gz) · [Per-role and per-tool timings](callbacks.json) · [Independent shared-filesystem samples](ambient-disk.jsonl)

The paid model calls exercised variable scientific products and ran in consecutive time blocks on a shared filesystem. Their first eight matching arrivals had submit p95 of 65.594 and 95.243 ms. Nearly the entire measured difference occurred inside the single outer GX writer. A second experiment replayed byte-identical complete callbacks against copies of the same GX state and artifacts on the real study filesystem. Submit p95 was 35.709 and 36.688 ms in that experiment. It found 13 filesystem sync operations per submit on both revisions and comparable SQLite commit time. The two controlled experiments bound the repeatable version-specific effect for their workloads. The cause of the larger paid-block difference remains unresolved. [First eight paid arrivals](../p1/eight-wave-callbacks.json) · [Exact request, SQL and filesystem audit](../../p1/callback-owner-audit/REPORT.md)

The same fixed-response workspaces also produced 72 successful new claims per release. READY probes without a claim fell from 789 to 164. Raw dispatch p95 rose from 112.253 to 835.604 ms. The successful-claim p95 rose from 1,624.178 to 1,690.407 ms, or 4.08%. Before entering the worker's warm dispatch, eligibility delay after excluding overlapping sibling dispatch had p50/p95 of 111.736/146.285 and 113.127/134.788 ms. Inside that warm dispatch, time to the durable claim was 8.608/17.225 and 15.519/28.704 ms. The smaller paid READY-probe denominator and an additional cost inside warm claim preparation both appear under controlled actions. [Native, queue, claim and GX writer components for all 24 workspaces](dispatch-components.json)
