"""Freeze the execution inputs at Job admission and attach the detached runner's outcome."""
import platform
import re
from pathlib import Path

from ..artifacts import ArtifactService
from ..capabilities import shipped
from ..domain import ids
from ..errors import GxError, SchemaInvalid
from ..persistence import store
from . import intent


def _refuse(reason, message, **facts):
    raise SchemaInvalid(message, details={"reason": reason, **facts})


def check_capability(conn, run_id, capability, *, review_probe=False):
    """GX checks the concrete capability, including hard constraints added after planning."""
    if not review_probe and not shipped.participates(conn, run_id, capability):
        _refuse("run_composition", f"{capability} is outside this Run composition", capability=capability)
    cap_id, version = capability.rsplit("@", 1)
    if intent.forbidden_capability(cap_id, version, intent.active_items(conn, run_id, intent.BAN_TYPES)):
        _refuse("current_intent", f"the active Intent forbids {capability}", capability=capability)


def _owner(conn, row, binding):
    if binding.get("v6_node_id"):
        # JobService has just adopted this exact operation in this writer.
        node = conn.execute(
            "SELECT n.node_id,n.version,n.branch_id,n.decision_ref,n.operation_key,a.plan_key,a.exec_key "
            "FROM v6_nodes n JOIN v6_job_attempts a USING(node_id) WHERE n.run_id=? AND n.job_id=? AND n.node_id=?",
            (row["run_id"],row["job_id"],binding["v6_node_id"])).fetchone()
        if node is None:
            _refuse("invalid_binding", "The dynamic node has no Job admission")
        return {"kind":"graph_node","workflow_id":"gx.v6.instance","workflow_version":str(node["version"]),
                "task_id":None,"attempt_id":node["operation_key"],"node_id":node["node_id"],
                "branch_id":node["branch_id"],"decision_ref":node["decision_ref"],
                "operation_key":node["operation_key"],"plan_key":node["plan_key"],"exec_key":node["exec_key"]}
    run = conn.execute("SELECT workflow_id, workflow_version FROM runs WHERE run_id=?",
                       (row["run_id"],)).fetchone()
    if binding.get("workflow_id") and binding["workflow_id"] != run["workflow_id"]:
        _refuse("invalid_binding", "the requested workflow does not own this run")
    if binding.get("task_id") or binding.get("attempt_id"):
        root = conn.execute(
            "SELECT task_id,attempt,lease_id,thread_id FROM p0_roots WHERE run_id=? AND root_key=?",
            (row["run_id"], binding.get("attempt_id"))).fetchone()
        if (root is None or root["task_id"] != binding.get("task_id")
                or (row["task_id"] is not None and row["task_id"] != root["task_id"])):
            _refuse("invalid_binding", "the attempt does not own the requested task")
        from .roots import require_binding
        require_binding(conn, row["run_id"], root["thread_id"])
        return {"kind": "durable_task", "task_id": root["task_id"], "attempt_id": binding["attempt_id"],
                "attempt": root["attempt"], "lease_id": root["lease_id"], "thread_id": root["thread_id"],
                "workflow_id": run["workflow_id"]}
    if row["task_id"]:
        lease = store.validate_lease(conn, row["lease_ref"], task_id=row["task_id"])
        return {"kind": "task", "task_id": row["task_id"], "attempt": lease["attempt"],
                "attempt_id": row["lease_ref"], "workflow_id": run["workflow_id"]}
    return {"kind": "workflow_node" if run["workflow_id"] and binding.get("node_id") else "run_job",
            "workflow_id": run["workflow_id"], "workflow_version": run["workflow_version"],
            "node_id": binding.get("node_id"), "path_id": binding.get("path_id") or row["path_id"],
            "task_id": None, "attempt_id": None}


