# Corrected admission timing control

With the reviewed grid plugin active on both revisions, the corrected driver reduced median wait after the preceding Team slot released from **1,826 ms to 127 ms**, and p95 from **2,965 ms to 531 ms**. Each arm produced 16 follow-on admissions. The experiment ran from 20:17:21.992 to 20:20:46.012 UTC on 22 November 2026, before the corrected real-model acquisition began. [All trials and commands](control.json) · [Same-Team queue samples](dispatch-samples.jsonl.gz)

| Fixed-loopback workload | Original P1 `3362256` | Corrected P1 `2476f2b` |
|---|---:|---:|
| Completed Runs / CPU Jobs / native Turns | 8 / 16 / 24 | 8 / 16 / 24 |
| Native scripted Responses / accepted products / rejected products | 184 / 16 / 8 | 184 / 16 / 8 |
| New claims / unadmitted READY probes | 24 / 48 | 24 / 53 |
| Unadmitted probe p95, ms | 3.215 | 3.174 |
| Successful dispatch p95, ms | 1,695.061 | 1,635.612 |
| All dispatch p95, ms | 1,613.889 | 954.357 |
| READY to claim p50 / p95, ms | 3,727 / 4,472 | 2,035 / 3,234 |
| Previous Team slot free to claim p50 / p95, ms | 1,826 / 2,965 | 127 / 531 |

The official Codex binary executed the same 46 model-visible requests per trial. Both revisions selected `org.gx.science-grid@2.0.0`. One trial per revision used the public CLI without additional instrumentation. Three trials per revision timed individual GX owners. During instrumented trials, the original raised `HoldActive` with 34 local provider-capacity denials and two Team-capacity denials. The corrected revision recorded 33 and six. These local occupancy checks carried the `provider_backoff` or `team_capacity` reason. No remote provider or credential was used by the local peer. [Owner times and per-trial outcomes](owner-summary.json) · [Trace with owner denial reasons](trace-samples.jsonl.gz)

The earlier control recorded 288 unadmitted probes on v2.1 across four trials with the same scripted scientific actions. The correction retained most of that reduction while removing the old P1 delay after release. All 448 runtime and test blobs in the corrected staged installation matched the immutable commit. [Earlier control](../dispatch-control/REPORT.md) · [Matched distributions](operations.json) · [Analysis commands](analysis-commands.json)
