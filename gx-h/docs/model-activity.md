# Secondary model for scientific judgment

A project can delegate an evidence assessment or next-round decision to a single Chat Completions request. The acquired GX worker supplies hash-verified originals, retains its lease and submits the resulting product through the ordinary GX task service. The activity has no agent session, tools, child agents or driver loop. The response model must exactly match the pinned model name. A workflow validator and an independent worker still own the relevant stage decisions.

## Configure a new scientific run

Include `execution_profiles` in the project's run inputs. Choose the key matching the task output kind. The science-grid driver consults this mapping when the task has been acquired. This example selects a loopback server with the OpenAI-compatible Chat Completions interface. Set `model` to its actual response model. A run created without a matching profile continues to use its existing worker policy.

```json
{
  "execution_profiles": {
    "science_assessment": {
      "provider": "local_chat",
      "endpoint": "http://127.0.0.1:8080/v1/chat/completions",
      "model": "your-local-model",
      "revision": 1,
      "max_requests": 8,
      "max_input_bytes": 100000,
      "max_output_tokens": 1200,
      "timeout_seconds": 120
    }
  }
}
```

To use DeepSeek, set `provider` to `deepseek`, `endpoint` to `https://api.deepseek.com/chat/completions`, and provide `DEEPSEEK_API_KEY` in the launcher environment. The client uses HTTPS through the local Clash proxy at port 7890. The key is read in the request worker. Use the service's exact model identifier. The provider must support JSON-object responses, return its model identifier and a terminal finish reason of `stop`. The one-request activity uses the model's default reasoning parameters. A service offering different capabilities needs a different profile implementation.

The science-grid entry point accepts a file containing the inner mapping (the keys are output kinds). Run `bin/gx-science start --state "$GX_STATE" --execution-profiles profiles.json` and `bin/gx-science resume --state "$GX_STATE"`. Resume reads the profiles pinned on start.

## Native worker call

A native_v2 scientific worker acquires its task and reads the complete brief. If its task output selects an execution profile, the worker calls `gx_model_activity({"task_id":"..."})`. GX extracts the original proposal, route inputs, Job measurements, evaluator output and prior assessment from the task. The active driver returns `pending` while the native host serves other callbacks. A later call for the same task and lease recovers the result. On completion, the worker passes the exact `artifact_ref` as the `manifest_ref` of `gx_task_finish`. GX checks the submitted model product against the recorded activity completion. An unknown outcome can be inspected with the status command below.

## Standalone call for a claimed task

The existing project database supplies scope, task input, acceptance criteria, the pinned provider and input originals. Write the following operator handoff as JSON with the real artifact references and task instruction.

```json
{
  "output_kind": "science_assessment",
  "input_refs": ["artifact_proposal", "artifact_route_evidence", "artifact_job_observations"],
  "instruction": "Assess each route against its frozen prediction. Return the assessment JSON specified in task_input."
}
```

From the GX checkout use the same database for each command.

```bash
python3 -m gx_core.research.model_activity run --db "$GX_STATE/gx.db" --task "$TASK" --lease "$LEASE" --handoff handoff.json
python3 -m gx_core.research.model_activity status --db "$GX_STATE/gx.db" --task "$TASK" --lease "$LEASE"
python3 -m gx_core.research.model_activity cancel --db "$GX_STATE/gx.db" --task "$TASK" --lease "$LEASE"
```

The completion receipt contains `context_ref` for the handoff, `response_ref` for the provider response and `artifact_ref` for the parsed output. The latter is available to the worker for normal formal submission. The context includes original job IDs when the input artifact has a job owner. Use those IDs and original job records for compute cost. Reported token usage belongs to the provider response ID and the GX operation. The profile caps requests per run, serialized context size per request and output tokens. Missing token counts, remote stop confirmation and currency cost remain unknown.

A matching retry reads the saved answer. A replacement lease can adopt the completed answer for the same scientific inputs and profile. An unresolved earlier request fences a replacement call. The replay boundary is one successful inference per unchanged task context and one driver per operation. There is no provider operation lookup for a disconnected request. A 429 rate-limit response permits one retry under the same lease and remaining request budget. A transport error preserves an unresolved outcome. Cancel records the request, then the active driver terminates its HTTP client. This closes the connection. The provider's internal work may still finish.

## Verification and rollback

Run `python3 -B -m unittest tests.test_model_activity -v` to reproduce the HTTP, lease, budget and persistence checks with recorded provider output on loopback. Run `python3 -B -m unittest tests.test_science_activity_cycle -v` for the two-round GX workflow, including one rejected HTTP request, its bounded retry, formal submissions and cold resume. The loopback peer reads its structured handoff and returns deterministic assessments. These checks measure protocol behavior. A project with an available model service can use the same interface for a scientific quality run.

For rollback, omit the mapping on a new run. Keep the previous database and artifact directory together for replay and audit. Existing run inputs preserve their configured profile and attempt receipts.
