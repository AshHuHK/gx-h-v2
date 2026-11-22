# Recoverable synthetic grid investigation

`science_grid@1` executes a finite numerical experiment on the fixture score `-(x-2)^2`. It uses the GX workflow registry, Engine checkpoints, project-owned local Jobs, native task delivery and independent review. Each proposal freezes its predictions, grids and threshold. The reviewer assesses positive, negative and missing observations. A planner selects the next grids or closes the investigation, with the accepted evaluation and original measurements in its task inputs.

## Run the offline acceptance case

Run from the GX checkout. Use the same state directory between commands. Each command exits after reporting its result. `start` returns while its detached Jobs are running. `resume` drives the workflow to its explicit round bound. Ctrl-C or process exit leaves project Jobs with their original owner. `status` opens the database read-only. `run` combines start and resume.

```sh
bin/gx-science start --state /tmp/gx-science-example
bin/gx-science status --state /tmp/gx-science-example
bin/gx-science resume --state /tmp/gx-science-example
bin/gx-science run --state /tmp/gx-science-example --instance failed-measurement --fail-route path-b
```

The directory contains a dedicated offline store in `offline_native/`. Each instance has its own GX run ID and checkpoints in the same project. The acceptance driver scripts two distinct worker identities through the App Server tool router. This exercises GX state, tool callbacks and real local CPU Jobs offline. It does not measure model intelligence. The first round tests both fixture hypotheses against the threshold -0.25. The planner reads the independent review and tests new candidates around the supported region. The two-round limit ends the example. An injected malformed path yields a failed Job and an inconclusive assessment for that path. The sibling's completed evidence stays readable.

For a pinned plugin evaluator, add `--evaluator 1` on a fresh instance. The persisted evaluator identity and result reference travel with each route. To configure an actual second model provider, see [Model Activity](model-activity.md).

## Native workflow

A real gx-project coordinator can select `science_grid@1` in `gx_run_start`. Pass the model-selected testable predictions, `inputs.proposal.threshold`, `inputs.proposal.paths` and a positive `inputs.max_rounds`. Each path has a distinct `id`, a `prediction` and a `grid` with `synthetic=true`, `method_id=fixture-grid`, `method_version=1`, and `candidates=[{"candidate_id":"...", "x":1.5}]`. The method owns this numerical score. Other scientific methods use their corresponding workflows and capabilities. The effective run budget and the Engine rewind ceiling both apply. The role-specific task briefs specify assessment and decision product schemas. Both tasks use the public acquire and finish tools. A next decision can include a changed grid for each selected route.

After a committed finish, the reactor validates the original hand-in, unlocks its dependents, and resumes a ready checkpoint under the Engine's advance lease. The project driver reconciles receipts and runs this mechanical work before spending a model turn. Run cancellation and pause use the existing GX run controller. An unknown Job receipt remains with its original operation for reconciliation.

## Read or recover an investigation

The final `report_ref` addresses a GX artifact containing per-round proposal, evidence, assessment, decision, task and submission IDs. The proposal identifies the original inputs and method. The evidence set identifies raw output refs or a failed Job. Every next proposal points to the previous decision. Use `gx_artifact_get` on these references. The offline acceptance evidence also provides a JSON export with the original data, independently checkable hashes and scheduler handles.

Reopen the same store, project and instance after interruption. Reuse the original operation and immutable grid for committed or indeterminate work. To change a commission after completion, start a distinct GX instance. An existing workflow retains its version and scientific conclusions.
