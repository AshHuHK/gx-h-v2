# Controlled dispatch timings

Eight fresh paired-grid workspaces ran in interleaved baseline and candidate order between 20:04:11.019 and 20:07:24.331 UTC on 22 November 2026. The experiment started after the first P1 pair finished and ended 13 milliseconds before the second scheduled release. All requests went to a credential-free loopback Responses peer through the pinned official Codex 0.169.0 binary. Each arm completed 8 Runs, 16 CPU Jobs and 24 native Turns. Every trial had 34 artifact reads, 6 scientific submissions and 6 final replies. The controlled peer omitted one finding from each first reviewer submission, producing exactly two rejected submissions and four accepted products per trial. The two real OAuth campaigns have separate receipts. [Trial commands, timestamps and outcomes](control.json) · [Workload](../control_workload.py)

| GX measurement | v2.1 n | v2.1 p95, ms | P1 n | P1 p95, ms |
|---|---:|---:|---:|---:|
| All dispatch calls | 312 | 106.483 | 72 | 735.559 |
| Actual new claim | 24 | 1,854.392 | 24 | 969.003 |
| Unadmitted READY probe | 288 | 2.923 | 48 | 3.156 |
| READY to actual claim | 24 | 3,441 | 24 | 3,779 |
| Original-lease redispatch | 0 | — | 0 | — |

New claims accounted for 7.69% of baseline dispatch samples and 33.33% of candidate samples. Reweighting the baseline timings to the candidate operation frequencies raised p95 to 1,182.016 ms. Reweighting candidate timings to the baseline frequencies gave 113.839 ms, a 6.9% increase from the observed baseline. Thus the change in operation frequency reproduces the raw p95 increase in this control, even as successful claim p95 fell. The first real pair also showed longer successful claims, so its raw change requires separate attribution. [All dispatch intervals and task/lease joins](dispatch-samples.jsonl.gz) · [Per-trial distributions and frequency reweighting](operations.json)

One trial per arm used the public CLI without instrumentation. The other three per arm recorded the same nested owner timers with a temporary CLI wrapper. Each instrumented READY denial raised `HoldActive`. Every new claim issued a native `thread/start` and a `turn/start`. No original-lease redispatch or native resume delivery occurred. [Instrumented intervals](trace-samples.jsonl.gz) · [Owner distributions](owner-summary.json) · [Instrumentation](../control_instrument.py)

| Owner on the admitted path | v2.1 p50, ms | v2.1 p95, ms | P1 p50, ms | P1 p95, ms |
|---|---:|---:|---:|---:|
| GX claim | 16.261 | 37.405 | 23.719 | 33.187 |
| GX thread materialization | 76.960 | 1,255.011 | 51.830 | 505.511 |
| Official `thread/start` | 66.039 | 1,239.125 | 30.772 | 492.629 |
| Official `turn/start` | 11.186 | 104.456 | 8.369 | 25.263 |
| GX turn delivery | 83.963 | 345.300 | 83.686 | 145.781 |

Each row contains 18 samples per arm. The nested intervals overlap. The median claim-owner time rose by 7.46 ms and its p95 fell by 4.22 ms. The first/last native RPCs and local filesystem introduced substantial across-trial variation. Continuing real arrivals are needed to determine the measured production effect under open-loop load. [Actual native owner timing samples](trace-samples.jsonl.gz)

Once a previous lease had released, its native Turn had ended and its funds had settled, the next READY same-Team worker waited a median/p95 of 123/1,328 ms on v2.1 and 1,703/1,862 ms on P1. Each arm contributed 16 follow-on admissions. The P1 driver's 0.5, 1 and 2 second retry intervals after capacity denial account for a reproducible wait after a slot becomes free. The first two real arrival pairs independently showed median/p95 of 119/128 ms on v2.1 and 899/2,019 ms on P1. [Control queue and release samples](dispatch-samples.jsonl.gz) · [First real pair](../p1-prior-3362256/first-wave-dispatch-phases.json) · [Real arrival checkpoints](../p1-prior-3362256/dispatch-wave-checkpoints.jsonl)
