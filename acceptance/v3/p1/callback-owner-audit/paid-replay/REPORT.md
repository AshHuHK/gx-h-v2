# Replay of the slowest accepted paid submissions

Across all 13 paid waves, p95 for the 78 formal submissions on each build rose from 66.66 to 103.29 ms. Seven accepted official submission bodies ran against their reconstructed pre-submit states on both releases. With release code, configuration, SQLite, artifacts and metrics on the acquisition filesystem, callback p95 was **44.90 ms on v2.1 and 42.76 ms on corrected P1**. Each build had 168 uninstrumented observations. The original 273.54 ms and 216.72 ms P1 planners had P1 replay maxima of **39.85 ms and 42.57 ms**. One different P1 planner reached 196.91 ms once. The historical paid increase remains unexplained. All replays invoked the original workflow validator and produced the original accepted product and verdict bytes. [Paired results and source integrity](comparison.json) · [Shared-filesystem raw samples](shared-code-and-state/samples.jsonl.gz)

## Paid cases and equal-input replay

Selection used the largest accepted reviewer and planner callback in each paid arm, the second-largest accepted P1 planner and the observed formal-submission p95 case in each arm. Wave numbers match the acquisition’s zero-based ledger. Each table cell for replay contains 24 measured submissions. The original wave 3 west callback includes a 266.06 ms GX writer interval. [Paid callback and writer receipts](../../../performance/callback-samples.jsonl.gz) · [Historical internal markers](../../../performance/paid-submit-timing/REPORT.md)

| Original paid source | Role | Product bytes | Original callback, ms | v2.1 replay p95, ms | P1 replay p95, ms |
|---|---|---:|---:|---:|---:|
| v2.1, wave 5 east | Reviewer | 908 | 66.45 | 45.69 | 40.15 |
| v2.1, wave 3 east | Planner | 1,494 | 69.07 | 33.54 | 33.64 |
| v2.1, wave 8 east | Planner, formal p95 | 838 | 66.66 | 48.25 | 53.12 |
| P1, wave 2 west | Reviewer | 928 | 95.24 | 39.64 | 40.13 |
| P1, wave 3 west | Planner | 905 | 273.54 | 34.60 | 37.39 |
| P1, wave 8 east | Planner | 731 | 216.72 | 37.19 | 38.62 |
| P1, wave 9 east | Planner, formal p95 | 681 | 103.29 | 30.93 | 48.97 |

In the fully shared-filesystem series, the median paired P1 difference was +0.41 ms. The 95% block-bootstrap interval was −0.10 to +0.89 ms. The difference between marginal p95s was −2.14 ms with a descriptive interval of −9.63 to +9.33 ms. Resampling used 24 time blocks with all seven cases and both builds kept together. The estimates describe these seven selected requests. [Paired analysis](comparison.json)

The one 196.91 ms P1 replay used 12.57 ms CPU, spent 195.18 ms in the writer and took 6.38 ms to acquire it. Its v2.1 partner took 57.12 ms. The v2.1 peak, 115.67 ms on the P1 wave 2 reviewer, used 75.01 ms CPU. Its P1 partner took 25.71 ms. The individual file sync and COMMIT intervals for those uninstrumented calls are unknown. The two original paid planners peaked in different places, before product publication in wave 3 and after the last lease/outbox write in wave 8. A persistent cost tied to either original body would be inconsistent with its repeated paired results. [Raw per-call CPU and writer times](shared-code-and-state/samples.jsonl.gz) · [Original paid timeline](../../../performance/paid-submit-timing/historical-samples.json)

## Local operations on the acquisition filesystem

The GX metric measures the normal `Roots._callback` interval. It includes the production metrics append at writer exit. Separate instrumented replays timed the POSIX artifact syncs, the Python-visible SQLite `execute` operations, and the normal GX metric writer. Each instrumented submission made two file syncs, eleven directory syncs, one `BEGIN IMMEDIATE` and one outer COMMIT. SQLite ran in WAL mode with `synchronous=FULL`. Kernel-internal SQLite WAL sync and scheduling waits share the COMMIT interval. Independent per-row percentiles need not sum to the total. [All 56 probed submissions per build](shared-code-and-state/results.json)

| GX interval, ms | v2.1 p50 | v2.1 p95 | P1 p50 | P1 p95 |
|---|---:|---:|---:|---:|
| Callback, 168 uninstrumented | 28.04 | 44.90 | 28.19 | 42.76 |
| Process CPU, 168 uninstrumented | 11.07 | 14.61 | 11.14 | 15.97 |
| Outer writer, 168 uninstrumented | 27.56 | 43.53 | 27.71 | 41.69 |
| File fsync, summed per call | 4.06 | 5.92 | 3.77 | 5.59 |
| Directory fsync, summed per call | 1.70 | 2.84 | 1.77 | 2.56 |
| All artifact fsync, summed per call | 5.76 | 8.54 | 5.63 | 7.59 |
| BEGIN IMMEDIATE | 0.14 | 0.25 | 0.14 | 0.17 |
| COMMIT | 4.88 | 6.72 | 4.69 | 6.97 |
| GX transaction metric recording | 0.38 | 0.78 | 0.37 | 0.75 |

