# Native task service and CLI compatibility

| Gate | Check | Result |
| --- | --- | --- |
| G02 | Install the v2 baseline from local Git, initialize a v1 native project, then upgrade the same temporary installation and query that project | All five existing help outputs match byte for byte. The worker's 28 visible tool names, submission schema, unknown-schema error, native state check and project status match. |
| G03 | Trace direct P0 calls and callback references to TeamService, then execute a numerical review through the task root | Six private references in the baseline and zero in the candidate. The new callback calls the public workflow submission operation. An archived v2 submission replays with the identical serialized response, artifact, verdict and outbox entry. |
| C01 | Hold run B's official response while run A is paused, then enqueue status and resume for A | The active workspace coordinator consumes both requests. Both runs complete. Each of four independent submissions has ordered task transitions, a matching committed trigger and a callback receipt for its own task and run. The observed App Server process exits. A fresh launcher makes zero additional model requests. |
| C07 | Collect production callback, transaction and dispatch timings during those two runs | The fixture captured 26 callbacks, 866 SQLite transactions and six dispatches. The respective p95 durations were 12.54 ms, 4.58 ms and 1162.59 ms. |
| Expiry | Replay both preserved CLI failures using separate database and artifact copies | The old installations raise the recorded Codex role-pin error. The candidate releases the expired lease and requeues the same task once, preserves the original submissions and verdict references, and makes the second settlement a no-op. |

The coordinator probe runs the pinned Codex App Server 0.169.0, a scripted loopback Responses provider and local numerical Jobs. The service tests exercise the wire callback on a real SQLite store and use the installed science_grid@1 validator. The expiry regression submits four failed-turn notifications across two attempts and lets both lease deadlines pass on the wall clock.

Run the service and compatibility comparison with `.venv/bin/python acceptance/v21/service/probe.py`. Run the launcher check with `.venv/bin/python acceptance/v21/service/coordinator_probe.py`. Each script uses a disposable installation and state. The captured results are [before.json](before.json), [after.json](after.json), [coordinator.json](coordinator.json) and [expiry.json](expiry.json).

Both regression groups passed under the checkout's offline environment.

```sh
PYTHONPATH=gx-h .venv/bin/python -m unittest tests.test_p0_roots tests.test_p0_root_delivery tests.test_p0_science tests.test_p0_service -q
# 23 tests passed
PYTHONPATH=gx-h .venv/bin/python -m unittest tests.test_native_facades tests.test_team_manager tests.test_p0_wait_leases tests.test_project_continuation tests.test_science_cycle -q
# 68 tests passed
```
