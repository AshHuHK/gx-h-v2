# Task deliveries

A native worker can finish a task using the artifact reference returned by
`gx_artifact_put` for that task. Supply the same `run_id` and `task_id` on the
put. Use `gx_task_finish` with `task_id`, `manifest_ref` set to the reference,
and an optional `summary`.

For multiple originals, the local workflow or Job wrapper can prepare a single
immutable delivery directly. Place outputs in `runs/<run_id>/inbox/` beside
the GX database. This example `outputs.json` preserves the method version and
the source of the measurements.

```json
{
  "entries": [
    {"kind": "result", "media_type": "text/csv", "inbox_path": "sweep.csv"},
    {"kind": "run_log", "media_type": "text/plain", "inbox_path": "solver.log"}
  ],
  "provenance": {
    "method_version": "sweep/1",
    "input_refs": ["<input artifact ref>"],
    "job_id": "<GX job ID>"
  }
}
```

An entry can also be an existing original of this run, written
`{"artifact_ref": "<GX artifact ref>"}`. Store mutable workflow annotations
outside the manifest. The preparation spec accepts inbox paths relative to the
run. Retrying preparation uses the current inbox bytes. A previously returned
digest continues to refer to its original snapshot.

```sh
python3 -m gx_core.artifacts.delivery --db "$DB" prepare \
  --run "$RUN" --task "$TASK" --attempt "$ATTEMPT" --spec outputs.json
```

Pass the returned `sha256:<digest>` to `gx_task_finish`. The committed GX
submission identifies the registered originals. The directory named by the
digest contains the `gx.artifact_manifest/1` document and each new file. Its
manifest records the run, task, numbered attempt, ordered entries and workflow
provenance. Each entry pins its kind, media type, byte count and full SHA-256.
Cited entries also pin the existing artifact reference. Claim and submission
ownership are checked through the current GX attempt.

## Recovery

Keep the SQLite database and its adjacent artifact tree together. GX flushes
staged file contents, then installs the directory and flushes its ancestors up to
the directory holding the database before returning the prepared digest. An
I/O error during that step leaves preparation retryable. The storage volume
must honor file and directory fsync for power-loss durability.

A legacy inbox put releases the source after the outer GX transaction commits.
If a process stops before cleanup, a leftover inbox file can be compared with
the published hash. Direct SQLite transaction callers own inbox cleanup.

An interrupted
prepare may leave a private incomplete directory. The following command cleans
incomplete directories older than one day when no producer is preparing files.

```sh
python3 -m gx_core.artifacts.delivery --db "$DB" gc
```

A completed preparation survives an interrupted or rolled-back finish and can
be submitted again using its digest. To reclaim a named delivery after its
attempt has been superseded or its task reached a terminal state, use

```sh
python3 -m gx_core.artifacts.delivery --db "$DB" discard \
  --run "$RUN" --task "$TASK" --attempt "$ATTEMPT" --manifest "$MANIFEST"
```

Discard retains any delivery with registered files or a submission on that
attempt. Where repeated submissions used a single previously published
artifact, that artifact follows the existing GX retention policy.
