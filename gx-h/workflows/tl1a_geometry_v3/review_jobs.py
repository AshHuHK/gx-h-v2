"""Hand the independent fits to a detached Job owned by the reviewing Root's attempt."""
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import ScopeDenied
from gx_core.orchestration.hooks import WorkerTools
from gx_core.p0 import manifest
from gx_core.persistence import store
from workflows.tl1a_geometry import domain, review_tools


CAPABILITY = "tl1a-review@1"
TOOL = {**review_tools.WORKER_TOOLS[0], "description":
        "Request one independent CPU Job for all measured protocols in this review. When pending, call "
        "gx_wait_job with its job_id and end this turn. After completion call this tool for each measured "
        "protocol to read the independent fits and obtain its check_ref. Requests for this batch share one Job."}


def prepare_inputs(conn, run_id, inputs):
    """Freeze the exact bytes from original Job observations or their explicitly cited rescoring."""
    assets = ArtifactService(conn)
    evidence = assets.read_json(inputs["evidence_ref"])
    routes = {}
    for route in evidence["routes"]:
        original = route.get("observations_ref")
        if not original:
            continue
        measurement = route.get("rescored_ref") or original
        sha = assets.describe(measurement)["sha256"]
        routes[route["protocol"]] = {"observation_job_id": route["job_id"], "observations_ref": original,
            "observations_sha256": assets.describe(original)["sha256"], "measurement_ref": measurement,
            "measurement_sha256": sha, "measurement_json": assets.read_bytes(measurement, expected_sha256=sha).decode("utf-8")}
    return domain.publish(conn, run_id, "geometry_review_inputs", {"schema": "gx.tl1a.review_inputs/1",
                          "evidence_ref": inputs["evidence_ref"], "routes": routes})


def build_worker_tools(conn, run_id):
    def call(row, tool, args, envelope, *, prepared=None):
        from gx_core.p0 import compute
        if tool != TOOL["name"]:
            raise ScopeDenied("this workflow offers the assigned coordinate review tool")
        scope = review_tools._scope(conn, row, args)
        task = conn.execute("SELECT input_json FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        task_inputs = store.loads(task, "input_json")
        assets = ArtifactService(conn)
        plan = assets.read_json(task_inputs["proposal_ref"])
        inputs = {"structure": scope["structure_ref"], "criteria": scope["criteria_ref"],
                  "observations": task_inputs["review_inputs_ref"]}
        key = "tl1a-review:" + ids.sha256_of_obj([row["root_key"], scope["evidence_ref"], inputs])
        queued = compute.queue_job(conn, row, capability_version=CAPABILITY, inputs=inputs, parameters={},
                                   idempotency_key=key, binding={"evaluator": plan["evaluator"]}, internal=True)
        jid = queued["job_id"]
        job = compute.job_status(conn, row, jid)
        if not job["terminal"]:
            return {"job_id": jid, "review_job_id": jid, "status": job["status"], "waiting": True,
                    "evidence_ref": scope["evidence_ref"], "request_turn_id": envelope["turn_id"],
                    "request_call_id": envelope["call_id"],
                    "instruction": "Call gx_wait_job for this Job and end the turn. Each measured protocol shares it."}
        if job["status"] != "SUCCEEDED":
            return {"job_id": jid, "status": job["status"], "failure": job.get("failure"), "waiting": False}
        saved = manifest.read_manifest(conn, jid)
        owner = saved["admission"]["owner"]
        if (owner["attempt_id"] != row["root_key"] or owner["thread_id"] != row["thread_id"] or
                owner["lease_id"] != row["lease_id"] or owner["task_id"] != row["task_id"]):
            raise ScopeDenied("the independent Job must belong to the current reviewer attempt")
        output = next(o for o in saved["completion"]["outputs"] if o["output_name"] == "checks.json")
        batch = assets.read_json(output["artifact_ref"])
        entry = batch["results"][scope["protocol"]]
        domain.require(batch["evidence_ref"] == scope["evidence_ref"] and entry["measurement_ref"] == scope["measurement_ref"]
                       and entry["observations_ref"] == scope["observations_ref"] and entry["observation_job_id"] == scope["job_id"],
                       "the detached fit must refer to this route's frozen measurements")
        # A second tool request retrieves the same reviewed result even when the native call ID changes.
        earlier = conn.execute("SELECT result_json FROM p0_root_calls WHERE thread_id=? "
            "AND json_extract(result_json,'$.review_job_id')=? AND json_extract(result_json,'$.protocol')=? "
            "AND json_extract(result_json,'$.check_ref') IS NOT NULL ORDER BY rowid LIMIT 1",
            (row["thread_id"], jid, scope["protocol"])).fetchone()
        if earlier:
            return store.loads(earlier, "result_json")
        request = conn.execute("SELECT turn_id,call_id FROM p0_root_calls WHERE thread_id=? "
            "AND json_extract(result_json,'$.review_job_id')=? AND json_extract(result_json,'$.waiting')=1 "
            "ORDER BY rowid LIMIT 1", (row["thread_id"], jid)).fetchone()
        domain.require(request, "the reviewer must first request the batch for this evidence")
        receipt = {**scope, "status": entry["status"], "job_id": jid, "observation_job_id": scope["job_id"],
                   "review_job_id": jid, "review_inputs_ref": inputs["observations"],
                   "request_turn_id": request["turn_id"], "request_call_id": request["call_id"],
                   "job_output_ref": output["artifact_ref"], "output_sha256": output["sha256"],
                   "admission_hash": saved["admission_hash"], "manifest_hash": saved["manifest_hash"],
                   "reviewer_thread_id": row["thread_id"], "reviewer_turn_id": envelope["turn_id"],
                   "reviewer_call_id": envelope["call_id"]}
        check_ref = domain.publish(conn, run_id, "geometry_checks", receipt, task_id=row["task_id"])
        return {**entry, **receipt, "check_ref": check_ref}
    return WorkerTools(specs=(TOOL,), call=call)
