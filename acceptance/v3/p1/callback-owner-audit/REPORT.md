# Callback owner comparison

Over the first eight real-model waves, submit p95 rose from **65.594 to 95.243 ms** across 63 and 62 callbacks. The matched outer writer interval rose from 64.945 to 94.758 ms p95. Acquiring that writer took 0.203 and 0.190 ms p95 respectively. Read p95 rose from 6.781 to 11.828 ms over 441 and 431 calls. The recorded intervals locate the increase after writer acquisition. They cannot apportion the extra time among SQLite commit, filesystem operations, validation and host scheduling. [Paid wave receipts](../../performance/p1/eight-wave-callbacks.json)

The matched controls found the same database work and file operations on both versions. On the same shared filesystem, submit p95 differed by **+0.979 ms** for fixed requests. The cause of the larger paid-run increase remains unresolved.

## Exact request on the real study filesystem

Each build processed the same byte-for-byte callback envelope against copies of the same claimed reviewer or planner Task, live lease, saved Root, and original scientific artifacts. The first-wave east specification produced two successful local grid Jobs. The common diagnostic Root exposed the native tools both releases know, retained the composed Run's context, and used a current thread/turn fixture. The callbacks published and validated their products through the production owners. A fresh database and original artifact population preceded every measured call. [Snapshot and envelope hashes](shared.json)

| Callback, both roles | Samples per arm | v2.1 p50 / p95 ms | P1 p50 / p95 ms |
| --- | ---: | ---: | ---: |
| Submit product | 16 | 30.042 / 35.709 | 29.271 / 36.688 |
| Read two-route evidence artifact | 16 | 6.605 / 10.673 | 6.815 / 9.665 |

Four blocks alternated which release ran first. With 16 observations per arm, nearest-rank p95 is the maximum. Separate probes timed eight submissions per arm. Median time in the 13 artifact syncs was 6.470 / 6.558 ms for v2.1 / P1. Both made two file and eleven directory syncs. Median SQLite COMMIT time was 5.360 / 5.157 ms and BEGIN IMMEDIATE was 0.155 / 0.159 ms. The COMMIT measurement includes the native SQLite statement. Individual kernel writeback syscalls were unobserved. The probe used its own temporary directory on `hcufs` from 21:49:21 to 21:49:32 UTC, between paid arrivals 8 and 9, and removed the directory. No native model turn was created. [Raw sync, commit and callback measurements](shared.json)

## Source and call profiles

- Both commits contain identical worker instructions, built-in tool schemas, original artifact-read implementation, binding checks and product-submission entry. P1 calls the existing read/access/analytical-data/grant sequence through a shared helper. Its optional plugin dispatch and replay permission checks apply to extension tools. An ordinary fresh read still reads the assigned bytes and records the callback. [Function and schema hashes](source-comparison.json)
- Submission still calls `TeamService.submit_workflow_product`. The task owner verifies the lease, publishes the received product and verdict, calls the graph's original validator, records its judgment, releases the lease and queues acceptance. The pinned validator obtains `default_registry` directly. Every profiled submit made one graph digest from its already loaded definition and source digests. Across all profiled reads and submits, neither build opened any Python source or called the P1 plugin restore gateway. Grid contributes research start, task context and its separate measurement tool. [Team owner](../../../../gx-h/gx_core/teams/service.py) · [Grid registration](../../../../gx-h/plugins/installed/org.gx.science-grid/2.0.0/plugin.py) · [Observed calls](summary.json)
- With identical source artifacts, both builds executed the same ordered SQL shapes. Reviewer submissions executed 75 statements, planner submissions 74 and reads 19. Both made 17 row changes per submit and seven per read in this fixture. The versions had identical Python file-open categories, including the existing `limits.json` reads. Each evidence read opened one artifact. All submits published the product and its verdict. SQLite's own internal I/O falls outside the Python open count. [Per-role profiles](summary.json) · [SQL shapes, opens and function times](samples.jsonl.gz)
- P1 added the grid tool to all 62 real submitting briefs. The p95 size of those saved briefs grew from 5,068 to 5,201 bytes. On these actual briefs, the existing canonical brief hash averaged under 0.103 ms CPU per callback for every 32-repetition measurement. The recorded paid product bytes at p95 were 1,475 / 1,422. [Read-only brief measurements](paid-briefs.json) · [Paid argument sizes](../../performance/p1/eight-wave-callbacks.json)

A separate local-overlay run measured 96 uninstrumented calls per tool and build, evenly split between accepted reviewer and planner submissions or their evidence reads. Submit p50 / p95 was 8.641 / 10.513 ms for v2.1 and 9.001 / 13.189 ms for P1. Process CPU p95 was 7.437 / 7.872 ms. Read p95 was 4.101 / 4.183 ms. The diagnostic fixes input bodies, task state, turn bindings, verdicts and evidence. Real runs additionally vary model products, artifact populations, concurrent work and storage load. The fixed-input timings cannot determine which of those factors drove the paid writer interval. [Local environment and input receipts](environment.json) · [Individual observations](samples.jsonl.gz)

## Reproduction

Run from `gx-h-v-3`. The scripts use Git archives of v2.1 `61ed0067448658e02d181b3cb85d7f68e39b6616` and corrected P1 `2476f2bdd74b53e3833ce35d08287120e4fdf0c8`. The second command uses a new temporary subdirectory under the requested shared storage root.

```bash
./.venv/bin/python -B acceptance/v3/p1/callback-owner-audit/run.py \
  --v21 ../gx-h-2-1 --p1 . --blocks 8 --per-block 6 --seed 403 --out /tmp/gx-callback-local-repeat
./.venv/bin/python -B acceptance/v3/p1/callback-owner-audit/shared.py \
  --v21 ../gx-h-2-1 --p1 . --storage ../scratchpad/v3-real-performance \
  --out /tmp/gx-shared-callback-repeat.json --blocks 4 --per-block 2
```

The subsequent seven-case replay used the original paid reviewer and planner products and their historical evidence under both releases. Fully shared source and data produced callback p95 of 44.90 ms on v2.1 and 42.76 ms on P1. [Paid-body replay, custody and remaining uncertainty](paid-replay/REPORT.md)
