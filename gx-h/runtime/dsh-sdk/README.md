# Optional DeepSeek Harness SDK execution profile

A GX worker can hand an assigned evidence assessment or decision to a genuine local DeepSeek Harness Agent Runtime. GX keeps the task, lease, submission, original files, project Jobs and request budget. Each selected task/lease maps to one stable DSH session ID in the GX activity-start event. The SDK starts the real bundled executable and mounts the sdk-minimal composition with science.yml. GX sends the task context and verified originals through the SDK and retains the SDK result and the provider's observed model/usage.

Install the published Python SDK and platform runtime into this optional local environment from the GX checkout. These Linux x86-64 tests used Python 3.11. The paired 0.2.2rc2 wheels are the published Python distribution tested here. The development source inspection and Cordis plugin use the 0.2.2-rc.5 TypeScript baseline.

```sh
python3 -m venv runtime/dsh-sdk/.venv
runtime/dsh-sdk/.venv/bin/pip install --proxy http://127.0.0.1:7890 \
  --index-url https://pypi.org/simple -r runtime/dsh-sdk/requirements.txt
runtime/dsh-sdk/.venv/bin/python -m unittest tests.test_dsh_runtime -v
python3 -m unittest tests.test_dsh_activity -v
```

The second test starts a recorded local model server on an ephemeral port, runs the two-round GX workflow, reopens it and exercises a mismatched response model. For another loopback service speaking Anthropic Messages SSE, set its port in an execution profile JSON file.

```json
{
  "science_assessment": {
    "runtime": "dsh_sdk",
    "provider": "local_messages",
    "revision": 1,
    "model": "deepseek-v4-flash",
    "endpoint": "http://127.0.0.1:8080/v1/messages",
    "max_requests": 2,
    "max_output_tokens": 900,
    "max_input_bytes": 40000,
    "timeout_seconds": 40
  }
}
```

Run `bin/gx-science start --state DIR --execution-profiles profile.json`, then `bin/gx-science resume --state DIR`. The same profile can be supplied in science_grid run inputs and executed by the native worker activity callback. The example CLI uses a scripted native peer for the decision. A loopback adapter forwards exactly one Messages POST for each budgeted activity, removes tool definitions, rejects tool-use responses and retains response identity and usage from the SSE stream. SDK session-log and package-inventory contributions, retry, compaction and shell/image tools are switched off in this composition. Dependencies and patch SHA are saved at project creation.

The SDK writes native JSONL history under the GX state directory at `dsh/<task_id>/sessions-home`. SDK cold resume loads this exact session, and closing the SDK reaps the native process. GX project recovery reuses a completed activity receipt. If the driver loses its reply before GX commits it, status returns outcome_unknown and the session ID for inspecting history. A restored session can be opened with the pinned SDK's resume_session API without sending any model input. Automatic prompt replay is disabled. Task cancellation stops local execution. Remote provider completion may still be unknown. The scope excludes shared GUIs, external DSH child dispatch and online quality measurements. New work can omit this profile to return to the selected Codex or direct model route.
