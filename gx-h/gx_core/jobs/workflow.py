"""Submit a versioned workflow computation through the existing detached Job owner."""
import json

from ..capabilities import shipped
from ..capabilities.storage import artifact_service
from ..errors import IdempotencyConflict, SchemaInvalid
from .service import JobService, PURPOSE_WORKFLOW, SUCCEEDED


def submit_workflow_job(conn, *, run_id, operation_id, family, capability, inputs, parameters, binding=None):
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise SchemaInvalid("A workflow Job needs a stable non-empty operation_id")
    if conn.in_transaction:
        raise RuntimeError("Submit the Job outside the workflow checkpoint transaction")
    jobs = JobService(conn)
    artifacts = artifact_service(conn)
    key = f"{family}:{operation_id}"
    existing = conn.execute("SELECT input_manifest_ref,submit_intent_json FROM jobs WHERE idempotency_key=?",
                            (f"job:{run_id}:{key}",)).fetchone()
    if existing:
        # Preserve the admitted request, shared data and evaluator pin across cold restarts.
        manifest_ref = existing["input_manifest_ref"]
        entries = artifacts.read_input_manifest(manifest_ref)["entries"]
        if {e["role"]: e["artifact_ref"] for e in entries} != inputs:
            raise IdempotencyConflict("This operation already names different inputs",
                                      details={"operation_id": operation_id, "manifest_ref": manifest_ref})
        original = json.loads(existing["submit_intent_json"])["request"].get("binding")
        if binding and original and any(binding.get(k) != original.get(k)
                                        for k in set(binding) | set(original) if k != "evaluator"):
            raise IdempotencyConflict("This operation already belongs to another execution assignment")
        binding = original
    else:
        shipped.ensure(jobs.ctx, capability)
        manifest_ref = artifacts.make_input_manifest(namespace=f"{run_id}/{family}/inputs", run_id=run_id,
            entries=[{"role": role, "artifact_ref": ref} for role, ref in inputs.items()]).artifact_ref
    return jobs.submit(run_id=run_id, capability_version=capability, purpose=PURPOSE_WORKFLOW,
                       input_manifest_ref=manifest_ref, parameters=parameters, idempotency_key=key, binding=binding)


def observe_workflow_job(conn, job_id, name):
    job = JobService(conn).status(job_id)
    if job["status"] == SUCCEEDED:
        output_ref = next(item["artifact_ref"] for item in job["outputs"] if item["output_name"] == name)
        job["observations_ref"] = output_ref
        job["observations"] = artifact_service(conn).read_json(output_ref)
    return job
