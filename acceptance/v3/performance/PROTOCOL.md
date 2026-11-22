# Real-model two-hour comparison

Both arms use the official Codex 0.169.0 executable, ChatGPT OAuth, OpenAI `gpt-6.2-astra` and `xhigh` effort. The first release in the v2.1 arm occurred at 17:42:35.651 UTC on 22 November 2026. The first recorded official model Turn started at 17:42:40.536 UTC. [Acquisition configuration](gate0/environment.json) · [First-wave receipts](gate0/first-wave.json)

Each arm releases two single-round finite-grid studies every ten minutes, beginning at zero and ending at minute 120. A fresh staging workspace holds each paired arrival. The public GX workspace coordinator drives its two Runs and enforces a two-Root ceiling. Each Run has one Team with one Root slot. Both use the two original numerical routes, with a small deterministic coordinate shift and their own prospective threshold. The 26 studies produce 52 initial CPU Jobs. Codex reviewers and planners independently read the resulting artifacts and submit the scientific products. GX can admit one additional native Turn per Run after a rejection. One arm has ceilings of 11,700,000 tokens, 78 native Turns, and 52 submitted Jobs, distributed over 13 finite GX workspace budgets. Existing GX service owners enforce each budget. [Workload and budget specification](run_workload.py)

This measures scheduled arrivals under a defined load. Report overall wall time, private coordinator process intervals, occupied model seconds, idle seconds between arrivals, peak native concurrency and a per-wave timeline. Calculate actual native occupancy from official `task_started` and `task_complete` rollout timestamps. Calculate reservation overlap independently. The first actual native start to last actual native completion must span at least 7,200 seconds. Every scheduled wave must have real model activity. Report the observed native busy fraction separately. Coordinator command exit stamps can lag process exit by up to five seconds while the launcher watches for the next arrival. [Receipt extraction](audit_workload.py)

The treatment for the v3 arm is the publicly selected, reviewed `science_grid@2` plugin. Record the durable plugin identity, version, digest, registered capability and tools for each Run. Compare the remaining scientific inputs, candidate grids, cutoffs, profile, budgets and arrival schedule across arms. Keep each source commit, the exact CLI invocations, process exits, operating system, hardware and Python/SQLite/Codex versions with the arm. Keep all tagged preflight observations separate from the two measured workloads. [Preflight outcomes](preflight.json)

## Decision thresholds

The implementation plan adopted a 20% relative and 5 ms absolute threshold for a p95 increase in GX dispatch, callback or SQLite transaction latency. The initial acquisition receipt recorded 25% and 5 ms. The stricter 20% and 5 ms rule applies to both arms and was recorded here before collecting any post-change samples. A decrease of more than 10% in completed Turns/hour or completed Runs/hour also requires investigation. Report queue READY-to-CLAIM, SQLite write-lock latency, observed response tokens, job successes and failures, rejected products, retries, transport recoveries and CPU time. Attribute every explained change to evidence. A threshold exceedance stays open until there is an explanation. [Implementation plan](../../../V3_IMPLEMENTATION_PLAN.md) · [Original acquisition receipt](gate0/environment.json)

The workload denominator includes scheduled interarrival time. Also compare active model durations and matched waves. Count the actual official terminal status, each failed or rejected attempt and the reconciled ledger. Recompute numerical scores and frozen threshold evaluations against artifacts, verify every artifact's size and SHA-256, and verify database integrity. Each arm retains private raw logs and official session files in its own staging directory. Public evidence contains the GX telemetry, numerical and timing receipts, artifact hashes and official-session file digests. Private OAuth material stays outside the repository. The baseline's minute-level shared-host load samples begin at 17:50:32 UTC, about eight minutes after release. Native receipts, GX telemetry and CLI logs cover the first arrival.

## Reproduce Gate 0

From the `gx-auto` workspace, choose a new private staging path and a new evidence directory. The selected runtime supplies its installed Python and matching official Codex executable. New outbound connections require both local Clash listeners and the documented proxy environment.

