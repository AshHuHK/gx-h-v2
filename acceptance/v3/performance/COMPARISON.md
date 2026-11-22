# Matched real-model performance

**Both two-hour real-model acquisitions passed their measurement checks. The comparative performance result is REVIEW.** All 26 paired Runs produced the same 52 numerical routes, 156 measured coordinates, frozen evaluations and accepted scientific dispositions. The paid arms reached the same concurrency of two active official model Roots. The elapsed rate of completed Runs was 12.774 on v2.1 and 12.730 on corrected P1. The paid GX dispatch and callback p95s crossed their predeclared growth bounds. [Raw threshold decisions and matched inputs](comparison.json) · [Complete scientific equivalence check](grid-equivalence.json) · [Gate 0](gate0/REPORT.md) · [Gate P1](p1/REPORT.md)

| Paid wall-time measure | Samples, v2.1 / P1 | v2.1 p95, ms | P1 p95, ms | P1 change |
|---|---:|---:|---:|---:|
| GX dispatch | 7,577 / 609 | 6.961 | 1,213.652 | +1,206.691 (+17335.03%) |
| GX callback | 916 / 1,019 | 44.801 | 68.240 | +23.439 (+52.32%) |
| SQLite outer writer | 38,086 / 30,464 | 6.303 | 8.878 | +2.575 (+40.86%) |
| SQLite writer-lock acquisition | 38,086 / 30,464 | 0.233 | 0.251 | +0.018 (+7.55%) |
| Task READY to claim | 78 / 78 | 27,111.000 | 28,954.000 | +1,843.000 (+6.80%) |
| Official worker Turn | 78 / 78 | 53,079.000 | 55,771.000 | +2,692.000 (+5.07%) |

The written plan flags GX dispatch or callback p95 increases over both 20% and 5 ms, or a throughput decrease over 10%. The numerical comparison also tracks SQLite outer-writer p95 against the same latency bounds and evaluates both native-Turn and Run throughput. The original acquisition environment recorded 25%. The stricter written rule was frozen before collecting candidate samples. The two paid arms ran consecutively on the shared host. Native Turns were active during 21.61% and 22.30% of their acquisition windows. The open-loop arrival schedule limits sensitivity to throughput under continuous saturation. [Pre-acquisition written thresholds](../../../V3_IMPLEMENTATION_PLAN.md) · [Frozen protocol](PROTOCOL.md) · [Arrival-by-arrival raw p95, activity, rate and counts](comparison.json)

## Dispatch and queueing

The two arms recorded 78 successful new claims each. Unadmitted calls against READY tasks fell from **7,499 to 531**. Every successful official delivery was a new `turn/start`; the receipts contained zero live-lease redispatches or resume-prefixed deliveries. Using P1's timings at v2.1's operation frequencies gives a p95 of 6.718 ms. Using v2.1's timings at P1's frequencies gives 449.652 ms. The mix change explains a large part of the raw difference. Successful claims also ran slower in the paid P1 block. [Matched events, definitions and standardized percentiles](dispatch-comparison.json) · [Every classified dispatch and lease ID](dispatch-samples.jsonl.gz)

| Paired observed component, ms | Samples, v2.1 / P1 | v2.1 p50 / p95 | Corrected P1 p50 / p95 |
|---|---:|---:|---:|
| Unadmitted call while READY | 7,499 / 531 | 4.211 / 6.729 | 2.503 / 6.386 |
| Successful dispatch through return | 78 / 78 | 184.803 / 2,310.111 | 689.477 / 4,341.657 |
| First reviewer dispatch, cold | 26 / 26 | 1,572.431 / 2,341.927 | 2,778.112 / 6,006.438 |
| Second reviewer dispatch, warm | 26 / 26 | 108.268 / 522.062 | 410.113 / 1,576.927 |
| Planner dispatch, warm | 26 / 26 | 106.139 / 430.564 | 425.732 / 1,489.678 |
| First dispatch to first GX writer | 26 / 26 | 1,303.917 / 2,086.272 | 2,213.309 / 4,525.074 |
| Durable claim to funding reservation | 78 / 78 | 70.000 / 378.000 | 303.000 / 1,184.000 |
| Funding reservation to official Turn start | 78 / 78 | 15.000 / 265.000 | 27.000 / 186.000 |
| Official start to dispatch return | 78 / 78 | 33.933 / 115.013 | 68.583 / 642.188 |
| Previous Team slot occupied while READY | 52 / 52 | 15,800.000 / 28,555.000 | 11,750.000 / 29,950.000 |
| READY with preceding Team slot released to claim | 52 / 52 | 118.000 / 145.000 | 141.000 / 331.000 |
| Eligible wait after excluding simultaneous sibling dispatch | 52 / 52 | 114.417 / 145.000 | 138.000 / 181.000 |
| Warm dispatch start to durable claim | 52 / 52 | 7.518 / 12.047 | 22.900 / 31.805 |
| Time in GX outer writers before warm claim | 52 / 52 | 7.459 / 11.989 | 16.669 / 22.709 |
| Time outside GX outer writers before warm claim | 52 / 52 | 0.046 / 0.068 | 5.441 / 9.389 |