The first two series used `/tmp` for the Git-exported source and its configuration. SQLite, WAL, artifacts and metrics were on HCFS in all three. The fully shared series placed both Git exports, including the shipped `limits.json` files, on HCFS as well. All three series used separate private storage under `scratchpad/v3-real-performance` at different times. Source placement and time of day varied together, so the absolute difference between series cannot quantify the cost of source placement. [First series](results.json) · [Second series](repeat/results.json) · [Fully shared series](shared-code-and-state/results.json)

| Series | v2.1 callback p95, ms | P1 callback p95, ms | Difference, ms |
|---|---:|---:|---:|
| `/tmp` release, HCFS state, first | 42.40 | 43.31 | +0.91 |
| `/tmp` release, HCFS state, repeat | 40.61 | 43.04 | +2.43 |
| HCFS release and HCFS state | 44.90 | 42.76 | −2.14 |

## Reconstruction and custody

- Each source is a completed paid workspace opened with SQLite’s immutable read-only URI. The final database hash was checked before and after extraction, with no pending WAL bytes. Each completed official tool event matched the registered session-file hash, call ID, thread, turn, GX request digest and returned submission. The envelope archive retains the original argument objects, recorded dynamic schemas, result and opaque GX identities. The report records the private original event’s filename, line number and SHA-256. [Canonical official envelopes](shared-code-and-state/official-envelopes.jsonl.gz) · [Source ledger](shared-code-and-state/results.json)
- Each case started from its own cloned database. The replay cut events at the original product’s first publication, trimmed every later native call receipt, restored the event-backed Run status and revision, Task state and owning lease, and removed the future terminal-turn record. It retained every preceding registered artifact, checked its bytes against the original hash and recreated its path. Foreign-key and database integrity checks passed. Across builds each trial checked its starting database against the case hash. Two independent reconstructions with `PYTHONHASHSEED=0` produced byte-identical images for all seven cases. An unseeded first reconstruction removed unused child rows in a different order for the two reviewer images. Their logical artifact inventories, queues, arguments and outcomes matched. [Reconstruction](reconstruct.py) · [Image hashes and retained native histories](shared-code-and-state/results.json)
- Both reviewers were on their second attempt and retained their earlier rejected submission. Both the original independent reviewer and planner remained identifiable for every decision. All fourteen case-route comparisons were rechecked against the real `fixture-grid@1` Job inputs, three original observations per route, exact evaluator and threshold. The implementation recomputed each score as `-(x-2)^2` and verified the reviewer’s support judgment against the frozen prediction. Every replay made 17 GX row changes, created a new independent submission ID and preserved the original request, product, brief and verdict hashes. [Per-case grid scores and evidence references](shared-code-and-state/results.json)
- For a P1-authored Root replayed on v2.1, the existing v2.1 constructor received the original ninth tool declaration, `gx_grid_measurement`, from the official native tool list. This preserves the original Root’s tool assignment and brief while submitting the shared built-in `gx_submit_product`. The replay used the original turn’s active ownership and the GX test clock to place the lease at the paid timestamp. The historical in-memory heartbeat timer was unavailable. Each build performed the same live lease check, more than half an hour before expiry. [Runner and postconditions](replay.py)

The reconstruction covers the callback’s business inputs and output-producing operations. The paid host’s WAL depth, kernel caches, contemporaneous native processes, filesystem queue, and unconsumed bookkeeping outside the callback cannot be recovered from a final SQLite file. The isolated series regenerates its WAL and has one synchronous callback writer. Each case reuses the same private physical destination for both builds, with its database bytes and artifact directory state reset between calls. Every second arm is immediately adjacent to its paired first arm, and each case has 12 bare and four probed pairs in each order. The paid metadata and independent end-to-end workloads remain the evidence for actual service performance. [Paid study](../../../performance/p1/REPORT.md) · [Interleaved actual-binary control](../../../performance/callback-control-2476f2b/validity.json)

## Reproduction

The tested releases were v2.1 `61ed0067448658e02d181b3cb85d7f68e39b6616` and corrected P1 `2476f2bdd74b53e3833ce35d08287120e4fdf0c8`. Each run launched separate interpreters with pinned CPU 112, Python 3.11.9 and SQLite 3.45.3. The fully shared acquisition ran 22:38:02 through 22:38:43 UTC on 2026-11-22. Each series contains 336 bare calls, 112 separately probed calls and 14 warmups. The 1,386 offline callback executions used local copies of the original acquisition databases and native sessions. Each trial directory was removed after the run. [Environment and exact archive digests](shared-code-and-state/results.json)

From the `gx-auto` workspace, the fully shared command was equivalent to the following. Use a fresh `--out` to retain a new series. The comparison command reads the three saved series and verifies their custody without running a callback.

```bash
p=gx-h-v-3/acceptance/v3/p1/callback-owner-audit/paid-replay
PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 python -B "$p/shared_checkout.py" \
  --v21 gx-h-2-1 --p1 gx-h-v-3 --storage scratchpad/v3-real-performance \
  --out "$p/shared-code-and-state" --bare-pairs 24 --traced-pairs 8 --seed 20261124
PYTHONDONTWRITEBYTECODE=1 python -B "$p/compare.py"
```

The measured sources are archived verbatim. The published Python files were formatted after measurement with local Black 26.5.1. The verifier checks each measured hash against its acquisition receipt and compares each published file’s Python syntax tree with the corresponding original. The artifact and envelope archives, historical source markers, pinned parent helper and case membership are also checked. [Exact measured scripts](measured-sources.tar.gz) · [Custody verifier](compare.py) · [Recorded original, formatted and syntax-tree digests](comparison.json)
