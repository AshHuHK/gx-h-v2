# D01–D08 workflow acceptance

The official Codex 0.169.0 App Server submitted an independent worker product through a temporary GX deployment with an additional installed calibration workflow. A scripted localhost Responses peer made four requests. The workflow ran one real `fixture-grid@1` CPU Job and retained its three readings. The project completed. [Run and SQLite receipts](run.json) · [App Server requests](requests.jsonl)

| Check | Operation and observed result |
| --- | --- |
| D01 | The package accepted its structured minimum of three samples. An operator change to zero was rejected with the package’s error. The original requirement remained active. |
| D02 | Codex received the installed `gx_sample_count` tool, called it, received three and submitted those observations under the assigned thread, turn and task. |
| D03 | The package’s dispatch hook copied the active minimum into the READY assignment before GX claimed it. The persisted task input and actual worker brief both contain three. |
| D04 | The worker called `gx_task_context`. Its response and initial brief contain the package’s instrument unit and the first attempt number. |
| D05 | An operator pause and public resume ran the package’s replan check against the saved requirement. Updating the threshold from −0.25 to 1.0 reevaluated the same Job output, changing PASS to FAIL. Both evaluation records remain. A new unrequested operation was refused by `prepare_job`. Replaying the admitted key returned the original Job. |
| D06 | A second installed workflow declaring no hooks completed with a numeric criterion. A parent graph bound its final digest while the same package was registering both graphs. Its default validator rejected an unknown evaluator with a specific error. |
| D07 | Installing the external Python module and adding its inventory entry and approval pin left the deployed `gx_core` source hash identical. Editing the package after registration changed the installed digest. A fresh public start refused the changed version. |
| D08 | The original v2.0 code produced a paused `module_collaboration@1` checkpoint. In a fresh process v2.1 restored the same waiting stage, task ID, task input and graph pin. All six original installed graph digests matched. A separate read-only check of the completed paid `tl1a_geometry@2` workspace found the original checkpoint and identical rows for all four Jobs, one final decision and 14 reservations. |

The negative controls exercised the same public Intent and Job owners. The native worker used a protocol fixture. The archived paid project supplied cold-read evidence only. [Defaults, refusal and idempotent replay](defaults-and-refusals.json) · [Nonterminal v2.0 checkpoint and v2.1 continuation](cold-resume.json) · [Paid project snapshot](old-pin.json)

The final focused regression ran 25 cases across Intent, replan, sources, versioned geometry and shipped Job admission. [Matrix](summary.json) · [Regression result](regression.log)

## Replay

From the release root, run the commands below. The baseline cold-resume probe reads the tracked `2314eed` Git revision into a temporary directory. The optional `--old-workspace DIR` argument to the first command checks a previous paid SQLite database using its read-only connection and a temporary backup. The script uses the shipped App Server binary or `GX_P0_CODEX`.

```sh
.venv/bin/python -B acceptance/v21/hooks/run.py --output /tmp/gx-hook-evidence
.venv/bin/python -B acceptance/v21/hooks/cold_resume.py --output /tmp/gx-hook-evidence/cold-resume.json
release_python="$PWD/.venv/bin/python"
(cd gx-h && "$release_python" -B -m unittest -q tests.test_p0_intent tests.test_p0_replan tests.test_p0_review_sources tests.test_tl1a_geometry tests.test_tl1a_review_versions tests.test_shipped_grid)
```

## Installation and hook contract

A trusted release installer adds a Python module exposing `register_all(registry)` to `gx-h/config/workflow_packages.json`. The module registers its graph and optional `WorkflowHooks` through `registry.register(flow, hooks=hooks)`. Its callable bindings, boolean settings and implementation file hashes enter the workflow digest. For a SHIPPED package, record that digest under `workflows` in `gx-h/config/shipped.json`. An installed package can also use the existing exact ACTIVE executable-workflow asset. Every invocation resolves the saved run’s ID, version and workflow digest. Trusted operator installation selects the modules. The research specification names an installed ID and version. [Independent example package](calibration_package.py)

| Hook | Arguments and result | Default |
| --- | --- | --- |
| `validate_intent` | `conn, run_id, edits`. Return an unsupported-item result or raise to reject a change. | Accepts the standard frozen evaluation reference or numeric threshold. GX validates formal item versions and commits Intent. |
| `build_worker_tools` | `conn, run_id`. Return `WorkerTools(specs, prepare, call)`. The Root checks task/turn ownership before callbacks. | No package-specific tools. |
| `prepare_dispatch` | `conn, run_id, task`. Return true when ready, false to wait. Runs inside the claim transaction. | Continue to the normal scheduler. |
| `build_task_context` | `conn, run_id, task_id, attempt`. Return `intent`, `supplement_sources` and any additional task-specific fields. | Task ID, bound Intent and an empty supplement list. |
| `reevaluate` | `conn, payload`. Consume a durable reevaluation request. The source measurement and previous criterion remain addressable. | Explicitly refuses an unavailable reevaluation method. |
| `replan` | `conn, run_id`. Prepare a successor from the existing checkpoint when applicable. | Keep the checkpoint and report no change. |
| `prepare_job` | `conn, run_id, proposal`. Validate the exact fresh request or raise before Job admission. | Leave admission to JobService. |

The six pre-hook v2.0 graphs retain their existing digests. The release separately pins their compatibility bindings in `workflow_compat_hooks`. The historically hashed `p0/science_refresh_v2.py` and `p0/science_inputs.py` retain their original source bytes. The generic driver, Roots, Intent, replan, reactor and Job owner select behavior from the pinned package.
