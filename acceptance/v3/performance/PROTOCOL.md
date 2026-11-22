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

The real 13-wave correction uses frozen commit `2476f2bdd74b53e3833ce35d08287120e4fdf0c8`. All 448 staged Git blobs matched that commit. After acquisition, `verify_treatment.py` checks every Run’s stored package, adapter, tool scope and scientific Job manifest. The read-only audit reconciles the complete workload. Reproductions require fresh output directories. The measured command lines used these paths. [Corrected control before launch](dispatch-control-2476f2b/REPORT.md) · [Verified installation](p1/runtime-details.json)

```sh
perf=gx-h-v-3/acceptance/v3/performance
stage=scratchpad/v3-real-performance
py=gx-h-2-1/.venv/bin/python
"$py" -B "$perf/run_workload.py" --source gx-h-v-3 \
  --revision 2476f2bdd74b53e3833ce35d08287120e4fdf0c8 --runtime gx-h-2-1 \
  --private "$stage/p1-2476f2b-xhigh" --evidence "$perf/p1" --composed-grid \
  --prior-candidate-summary "$perf/p1-prior-3362256/summary.json" --combined-token-ceiling 11700000
"$py" -B "$perf/verify_treatment.py" --private "$stage/p1-2476f2b-xhigh" \
  --evidence "$perf/p1" --runtime gx-h-2-1
"$py" -B "$perf/audit_workload.py" --private "$stage/p1-2476f2b-xhigh" --evidence "$perf/p1"
"$py" -B "$perf/compare_workloads.py" --baseline "$perf/gate0" --candidate "$perf/p1" \
  --output "$perf/comparison.json"
"$py" -B "$perf/dispatch_phases.py" --baseline "$stage/v21-gate0-xhigh" \
  --candidate "$stage/p1-2476f2b-xhigh" --output "$perf/dispatch-comparison.json" \
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

The scheduling-correction control selected the same reviewed plugin on both arms. Its candidate archive was verified against all 448 recorded Git blobs before the interleaved trials. [Matched corrected control](dispatch-control-2476f2b/REPORT.md)

```sh
mkdir "$stage/control-stage-2476f2b"
git -C gx-h-v-3 archive 2476f2bdd74b53e3833ce35d08287120e4fdf0c8 bin codex_adapter examples gx-h |
  tar -xf - -C "$stage/control-stage-2476f2b"
"$py" -B "$perf/control_workload.py" --baseline "$stage/p1-3362256-xhigh/install" \
  --candidate "$stage/control-stage-2476f2b" --baseline-composed --runtime gx-h-2-1 \
  --private "$stage/control-dispatch-2476f2b" --evidence "$perf/dispatch-control-2476f2b"
"$py" -B "$perf/control_summary.py" --private "$stage/control-dispatch-2476f2b" \
  --evidence "$perf/dispatch-control-2476f2b"
```

The corrected arm recorded optional process startup timings beginning at wave index four. A passive local observer sampled the Linux start ticks for both official Codex children of the public GX driver. Each birth was joined to the unique first dispatch of its Run and the first same-Task GX outer transaction. The baseline arm has no matching process-birth samples. The two real arms retain their original aggregate and per-operation GX timings. [Process observer](observe_native_births.py) · [Time and identity join](native_birth_diagnostics.py)

## Direct callback timing control

The callback study compares v2.1 and the corrected candidate directly in 24 interleaved paired trials. Each revision runs 12 fresh workspaces with the original public CLI and official native binary. The local peer supplies identical scientific actions and reply delays. Existing GX timers measure the callbacks. Official completed calls provide canonical UTF-8 argument sizes, the underlying GX submission identity and the accepted or rejected scientific outcome. The local control waits for confirmed idle windows in the separate paid study, with time reserved before the next scheduled release. [Control workload](control_workload.py) · [Callback and writer joins](callback_diagnostics.py) · [Independent warm-read and fresh-fsync probe](callback_disk_probe.py)

```sh
"$py" -B "$perf/control_workload.py" --baseline "$stage/v21-gate0-xhigh/install" \
  --candidate "$stage/control-stage-2476f2b" --runtime gx-h-2-1 --callback-study \
  --avoid-live-arrivals "$perf/p1" --private "$stage/callback-control-2476f2b" \
  --evidence "$perf/callback-control-2476f2b"
"$py" -B "$perf/callback_diagnostics.py" --baseline "$stage/callback-control-2476f2b" \
  --candidate "$stage/callback-control-2476f2b" --control \
  --output "$perf/callback-control-2476f2b/callbacks.json" \
  --samples "$perf/callback-control-2476f2b/callback-samples.jsonl.gz"
```

## Paid grid-tool history supplement

Before starting the supplementary control, the six official Turns of the first corrected paid arrival supplied its tool order. Both first-attempt reviewers requested their two paths after submission and received scope refusals. Both second-attempt reviewers requested their two paths before submission and succeeded. The west planner used the two paths before submission and the east planner had no measurement call. The supplement uses this exact per-lane/per-role order. [Original official Turn and tool call IDs](p1/first-wave-grid-tool-order.json)

The three-way control uses six uninstrumented workspaces per condition in all permutations of the three conditions. The first runs v2.1 with the common local response script. The second uses corrected P1 with its reviewed measurement tool available. The third uses that same corrected installation and selects measurement calls according to the original paid sequence. The fixture preserves every remaining scientific action and submission. Each product is hashed after replacing workspace-generated artifact, Job and evaluation references with their explicit scientific roles. Numerical values and complete scientific reasons are preserved. Each common outcome is compared by worker role and verdict. This study starts after the separate cold-start probe’s reserved idle window and suspends its own new arrivals around the continuing paid campaign. [Replay implementation](measurement_sequence.py) · [Scientific identity and sequencing checks](validate_callback_study.py)

```sh
"$py" -B "$perf/control_workload.py" --baseline "$stage/v21-gate0-xhigh/install" \
  --candidate "$stage/control-stage-2476f2b" --runtime gx-h-2-1 --callback-study \
  --replay-grid-sequence "$perf/p1/first-wave-grid-tool-order.json" \
  --avoid-live-arrivals "$perf/p1" --private "$stage/callback-tool-history-2476f2b" \
  --evidence "$perf/callback-tool-history-2476f2b"
```

During each uninstrumented control acquisition, the independent disk observer samples one existing file and a fresh durable write per timing iteration. Start it once the control has written its planned trial order. The final commands join official tool completions, verify the paid sequence and compare the 36 matched submissions across all three conditions.

```sh
study="$perf/callback-tool-history-2476f2b"
private_study="$stage/callback-tool-history-2476f2b"
"$py" -B "$perf/callback_disk_probe.py" --private "$private_study" --evidence "$study" \
  --reference "gx-h-2-1/examples/paired-grid.json"
"$py" -B "$perf/callback_diagnostics.py" --control --baseline "$private_study" --candidate "$private_study" \
  --output "$study/callbacks.json" --samples "$study/callback-samples.jsonl.gz"
"$py" -B "$perf/validate_callback_study.py" --control "$study" --paid "$perf/p1"
"$py" -B "$perf/measurement_effect.py" --evidence "$study"
```

The earlier direct callback study's 24 workspaces also support a matched dispatch comparison from the original leases, Turns and GX timers.

```sh
"$py" -B "$perf/dispatch_phases.py" --control --baseline "$stage/callback-control-2476f2b" \
  --candidate "$stage/callback-control-2476f2b" --output "$perf/callback-control-2476f2b/dispatch-components.json"
```