```sh
gx-h-2-1/.venv/bin/python -B gx-h-v-3/acceptance/v3/performance/run_workload.py \
  --source gx-h-2-1 --revision 61ed0067448658e02d181b3cb85d7f68e39b6616 \
  --runtime gx-h-2-1 --private scratchpad/v3-real-performance/v21-gate0-xhigh \
  --evidence gx-h-v-3/acceptance/v3/performance/gate0
gx-h-2-1/.venv/bin/python -B gx-h-v-3/acceptance/v3/performance/audit_workload.py \
  --private scratchpad/v3-real-performance/v21-gate0-xhigh \
  --evidence gx-h-v-3/acceptance/v3/performance/gate0
```

The second command reads the completed project journals and produces the measurement summary. Omitting its evidence argument prints a read-only progress snapshot.

## Matched P1 invocation

The prepared runner adds `--composed-grid` to the same command, using the immutable candidate revision and new private state and evidence directories. It asks the operator CLI to verify `org.gx.science-grid@2.0.0`, then includes `"plugins":["org.gx.science-grid@2.0.0"]` on each public Run specification. Before any candidate acquisition, the generator reproduced all 26 baseline scientific and budget documents exactly. The original baseline runner is preserved in Git commit `41f3b9da407123daf70be746857e320a09fdff91`. [Input comparison and runner digests](post-input-check.json)

The first P1 candidate was `336225603dec3a1bc5efd6eccb401ccd25e63287`. It stopped after two complete pairs when the timing study isolated a delay after a Team slot was released. All 1,330,448 paid tokens, 12 Turns and eight CPU Jobs remain attributable to that revision. The corrected candidate uses the same per-workspace budgets and includes that spend in its 11,700,000-token aggregate envelope. [Prior candidate report](p1-prior-3362256/REPORT.md)

 After the acquisition exits, `verify_treatment.py` checks every live Run’s stored package, adapter, tool scope and scientific Job manifest, then the same read-only audit used for Gate 0 reconciles the complete workload. Run reproductions in fresh output directories. The original commands used these paths.

```sh
perf=gx-h-v-3/acceptance/v3/performance
stage=scratchpad/v3-real-performance
py=gx-h-2-1/.venv/bin/python
"$py" -B "$perf/run_workload.py" --source gx-h-v-3 \
  --revision 336225603dec3a1bc5efd6eccb401ccd25e63287 --runtime gx-h-2-1 \
  --private "$stage/p1-3362256-xhigh" --evidence "$perf/p1" --composed-grid
"$py" -B "$perf/verify_treatment.py" --private "$stage/p1-3362256-xhigh" \
  --evidence "$perf/p1" --runtime gx-h-2-1
"$py" -B "$perf/audit_workload.py" --private "$stage/p1-3362256-xhigh" --evidence "$perf/p1"
"$py" -B "$perf/compare_workloads.py" --baseline "$perf/gate0" --candidate "$perf/p1" \
  --output "$perf/comparison.json"
"$py" -B "$perf/dispatch_phases.py" --baseline "$stage/v21-gate0-xhigh" \
  --candidate "$stage/p1-3362256-xhigh" --output "$perf/dispatch-comparison.json" \
  --samples "$perf/dispatch-samples.jsonl.gz"
```

## Same-request dispatch control

For the diagnostic control, fixed loopback Responses exercise the release CLI, the matching official binary and real local numerical Jobs. Eight interleaved paired trials hold model-visible reads, submissions, active worker count and reply delays fixed. Under the same scripted requests, the operation trace reports successful claim, capacity denial, native thread creation, turn start, queue time and elapsed time after an occupied Team slot releases. The first trial in each arm used the ordinary CLI. Later trials wrapped the same GX and native owners in each subprocess to measure nested intervals. [Control methods and measured outcomes](dispatch-control/REPORT.md)

```sh
"$py" -B "$perf/control_workload.py" --baseline "$stage/v21-gate0-xhigh/install" \
  --candidate "$stage/p1-3362256-xhigh/install" --runtime gx-h-2-1 \
  --private "$stage/control-dispatch-01" --evidence "$perf/dispatch-control"
"$py" -B "$perf/control_summary.py" --private "$stage/control-dispatch-01" --evidence "$perf/dispatch-control"
"$py" -B "$perf/dispatch_phases.py" --baseline "$stage/control-dispatch-01" \
  --candidate "$stage/control-dispatch-01" --control --output "$perf/dispatch-control/operations.json" \
  --samples "$perf/dispatch-control/dispatch-samples.jsonl.gz"
```
