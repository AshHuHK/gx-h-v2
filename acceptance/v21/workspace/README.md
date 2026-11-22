# One workspace with multiple workflow runs

Use the same state directory and project name for related runs. Each start returns its actual GX `run_id`. A run key identifies the creation request across restarts. Reusing a key and identical specification recovers the original run. A different specification needs its own key.

```sh
bin/gx-p0 start --state /tmp/river-study --spec grid-a.json --run-key A
bin/gx-p0 start --state /tmp/river-study --spec grid-b.json --run-key B
bin/gx-p0 status --state /tmp/river-study
```

The first run establishes the project's formal objective. Each run keeps its own research question, workflow ID, version, digest, inputs and frozen evaluation. Project objectives, method bans, hard constraints, resource limits, preferences, priorities and optimization targets propagate to members in the commit that authorizes the change. Workflow-specific evaluation criteria and data sources belong to their run. New members inherit the complete public item history with its existing source message, change, version and hash. A member's explicit conflicting project item requires a formal change.

Set `workspace_budget` in the first specification to bound individual lines of its chosen budget profile. Each run can use `run_budget` to request particular amounts from the remaining workspace budget. For an explicitly keyed run, an omitted line receives half the amount still available for allocation, rounded up when the available amount is integral and capped by the run's own profile. For two equally funded runs, specify both shares. For example, a workspace with 12 CPU seconds can grant 8 to run A and 4 to run B. `workspace.max_parallel_roots` accepts 1 through 10 and defaults to 10.

The project's `spent` and `reserved` count actual work across the live budget tree. `available` measures room for additional work. `unallocated` measures parent funds available to assign to members. A terminal run can return unused funds after its pending work and nested allocations settle. Reported overruns remain chargeable and reduce the amount available throughout the workspace.

An existing single-run database retains its run ID, original root budget, charges and reservations. Opening it for status requires no write. On the first additional keyed start its original ledger becomes the project parent, and the original run continues to spend from its unallocated balance. New keyed runs use child allocations.

## Evidence

- The v2 baseline accepted one grid start and refused the second specification in the same directory. [Public baseline](../long-run/before.json)
- The public start owner created grid versions 2 and 1 in the same database. Both reached their first actual local Job wait with distinct run IDs, digest pins, budgets and questions. A cold replay of B returned its original ID and preserved the reservation count. [Public start records](public-start.json)
- Two separate `bin/gx-p0 start` processes created runs A and B, and a third process read both through `status`. The command reported 60 CPU seconds reserved for two Jobs and 20 still available to allocate. Both Jobs subsequently succeeded. [CLI receipts](cli.json)
- One edit authored in B refreshed workflow dependencies in A and B. A later member could read both versions and their original hashes. The evaluation item stayed in B. [Intent journal](intent.json)
- The two runs reserved 12 CPU seconds of workspace allocation while cumulative work held started at zero. After the first actual charge reached 9, the second run could use 3 of its originally allocated 4. Closing the first run twice preserved cumulative spend at exactly 12. [Ledger observations](accounting.json)
- Concurrent attempts to allocate 4 units from a remaining 5 admitted one run. Tests also cover explicit conflicting settings, rollback, and read-only selection followed by adoption of the v2 table layout. [Test results](tests.log)
- The related Intent, race, resource-dimension, metering-recovery and cold-input suites passed all 29 tests. [Regression log](regression.log)

Run the focused tests from the release root.

```sh
PYTHONPATH=gx-h:gx-h/tests .venv/bin/python -m unittest tests.test_p0_workspace -v
```
