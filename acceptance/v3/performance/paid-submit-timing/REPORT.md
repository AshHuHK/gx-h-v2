# Historical timing inside the paid submission callback

Both paid arms contained 78 formal submissions, with 52 accepted and 26 rejected. All 156 matched an official call, original Task and lease, product Artifact, verdict Artifact, transition and GX callback timer. The remaining 25 baseline and 24 candidate submit invocations produced no formal submission. Each formal submission retained two durable Artifacts. The markers show that the P1 elapsed-time increase is spread across more than one operation. [All original markers and aggregates](historical-samples.json) · [Extractor](extract.py)

| Elapsed interval, p95 in ms | v2.1 | P1 |
|---|---:|---:|
| Callback entry through product Artifact’s durable timestamp | 28.89 | 39.52 |
| Durable product through its publication event | 1 | 2 |
| Publication event through SUBMITTED, including judgment of the product | 15 | 29 |
| SUBMITTED through VALIDATING | 1 | 1 |
| VALIDATING through verdict Artifact’s durable timestamp | 20 | 20 |
| Durable verdict through its publication event | 1 | 1 |
| Verdict publication event through persisted submission | 2 | 3 |
| Persisted submission through accepted or rejected Task transition | 1 | 1 |
| Task transition through last lease release or acceptance outbox insertion | 1 | 1 |
| Last lease or outbox timestamp through outer transaction metric emission | 23.37 | 33.02 |
| Outer transaction metric emission through callback end | 0.47 | 0.48 |

Each row has its own 78 observations per arm. The database clock retains milliseconds. The Artifact owner takes its durable timestamp after moving the staged bytes and syncing the destination directory, before inserting Artifact metadata. Submission judgment calls the unchanged default registry and pinned original workflow validator. The transaction metric captures a monotonic duration through COMMIT and emits its wall-clock timestamp after its transaction-end callbacks. The original GX metrics writer appends each record to the shared filesystem synchronously. Those markers support elapsed intervals, while validation CPU, individual sync calls, SQLite COMMIT and scheduler waiting share intervals. [Artifact publication](../../../../gx-h/gx_core/artifacts/service.py) · [Submission](../../../../gx-h/gx_core/teams/service.py) · [Transaction timing](../../../../gx-h/gx_core/persistence/store.py) · [Original GX metrics](../../../../gx-h/gx_core/metrics.py)

## Three distinct paid peaks

| P1 case | End-to-end | Measured intervals within the single callback |
|---|---:|---|
| Wave 3 west, accepted planner, 905-byte product | 273.54 ms | 128.5 ms to durable product, 17 ms to its publication event, 51 ms to SUBMITTED, 27 ms from VALIDATING to durable verdict and 42.6 ms from lease/outbox to outer metric emission |
| Wave 8 east, accepted planner, 731-byte product | 216.72 ms | 28.7 ms to durable product, 14 ms from its publication event to SUBMITTED, 15 ms from VALIDATING to durable verdict, 114.1 ms from lease/outbox to outer metric emission and 39.9 ms after that emission |
| Wave 10 east, rejected reviewer, 729-byte product | 439.80 ms | 184.4 ms to durable product, 11 ms to its publication event, 42 ms to SUBMITTED, 118 ms from verdict publication event to persisted submission, 32 ms to outcome transition and 10.3 ms after the outer metric emission |

The two accepted planner peaks warrant separate replays because their measured delays occurred on opposite sides of verdict persistence. Their payload hashes, official call and turn IDs and original Artifact references are in the per-case receipt. The rejected slip spans product I/O, validation and simple inherited persistence work. The full teams, artifact, store, engine and recovery modules and grid validation nodes match byte-for-byte. The existing submit branch handles the fresh product before plugin-tool dispatch. Activated P1 in the separate fixed-history control also produced a lower submit p95 than v2.1, whereas the same P1 code yielded a higher submit p95 in the quiet control. The receipts support separating the observed paid tail from direct plugin execution inside these spans. They do not identify an exclusive storage, scheduling or application cause. [Source comparison](../../p1/callback-owner-audit/source-comparison.json) · [Fixed-history control](../callback-tool-history-2476f2b/callbacks.json)

## Resolution of the historical records

Every formal callback in both arms began after all four CPU Jobs in its workspace had finished. The shortest gap was 19.53 seconds on v2.1 and 18.40 seconds on P1. Consecutive host observations at roughly one-minute intervals bracket all 78 P1 formal submissions. Both cgroup quota counters stayed constant across every bracket. On v2.1, 68 callbacks had such zero-throttling brackets, four fell in intervals that contained some quota throttling, and six preceded the initial host sample. These observations exclude active GX numerical Jobs in the same workspace and recorded quota throttling for P1’s peaks. Other host activity and ordinary operating-system scheduling remain possible. [Resource intervals for each callback](resource-coverage.json) · [Coverage extraction](coverage.py)

The preserved filesystem modification and change times of all 312 associated Artifact files resolve to whole seconds. All 26 paid databases currently have empty WAL sidecars. The paid host sampler recorded free space, aggregate load, memory and cgroup counters. It did not record disk queue depth, per-process CPU waits or per-syscall timing. Official native completion timestamps corroborate the external call and its disposition. Continuous per-call kernel or SQLite profiling would require an instrumented replay. [Original host sampler](../sample_resources.py) · [Paid callback joins](../callback-comparison.json)