For the 52 warm follow-ons per arm, the original had one outer writer crossing the claim and zero preceding writers. Corrected P1 retained that writer and added one completed writer on each second reviewer and two on each planner. Its composed dispatch validates the prepared scientific basis before issuing the lease, and its planner also checks for replacement review. Clipped writer time and time outside the writer both increased. The before-claim writer-lock acquisition p95s were 0.199 and 0.551 ms. These are observed outer transaction intervals without per-statement timings. The largest eligible wait initially observed on P1 included 1,288.5 ms of synchronous sibling dispatch in 1,411 ms elapsed. [Writer counts, per-role samples and concurrent lane intervals](dispatch-comparison.json) · [Original observed sibling collision](p1/second-wave-dispatch-breakdown.json)

In the direct 24-workspace interleaved control, both arms completed 72 new claims. Unadmitted READY probes fell from 789 to 164. Raw p95 rose from 112.253 to 835.604 ms, while successful-claim p95 rose from 1,624.178 to 1,690.407 ms (+4.08%). The time to claim inside warm dispatch rose from 8.608/17.225 to 15.519/28.704 ms median/p95. Pre-dispatch eligibility after excluding sibling dispatch had p95 of 146.285 and 134.788 ms. Controlled sampling therefore reproduces the frequency change and the additional warm verification cost, while the larger paid successful-dispatch gap remains. [Direct official-binary components](callback-control-2476f2b/dispatch-components.json)

The stopped paid candidate had exhibited 899 ms median and 2,019 ms p95 after a Team slot became available in the first two arrivals. A same-plugin interleaved controlled test of its correction recovered 1,826 / 2,965 ms to 127 / 531 ms median / p95 while retaining the reduction in denied probes. The full corrected paid sample and its per-arrival dispersion appear above and in the underlying receipts. [Stopped paid acquisition](p1-prior-3362256/REPORT.md) · [Controlled scheduler correction](dispatch-control-2476f2b/REPORT.md) · [Original v2.1 versus first P1 control](dispatch-control/REPORT.md)

Cold-start process observations covered 18 corrected Roots across 9 late arrivals. Dispatch to official child birth had midpoint p95 118.049 ms, then child birth to first GX writer had p95 5,027.721 ms. Baseline OS birth timestamps were unavailable. The conservative P1 post-birth time alone exceeded the corresponding v2.1 entire dispatch-to-writer interval in 16 of 18 observed pairs, with a median paired surplus of 753 ms and p95s of 5,023 ms versus 2,435 ms. An independent interleaved zero-Turn study measured real production-profile startup p95 at 1,670 and 1,837 ms across eight cold starts per release. The P1 sample at p95 spent 690 ms in official initialization, 411 ms retrieving the account and 704 ms listing models. Package and worker-tool resolution inside timed dispatch took at most 1.24 ms in that study. The separate preceding first coordinator pass cost time for binding preparation. These controls expose both a small fresh-binding cost and substantial official startup variability. They cannot assign specific portions of the paid cold tail to individual network or native operations. [Paid process-to-writer bounds by Run](p1/native-startup.json) · [Paired conservative post-birth bounds](cold-metadata-control/paid-bound-final.json) · [Source path identities](p1/cold-source-comparison.json) · [Matched zero-Turn controls](cold-metadata-control/REPORT.md)

## Callback and writer residency

