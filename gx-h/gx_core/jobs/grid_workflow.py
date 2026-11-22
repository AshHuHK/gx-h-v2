"""The synthetic grid workflow uses project-owned Jobs and their original outputs."""
import sqlite3

from ..capabilities.shipped import ensure_grid
from ..capabilities.storage import artifact_service
from ..errors import IdempotencyConflict, SchemaInvalid
from .service import JobService, PURPOSE_WORKFLOW, SUCCEEDED


def submit_grid(conn: sqlite3.Connection, *, run_id: str, operation_id: str, input_ref: str) -> dict:
    """Start or recover this route's frozen grid request, even if the last ACK was lost."""
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise SchemaInvalid("A grid operation needs a stable non-empty operation_id")
    if conn.in_transaction:
        raise RuntimeError("Submit the grid Job outside the workflow checkpoint transaction")

    jobs = JobService(conn)
    artifacts = artifact_service(conn)
    key = f"grid:{operation_id}"
    existing = conn.execute("SELECT input_manifest_ref FROM jobs WHERE idempotency_key=?",
                            (f"job:{run_id}:{key}",)).fetchone()
    if existing:
        # Keep the original manifest (including its contract pin) across a software upgrade.
        manifest_ref = existing["input_manifest_ref"]
        entries = artifacts.read_input_manifest(manifest_ref)["entries"]
        if [item["artifact_ref"] for item in entries if item["role"] == "grid"] != [input_ref]:
            raise IdempotencyConflict("This grid operation already names a different input",
                                      details={"operation_id": operation_id, "manifest_ref": manifest_ref})
    else:
        ensure_grid(jobs.ctx)
        manifest_ref = artifacts.make_input_manifest(
            namespace=f"{run_id}/grid/inputs", run_id=run_id,
            entries=[{"role": "grid", "artifact_ref": input_ref}],
        ).artifact_ref
    return jobs.submit(
        run_id=run_id, capability_version="fixture-grid@1", purpose=PURPOSE_WORKFLOW,
        input_manifest_ref=manifest_ref, parameters={"operation": operation_id},
        idempotency_key=key,
    )


def observe_grid(conn: sqlite3.Connection, job_id: str) -> dict:
    """Reconcile by receipt, returning raw observations and reference only on success."""
    job = JobService(conn).status(job_id)
    if job["status"] == SUCCEEDED:
        output_ref = next(item["artifact_ref"] for item in job["outputs"]
                          if item["output_name"] == "observations.json")
        job["observations_ref"] = output_ref
        job["observations"] = artifact_service(conn).read_json(output_ref)
    return job