def _evaluation(conn, binding):
    evaluator = binding.get("evaluator")
    if evaluator is None:
        return None
    if (not isinstance(evaluator, dict) or set(evaluator) != {"id", "version", "spec_hash"}
            or not isinstance(evaluator["id"], str) or not evaluator["id"]
            or not isinstance(evaluator["spec_hash"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", evaluator["spec_hash"])):
        _refuse("invalid_binding", "an evaluator must identify its frozen specification")
    version = str(evaluator["version"])
    frozen = conn.execute("SELECT status,frozen_sha,spec_json FROM eval_specs WHERE eval_spec_id=? AND version=?",
                          (evaluator["id"], version)).fetchone()
    if (frozen is None or frozen["status"] != "FROZEN" or frozen["frozen_sha"] != evaluator["spec_hash"]
            or ids.sha256_of_obj(store.loads(frozen, "spec_json")) != evaluator["spec_hash"]):
        _refuse("invalid_binding", "the evaluator pin differs from its frozen GX specification")
    return {**evaluator, "version": version}


def _source(intent_doc, bundle):
    files = {"runner": intent_doc["runner_sha256"], **intent_doc["platform"]["files"]}
    for name in ("entry_script", "parser"):
        if script := bundle.get(name):
            files[name] = script["sha256"]
    source = bundle["manifest"]["source"]
    if source.get("sha256"):
        files["method_source"] = source["sha256"]
    job_source = Path(__file__).resolve().parents[1] / "jobs"
    for name in ("resource_guard.py", "executor.py", "service.py"):
        files["gx_core/jobs/" + name] = ids.sha256_of_file(job_source / name)
    files["gx_core/p0/manifest.py"] = ids.sha256_of_file(__file__)
    if any(item.get("nature") for item in bundle["manifest"]["outputs"]):
        files["gx_core/p0/v62_outputs.py"] = ids.sha256_of_file(Path(__file__).with_name("v62_outputs.py"))
    return {"scope": "method, GX Job runner, publication and adapter sources", "files": files,
            "tree_sha256": ids.sha256_of_obj(files), "method_source": source}


def _runtime(env, resources, preflight):
    spec = env["spec"]
    interpreter = (env["probe"].get("interpreter") or {}).get("reported") or {}
    python_path = env["python_executable"]
    dependencies = {"python": interpreter.get("python"), "distributions": interpreter.get("dists", {}),
                    "requirements": env["probe"].get("requirements", {}),
                    "interpreter_sha256": ids.sha256_of_file(python_path) if python_path else None}
    return {"env_id": env["env_id"], "kind": env["kind"], "os": platform.system(),
            "machine": platform.machine(), "execution_host": env["probe"].get("probe_host"),
            "container_digest": spec.get("container_digest"), "lockfile_ref": spec.get("lockfile_ref"),
            "observed_dependencies": dependencies, "dependency_lock_scope": "interpreter and declared distributions",
            "observed_dependencies_sha256": ids.sha256_of_obj(dependencies),
            "gpu": {"requested": resources["gpu"], "preflight": preflight} if resources["gpu"] else None}


def prepare_runtime(launch, bundle, env, resources, preflight):
    """Freeze filesystem-backed source and interpreter observations before the Job writer."""
    capability = launch["capability_version"]
    return {"source": _source(launch, bundle), "runtime": _runtime(env, resources, preflight),
            "pinned_method": capability in shipped.MANIFESTS and shipped.portable_digest(bundle) == shipped.pin(capability)}


def admit(conn, job_id, *, launch, bundle, inputs, resources, prepared):
    """Called under the same write lock as the new Job and its budget reservation."""
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    cap = launch["capability_version"]
    run_id = row["run_id"]
    check_capability(conn, run_id, cap, review_probe=launch.get("purpose") == "probe")
    binding = launch["request"].get("binding") or {}
    owner = _owner(conn, row, binding)
    if owner["kind"] == "durable_task":
        conn.execute("UPDATE jobs SET task_id=?,lease_ref=? WHERE job_id=?",
                     (owner["task_id"], owner["lease_id"], job_id))
    roles = {}
    for kind, owner_id in (("task", owner["task_id"]), ("workflow", run_id if owner.get("workflow_id") else None)):
        if owner_id:
            for item_id, dep in intent.snapshot(conn, run_id, kind, owner_id).items():
                if dep["role"] == "execution":
                    current = intent.read_item(conn, run_id, item_id)
                    if (dep["item_type"] not in intent.BAN_TYPES | {"resource_limit"} and
                            (current["status"] != "active" or current["sha256"] != dep["sha256"])):
                        _refuse("current_intent", "the execution assignment has a superseded dependency",
                                item_id=item_id)
                    if current["status"] == "active":
                        roles[item_id] = "execution"
    deps = intent.bind_dependencies(conn, run_id, "job", job_id, roles)
    checked_constraints = intent.active_items(conn, run_id, intent.BAN_TYPES | {"resource_limit"})
    frozen_inputs = [{key: entry[key] for key in ("role", "artifact_ref", "sha256", "bytes")}
                     for entry in inputs]
    method = bundle["manifest"]
    params = launch["parameters"]
    levels = {"level": "provenance", "replay": "requires original runtime and data",
              "known_variation": ["host runtime and external inputs may vary"]}
    pinned_method = prepared["pinned_method"]
    if pinned_method and cap == shipped.GRID:
        levels = {"level": "deterministic_synthetic_cpu", "replay": "numeric observations on frozen inputs",
                  "known_variation": ["process identifiers, wall time and resource measurements"]}
    elif pinned_method and cap == shipped.ARTIFACT_ANALYSIS:
        levels = {"level": "deterministic_restricted_json_analysis", "replay": "frozen program, inputs, parameters and seed",
                  "known_variation": ["interpreter float roundoff, wall time and resource measurements"]}
    elif pinned_method:
        levels = {"level": "deterministic_coordinate_analysis", "replay": "deposited coordinates and frozen inputs",
                  "known_variation": ["floating-point roundoff, wall time and resource measurements"]}
    doc = {
        "schema": "gx.execution-manifest.admission/1", "job_id": job_id, "run_id": run_id, "owner": owner,
        "task_id": owner["task_id"], "attempt_id": owner["attempt_id"], "job_attempt": row["job_attempt"],
        "admitted_at": row["created_at"], "backend": row["scheduler_kind"],
        "request_sha256": launch["request_sha256"], "operation_key": row["idempotency_key"],
        "intent_dependencies": {key: {field: item[field] for field in ("version", "sha256", "item_type", "role")}
                                for key, item in deps.items()},
        "hard_constraints_at_admission": {k: {"version": v["version"], "sha256": v["sha256"]}
                                         for k, v in checked_constraints.items()},
        "inputs": frozen_inputs, "input_manifest": {"artifact_ref": launch["input_manifest_ref"],
                                                    "sha256": launch["input_manifest_sha256"]},
        "datasets": [{"id": e["artifact_ref"], "version": e["sha256"], "sha256": e["sha256"]}
                     for e in frozen_inputs], "external_data_paths": method["required_data"],
        "source": prepared["source"], "capability": {"id": row["capability_id"],
                    "version": row["capability_version"], "sha256": launch["manifest_sha256"]},
        "model": {"id": None, "provider": None, "weights": launch["weights"],
                  "applicability": "no learned model" if pinned_method else "as declared by capability"},
        "parameters": params, "seeds": {k: v for k, v in params.items() if "seed" in k.lower()},
        "argv": launch["argv"], "limits": resources, "budget_reservation_ref": row["reservation_ref"],
        "runtime": prepared["runtime"], "evaluator": _evaluation(conn, binding),
        "reproducibility": levels,
    }
    if any(item.get("nature") for item in method["outputs"]):
        from .v62_outputs import freeze_declarations
        doc.update(schema="gx.execution-manifest.admission/2",
                   output_declarations=freeze_declarations(method["outputs"], frozen_inputs),
                   provenance_cursor=conn.execute("SELECT COALESCE(MAX(event_id),0) FROM events").fetchone()[0])
    if owner["kind"] == "graph_node":
        from . import capability_views
        revision = capability_views.latest(conn, run_id)
        if revision:
            entry = next(record for record in revision["catalog"] if record["capability_version"] == cap)
            doc["goal_capability"] = {"revision":revision["revision"], "view_sha256":revision["digest"],
                                      **{key:entry[key] for key in ("portable_sha256","plugin","provider_closure")
                                         if key in entry}}
    conn.execute("INSERT INTO p0_execution_manifests(job_id,admission_json,admission_sha256) VALUES(?,?,?)",
                 (job_id, ids.canonical_json(doc), ids.sha256_of_obj(doc)))


def require_current(conn, job_id):
    if not intent.job_current(conn, job_id):
        _refuse("current_intent", "current Intent excludes this Job's execution", job_id=job_id)


def acquires_material(conn, job_id):
    return conn.execute("""SELECT 1 FROM p0_execution_manifests m,json_each(m.admission_json,'$.output_declarations') d
        WHERE m.job_id=? AND json_type(d.value,'$.acquired_material')='object' LIMIT 1""", (job_id,)).fetchone() is not None


def check_launch(conn, job_id):
    require_current(conn, job_id)
    saved = conn.execute("SELECT admission_json FROM p0_execution_manifests WHERE job_id=?",
                         (job_id,)).fetchone()
    if saved:
        admission = store.loads(saved, "admission_json")
        owner = admission["owner"]
        if owner["kind"] == "durable_task":
            from .roots import require_binding
            try:
                require_binding(conn, admission["run_id"], owner["thread_id"])
            except GxError as exc:
                _refuse("superseded_attempt", "the attempt no longer owns this launch",
                        task_id=owner["task_id"], cause=exc.code)


def complete(conn, job_id):
    """The historical completion is immutable even if current scientific eligibility changes."""
    head = conn.execute("SELECT admission_sha256,admission_json FROM p0_execution_manifests WHERE job_id=?",
                        (job_id,)).fetchone()
    if head is None:
        return  # Historical Jobs have no prospective P0 manifest.
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    out = store.loads(row, "outcome_json", {})
    runner = out.get("runner") or {}
    outputs = out.get("outputs") or []
    logs = [entry for entry in outputs if entry["kind"] in ("job_stdout", "job_stderr")]
    doc = {"schema": "gx.execution-manifest.completion/1", "admission_sha256": head["admission_sha256"],
           "job_id": job_id, "status": row["status"], "backend": row["scheduler_kind"],
           "backend_job_id": row["scheduler_ref"], "submitted_at": row["submitted_at"],
           "started_at": runner.get("started_at"), "finished_at": runner.get("finished_at") or row["finished_at"],
           "settled_at": row["finished_at"], "exit_code": row["exit_code"], "runner": runner,
           "measured": store.loads(row, "measured_json", {}), "gpu": out.get("gpu"),
           "outputs": [e for e in outputs if e["kind"] not in ("job_stdout", "job_stderr")],
           "logs": logs, "failure": out.get("failure"), "recovery_artifact": out.get("kept"),
           "current_at_completion": intent.job_current(conn, job_id)}
    if "output_declarations" in store.loads(head, "admission_json"):
        doc["schema"] = "gx.execution-manifest.completion/2"
    conn.execute("UPDATE p0_execution_manifests SET completion_json=?,completion_sha256=?"
                 " WHERE job_id=? AND completion_json IS NULL",
                 (ids.canonical_json(doc), ids.sha256_of_obj(doc), job_id))


def read_manifest(conn, job_id, *, missing_ok=False):
    row = conn.execute("SELECT * FROM p0_execution_manifests WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        if missing_ok:
            return None
        _refuse("missing_manifest", "no prospective execution manifest for this Job", job_id=job_id)
    admission = store.loads(row, "admission_json")
    completion = store.loads(row, "completion_json")
    if (ids.sha256_of_obj(admission) != row["admission_sha256"] or admission.get("job_id") != job_id
            or (completion is not None and (ids.sha256_of_obj(completion) != row["completion_sha256"]
                or completion.get("admission_sha256") != row["admission_sha256"]
                or completion.get("job_id") != job_id))):
        _refuse("integrity_mismatch", "execution manifest digest mismatch", job_id=job_id)
    return {"job_id": job_id, "admission": admission, "completion": completion,
            "admission_hash": row["admission_sha256"], "manifest_hash": row["completion_sha256"]}


def verify_artifact_pin(artifacts, pinned):
    verified = artifacts.verify(pinned["artifact_ref"])
    if not verified["ok"] or verified["observed_sha256"] != pinned["sha256"]:
        _refuse("integrity_mismatch", "artifact bytes differ from their recorded digest",
                artifact_ref=pinned["artifact_ref"])


def validate_evidence(conn, *, run_id, job_id, artifact_ref):
    """Validate the exact scientific output and original inputs against current GX authority."""
    with store.transaction(conn):
        manifest = read_manifest(conn, job_id)
        admission, result = manifest["admission"], manifest["completion"]
        if admission["run_id"] != run_id:
            _refuse("artifact_mismatch", "the Job belongs to another research run")
        if not result or result["status"] != "SUCCEEDED":
            _refuse("incomplete_job", "the Job has no successful execution receipt")
        output = next((e for e in result["outputs"] if e["artifact_ref"] == artifact_ref), None)
        if output is None:
            _refuse("artifact_mismatch", "this artifact is absent from the job's scientific outputs",
                    artifact_ref=artifact_ref, job_id=job_id)
        if "runner_sha256" in output and output["runner_sha256"] != output["sha256"]:
            _refuse("integrity_mismatch", "the published output does not match the detached runner's file receipt",
                    artifact_ref=artifact_ref, runner_sha256=output["runner_sha256"], published_sha256=output["sha256"])
        require_current(conn, job_id)
        artifacts = ArtifactService(conn)
        try:
            actual = artifacts.describe(artifact_ref)
            if actual["job_id"] != job_id or actual["run_id"] != run_id or actual["origin"] != "JOB_OUTPUT":
                _refuse("artifact_mismatch", "artifact ownership differs from the Job receipt")
            for pinned in [admission["input_manifest"], *admission["inputs"], output]:
                verify_artifact_pin(artifacts, pinned)
        except GxError as exc:
            if isinstance(exc, SchemaInvalid):
                raise
            _refuse("integrity_mismatch", "unable to verify the frozen execution artifact",
                    artifact_ref=artifact_ref, cause=exc.code)
        return {"job_id": job_id, "manifest_hash": manifest["manifest_hash"],
                "admission_hash": manifest["admission_hash"], "artifact_ref": artifact_ref, "sha256": output["sha256"]}