| Paid callback, ms | Samples, v2.1 / P1 | v2.1 p50 / p95 | Corrected P1 p50 / p95 | Writer p95, v2.1 / P1 |
|---|---:|---:|---:|---:|
| gx_read_artifact | 713 / 700 | 4.472 / 6.819 | 5.856 / 12.716 | 6.457 / 12.104 |
| gx_submit_product | 103 / 102 | 43.616 / 65.727 | 67.259 / 97.532 | 65.183 / 97.031 |
| evidence reviewer/REJECTED | 26 / 26 | 48.624 / 65.727 | 65.247 / 89.954 | 65.183 / 89.558 |
| evidence reviewer/ACCEPTED | 26 / 26 | 46.049 / 65.300 | 73.636 / 93.957 | 64.848 / 93.429 |
| planner/ACCEPTED | 26 / 26 | 45.370 / 67.313 | 81.904 / 216.719 | 66.931 / 146.516 |
| evidence reviewer/TOOL REFUSED | 25 / 24 | 0.784 / 1.111 | 0.869 / 2.511 | 0.722 / 1.833 |

The paid callback comparison joins each native completion to the GX tool, role, Attempt, submission verdict, argument size and outer writer. Concrete plan/assessment lengths vary with independent scientific text. Each matched submit contains one outer GX writer. Submit writer-lock acquisition p95 was 0.203 / 0.190 ms and time outside the writer p95 was 0.636 / 1.255 ms. Unrelated outer SQL during the callback epochs had p95s of 6.070 and 6.500 ms. These event joins localize elapsed time to the outer writer but do not time its individual SQLite or filesystem operations. [Per-tool and role, payload sizes, ambient writer times and joining limits](callback-comparison.json) · [Individual callback receipts](callback-samples.jsonl.gz)

A separate direct official-binary control held all scripted actions and argument lengths constant through 24 interleaved workspaces. All-callback p95 was 67.248 / 70.354 ms and submit p95 was 91.571 / 92.723 ms on 72 formal products per arm. It used the public CLI with the corrected tool available. A byte-identical owner-level control on the paid study filesystem found submit p95 of 35.709 / 36.688 ms, 13 filesystem syncs per submission in each release, and median outer SQLite commit at 5.360 / 5.157 ms. Both releases executed the same business call counts and SQL. These results do not identify a changed submit call stack for the paid writer gap. A further three-condition official-binary control replayed the actual paid tool order around unchanged scientific products. Each condition supplied 36 matched submits. Formal-submit p95 was 81.650 ms on v2.1, 120.173 ms on corrected P1 with the tool available, and 69.106 ms on the same corrected code actively replaying 36 successful pre-submit reads and 24 post-submit scope refusals. The active-versus-quiet difference was -51.067 ms (-42.49%). The small quiet P1 sample also crossed the 20% and 5 ms reference against its contemporary v2.1 group. Across both quiet replications, the p95s for 108 formal submissions per revision were 87.601 and 95.221 ms (+8.70%). Individual outliers on both versions spent most of their time in the same outer writer. The cause of the larger paid-block submit tail remains unresolved. [Matched uninstrumented callbacks](callback-control-2476f2b/REPORT.md) · [Byte-identical SQL, file calls and synchronization](../p1/callback-owner-audit/REPORT.md) · [Active tool history control](callback-tool-history-2476f2b/REPORT.md) · [Quiet replications and individual outliers](callback-tool-history-2476f2b/quiet-replications.json)

## Scientific execution and spending

Both groups completed 26 Runs, 52 independent reviewer/planner products, 52 CPU Jobs, 78 provider-confirmed native Turns and 26 initial rejection/retry cases. Each arm recorded zero duplicate Jobs, failed native Turns, transport recoveries or open native reservations. The original frozen criteria and bounded inputs matched on every paired Run. The candidate's 26 explicitly composed Runs also bound the verified dispatch adapter, scientific capability and Root measurement tool. Corrected P1 Roots spontaneously made 118 measurement calls, with 68 successful reads and 50 stale-lease refusals. [Native binding, jobs and per-Run outcomes](p1/treatment.json) · [Exact route measurements and independent scientific verdicts](grid-equivalence.json)

The baseline used 8,647,052 tokens, corrected P1 used 8,638,327, and the stopped previous P1 revision separately used 1,330,448. Together the two paid candidates account for 9,968,775 tokens, 90 Turns and 60 Jobs, with 1,731,225 tokens of remaining headroom. No individual GX ledger was expanded. Full exposure was checked before each scheduled release. [Per-arm charges and aggregate ceiling](comparison.json) · [Pre-admission headroom](p1/cumulative-candidate-tokens.jsonl) · [Earlier zero-charge configuration preflights](preflight.json)
