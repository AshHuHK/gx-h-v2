# V6 → V6.1 comparison point

The review checkout was clean at `47fc728d32bc10ba478df6a063529a8d98f98828`. The earlier formal/live V6 snapshot was `fe7ff695a7328c198ffb5bb90b50cc2391f6f397`. P2A.1 `e02df99757686982744813e5de745e86ef12e0a3` is the historical reference. Snapshot collection reads a checkout without opening a project. This test rerun used the review checkout and the same machine and pinned environments as the candidate. Checks used local tools and CPU jobs. No live inference was requested.

## Registry and package pins

The public inspection command `gx-h/bin/gx doctor` returned `ok: true`. Its nine provider modules all loaded. Its `registry.compatibility_only` field lists by name 40 tools, 14 bundle validators, and 17 checks that have vocabulary in this distribution but lack a registered provider. For each entry this is a statement about the runtime registry. A contract word by itself does not supply an implementation. The four V6 native dynamic names audited here were outside that contract namespace. They already exist in the V6 native callback path. The snapshot includes the exact provider table and owner for every published name. The candidate comparison records every unchanged, removed and newly introduced gap in all three groups. [Base doctor, registry, per-file contract hashes and scope](base.json)

The old contract combined SHA-256 is `afe0646bcec1a57c36160b38a1011822258bd6cf921b26b6e5b50f18c11ff8dd`. Its DDL digest is `456753d35343e1571693a0e69d7921b75dfee0ebe653942d16eb36e842f1c4a8`. The public plugin verify command resolved `org.gx.protein@0.1.0-v6.1` to `ac5b5d5cf96e942bfc57dc8492aa56b77ad9ecee0716735a3e6338463630fce9` with grants for `tl1a-coordinate-quality@1`, `tl1a-geometry@1` and `tl1a-review@1`.

## Audited runtime route at the review commit

| Phase | Source and observed responsibility |
|---|---|
| CLI admission | `bin/gx-p0` → `gx_core.p0.cli.execute` → `cli.start`. Goal accepts an objective and material, rejects workflow/plugins, resolves its capabilities in `v6_goal.start`, and persists the first decision context via `v6_goal.ensure_task`. A static spec resolves `research_ref` and starts its selected workflow. |
| Native scope | `p0.roots.Roots._tools_for` → `p0.v6_native.tool_specs` supplies the frozen task's dynamic tools. `v6_native.call` binds the Root, lease, thread, turn and call to the decision. |
| Graph commit | `v6_decisions.prepare` resolves inputs and proposed effects, `commit` compares the authoritative head and writes both the graph and durable dispatches (`_apply_ops`, outbox topic `v6.node`) in one transaction. |
| External dispatch | `orchestration.engine.Engine.advance` → `v6_jobs.advance` → `_run_node` → `jobs.service.JobService.submit`. The node executor rejects dispatch inside the graph writer transaction. The operation key fences the Job and reservation. |
| Result and next turn | `v6_jobs._active` and `_finish` associate original Job output and persisted observation with its node; `v6_goal.ensure_task` and `_brief` capture newly visible evidence for the next native decision. |
| Reopen | A new public process reopens the database through `p0.cli.existing_db`. `run_until_stopped` / `Driver.drive` and the Engine use the existing graph, task, Job and meter owners. Status uses `p0.driver.snapshot` and `v6_goal.projection`. |

The historic “24” counts occurrences of `is_goal(` or the cached `_goal_mode` name by source line across the seven runtime modules specified in the plan. Four direct comparisons containing the `goal` literal are inventoried separately. A source line may occur in both inventories. Across `gx_core`, the eight Python equality comparisons containing the `science_grid` literal coexist with two other predicates (an SQL predicate and membership test). Seven import sites in four modules reference `v6_protein`. All locations and predicates are retained in the snapshot. The candidate uses exactly these same scan rules; mode behavior is exercised by the engineering and domain tests.

## Reproduce

Set `GX_PYTHON` to the released Python 3.11.9 executable with Biopython 1.88 and NumPy 1.26.4. The checked release also used Node 22.17.0 and npm 10.9.2. Run the snapshot from the candidate checkout using either checkout as input.

```sh
"$GX_PYTHON" -B acceptance/v61/baseline/snapshot.py --repo "$BASE_CHECKOUT" --out /tmp/gx-v6-base.json
"$GX_PYTHON" -B acceptance/v61/baseline/snapshot.py --repo "$CANDIDATE" --compare /tmp/gx-v6-base.json --out /tmp/gx-v61-candidate.json
```

Both fresh baseline suites passed, 45/45 V6 and 62/62 inherited. The latter includes all three `GeometryWorkflow` cases. The review sandbox's earlier 59/62 remains specific to that environment. Exact case names, command arguments, per-suite exits and log hashes are in the [test receipt](unit-receipts.json). The adjacent logs include every test outcome.
