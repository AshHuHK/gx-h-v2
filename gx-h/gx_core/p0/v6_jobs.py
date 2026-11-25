"""Fulfil accepted graph nodes with the existing GX Job owner, including after cold restart."""

from __future__ import annotations

from collections import Counter
import json

from ..capabilities import environments, manifest, registry, shipped, storage
from ..domain import clock, ids, machines
from ..errors import (
    ApprovalRequired,
    BudgetExceeded,
    DependencyUnmet,
    EnvNotReady,
    GxError,
    HoldActive,
    IdempotencyConflict,
    PhaseDenied,
    ResourceDenied,
    SchemaInvalid,
)
from ..jobs import cpu_queue
from ..jobs.service import JobService, PURPOSE_WORKFLOW, TERMINAL
from ..persistence import store
from . import manifest as execution
from . import research_window, v6_method_context, v62_evidence
from ..plugins import goal as goal_plugins

NODE_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "REUSED", "BLOCKED"}


def _tx(conn, run_id, operation):
    return store.transaction(conn, transaction_owner="V6Jobs", operation=operation, run_id=run_id)


def _roles(method, count):
    roles = manifest.input_roles(method)
    if "{inputs.json}" in method["entry"]["argv_template"]:
        extra = count - len(roles)
        if not 1 <= extra <= 8:
            raise SchemaInvalid("A grouped capability accepts one to eight additional inputs")
        roles += ["input_" + chr(ord("A") + i) for i in range(extra)]
    if len(roles) != count:
        raise SchemaInvalid(f"Capability requires the ordered input roles {roles}")
    return roles


def validate_node(conn, run_id, spec, *, node_ids=(), limits=None):
    """Check the declared call before adoption. The decision owner checks reference visibility."""
    capability = spec["capability"]
    execution.check_capability(conn, run_id, capability)
    view = registry.candidate(conn, capability)
    method = view["manifest"]
    goal = conn.execute("SELECT capabilities_json FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
    catalog = json.loads(goal[0]) if goal else {}
    if not isinstance(catalog, dict) or catalog.get(capability) != shipped.portable_digest(view["bundle"]):
        raise SchemaInvalid("Capability differs from this Goal's granted content pin")
    registry.active_view(conn, view, run_id=run_id)
    producers = dict(node_ids)
    assets = storage.artifact_service(conn)
    for ref in spec["inputs"]:
        if ref in producers:
            continue
        meta = assets.describe(ref)
        if meta["run_id"] != run_id or not assets.verify(ref)["ok"]:
            raise SchemaInvalid("Input is not an intact artifact in this Run")
    parameters = manifest.effective_parameters(method, spec["parameters"])
    provider = goal_plugins.method(conn, run_id, capability)
    if provider and provider.validate:
        ctx = v6_method_context.MethodContext(conn, run_id, spec["inputs"])
        v6_method_context.invoke(provider.validate, ctx, capability, spec["inputs"], parameters,
                                 producers=producers)
    else:
        _roles(method, len(spec["inputs"]))
        if set(parameters) != set(manifest.parameter_names(method)):
            raise SchemaInvalid(f"{capability} expects parameters {manifest.parameter_names(method)}")
        for name, value in parameters.items():
            manifest.coerce_parameter(name, value, whole_token=True)
    if limits is not None:
        required = JobService._budget_amounts(manifest.effective_resources(method, {}))
        short = {key: value for key, value in required.items() if value > limits.get(key, 0)}
        # A matched completed or incoming result costs this branch no new Job. The ready-time
        # execution fingerprint and actual ledger decide whether it can take that path.
        has_other_producer = any(
            name != spec["node"] and cap == capability for name, cap in producers.items()
        )
        if short and not has_other_producer:
            raise BudgetExceeded(
                "Branch ceiling cannot fund the proposed capability's minimum Job reservation",
                details={
                    "capability": capability,
                    "branch": spec["branch"],
                    "required": required,
                    "branch_limits": limits,
                    "shortfall_lines": sorted(short),
                },
            )
        return required


def _node(conn, run_id, node_id):
    return conn.execute(
        "SELECT n.*, b.status AS branch_status, b.spec_json AS branch_spec, "
        "g.status AS goal_status FROM v6_nodes n JOIN v6_branches b ON b.branch_id=n.branch_id "
        "JOIN v6_goals g ON g.run_id=n.run_id WHERE n.run_id=? AND n.node_id=?",
        (run_id, node_id),
    ).fetchone()


def _live(node, expected):
    if (
        not node
        or node["status"] != expected
        or node["branch_status"] != "OPEN"
        or node["goal_status"] != "OPEN"
    ):
        raise SchemaInvalid("Graph delegation is no longer active", details={"reason": "superseded_attempt"})


def _runtime_identity(runtime):
    # A device availability observation is made after the execution plan is sealed.
    gpu = runtime.get("gpu")
    return {**runtime, "gpu": gpu["requested"] if gpu else None}


def confirmed_result_identity(conn, node_id, proof, *, verify_inputs=False):
    """Bind a published (or reused) result to its frozen inputs, environment and original Job."""
    attempt = conn.execute(
        "SELECT n.run_id,n.status,n.job_id,n.exec_key,a.exec_key AS attempt_key,a.identity_json,a.reused_from "
        "FROM v6_nodes n JOIN v6_job_attempts a USING(node_id) WHERE n.node_id=?", (node_id,)
    ).fetchone()
    admission, completion = proof["admission"], proof["completion"]
    owner = admission["owner"]
    if (not attempt or attempt["status"] not in {"SUCCEEDED", "REUSED"}
            or attempt["job_id"] != proof["job_id"] or attempt["run_id"] != admission["run_id"]
            or not completion or completion["status"] != "SUCCEEDED"
            or owner.get("kind") != "graph_node"
            or owner.get("node_id") != (attempt["reused_from"] or node_id)):
        raise SchemaInvalid("The successful Job is not bound to this graph result")
    identity = json.loads(attempt["identity_json"])
    method = admission["capability"]
    roles = [[entry["role"], entry["sha256"]] for entry in admission["inputs"]]
    environment = _runtime_identity(admission["runtime"])
    if (ids.sha256_of_obj(identity) != attempt["attempt_key"] or attempt["attempt_key"] != attempt["exec_key"]
            or attempt["exec_key"] != owner.get("exec_key")
            or identity["capability"] != f"{method['id']}@{method['version']}"
            or identity["manifest_sha256"] != method["sha256"] or identity["roles"] != roles
            or identity["parameters"] != admission["parameters"]
            or _runtime_identity(identity["runtime"]) != environment):
        raise SchemaInvalid("Graph execution identity differs from the Job's admitted inputs or runtime")
    if verify_inputs:
        if conn.in_transaction:
            raise RuntimeError("Verify original input files outside the graph writer")
        artifacts = storage.artifact_service(conn)
        for entry in (admission["input_manifest"], *admission["inputs"]):
            verified = artifacts.verify(entry["artifact_ref"])
            if not verified["ok"] or verified["observed_sha256"] != entry["sha256"]:
                raise SchemaInvalid("Original Job input differs from its admission receipt")
    return {"exec_key": attempt["exec_key"], "input_sha256": admission["input_manifest"]["sha256"],
            "environment_sha256": ids.sha256_of_obj(environment)}


def _dependencies(conn, node, spec, *, check_all=False):
    resolved = {}
    waiting = False
    for ref in dict.fromkeys([*spec["depends_on"], *spec["inputs"]]):
        dep = conn.execute(
            "SELECT status,result_ref FROM v6_nodes WHERE run_id=? AND node_id=?", (node["run_id"], ref)
        ).fetchone()
        if dep:
            if dep["status"] in {"FAILED", "CANCELLED", "BLOCKED"}:
                details = {"node": ref}
                if check_all and dep["status"] == "BLOCKED":
                    excluded = conn.execute("""SELECT event_id FROM events WHERE run_id=? AND kind='v6.node.settled'
                        AND subject_id=? AND (json_extract(payload_json,'$.condition.reason')='evidence_condition_false'
                        OR json_extract(payload_json,'$.dependency.reason')='evidence_route_blocked') LIMIT 1""",
                        (node["run_id"], ref)).fetchone()
                    if excluded:
                        details.update(reason="evidence_route_blocked", settlement_event=excluded[0])
                raise DependencyUnmet("A declared dependency ended without a usable result", details=details)
            if dep["status"] not in {"REUSED", "SUCCEEDED"}:
                if not check_all:
                    return None
                waiting = True
                continue
            resolved[ref] = dep["result_ref"]
    return None if waiting else [resolved.get(ref, ref) for ref in spec["inputs"]]


def _compile(conn, node, refs):
    spec = json.loads(node["spec_json"])
    cap = spec["capability"]
    method = registry.candidate(conn, cap)
    parameters = manifest.effective_parameters(method["manifest"], spec["parameters"])
    provider = goal_plugins.method(conn, node["run_id"], cap)
    if provider and provider.bind:
        ctx = v6_method_context.MethodContext(conn, node["run_id"], refs, writable=True)
        inputs, params = v6_method_context.invoke(provider.bind, ctx, cap, refs, parameters)
    else:
        inputs = dict(zip(_roles(method["manifest"], len(refs)), refs))
        params = parameters
    artifacts = storage.artifact_service(conn)
    entries = [{"role": key, "artifact_ref": ref} for key, ref in inputs.items()]
    inputs_ref = artifacts.make_input_manifest(
        namespace=f"{node['run_id']}/v6/inputs", run_id=node["run_id"], entries=entries
    ).artifact_ref
    entries = artifacts.read_input_manifest(inputs_ref)["entries"]
    runtime = environments.require_ready(conn, method["env_id"])
    resources = manifest.effective_resources(method["manifest"], {})
    env = execution._runtime(runtime, resources, {})
    identity = {
        "version": 1,
        "capability": cap,
        "manifest_sha256": method["manifest_sha256"],
        "roles": [[entry["role"], entry["sha256"]] for entry in entries],
        "parameters": params,
        "runtime": env,
        "method_dependencies": v6_method_context.dependencies(conn, node["run_id"], cap, runtime["python_executable"]),
    }
    exec_key = ids.sha256_of_obj(identity)
    plan_key = ids.sha256_of_obj(
        {"run": node["run_id"], "graph": node["version"], "node": node["node_id"], "spec": spec}
    )
    binding = {
        "v6_node_id": node["node_id"],
        "node_id": node["node_id"],
        "decision_ref": node["decision_ref"],
        "branch_id": node["branch_id"],
        "plan_key": plan_key,
        "exec_key": exec_key,
    }
    request = {
        "run_id": node["run_id"],
        "path_id": None,
        "task_id": None,
        "purpose": PURPOSE_WORKFLOW,
        "input_manifest_ref": inputs_ref,
        "binding": binding,
        "parameters": params,
        "tighten": {},
        "capability_version": cap,
    }
    with _tx(conn, node["run_id"], "prepare_node_job") as tx:
        _live(_node(tx, node["run_id"], node["node_id"]), "PENDING")
        tx.execute(
            "INSERT INTO v6_job_attempts(node_id,run_id,operation_key,plan_key,exec_key,identity_json,"
            "request_json,request_sha256) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(node_id) DO NOTHING",
            (
                node["node_id"],
                node["run_id"],
                node["operation_key"],
                plan_key,
                exec_key,
                ids.canonical_json(identity),
                ids.canonical_json(request),
                ids.sha256_of_obj(request),
            ),
        )
        tx.execute(
            "UPDATE v6_nodes SET exec_key=(SELECT exec_key FROM v6_job_attempts WHERE node_id=?) "
            "WHERE node_id=?",
            (node["node_id"], node["node_id"]),
        )


def branch_exposure(conn, branch_id):
    """Booked usage and held maximum from original JobService reservations in this branch."""
    rows = conn.execute(
        "SELECT r.amounts_json,r.status,e.payload_json FROM v6_nodes n "
        "JOIN v6_job_attempts a ON a.node_id=n.node_id AND a.reused_from IS NULL "
        "JOIN jobs j ON j.job_id=n.job_id JOIN budget_reservations r ON r.reservation_ref=j.reservation_ref "
        "LEFT JOIN events e ON e.kind='budget.settled' AND e.subject_kind='budget' AND e.subject_id=r.budget_ref "
        "AND json_extract(e.payload_json,'$.reservation_ref')=r.reservation_ref "
        "WHERE n.branch_id=?",
        (branch_id,),
    )
    used = Counter()
    for row in rows:
        if row["status"] == "RELEASED":
            continue
        exposure = (
            json.loads(row["payload_json"])["actual"]
            if row["payload_json"]
            else json.loads(row["amounts_json"])
        )
        used.update(exposure)
    return used


def _branch_room(conn, node, amounts):
    used = branch_exposure(conn, node["branch_id"])
    limits = json.loads(node["branch_spec"])["budget_limits"]
    for key, amount in amounts.items():
        if used[key] + amount > limits.get(key, 0):
            raise BudgetExceeded(
                "Branch allowance cannot fund this Job",
                details={
                    "branch_id": node["branch_id"],
                    "line": key,
                    "held_or_spent": used[key],
                    "needed": amount,
                    "limit": limits.get(key, 0),
                },
            )


def fence_admission(conn, request, request_sha, key, job_id, amounts, runtime, method_dependencies):
    """The Job writer calls this after inserting the row, inside its own admission transaction."""
    binding = request.get("binding") or {}
    node = _node(conn, request["run_id"], binding.get("v6_node_id"))
    _live(node, "PENDING")
    _module_grant(conn, node, json.loads(node["spec_json"]))
    run_status = conn.execute("SELECT status FROM runs WHERE run_id=?", (request["run_id"],)).fetchone()[0]
    if run_status not in machines.machine("run")["executable"]:
        raise PhaseDenied("Run is no longer accepting Jobs", details={"status": run_status})
    attempt = conn.execute("SELECT * FROM v6_job_attempts WHERE node_id=?", (node["node_id"],)).fetchone()
    objective = conn.execute(
        "SELECT version,status FROM p0_intent_versions WHERE run_id=? AND item_id='objective' "
        "ORDER BY version DESC LIMIT 1",
        (node["run_id"],),
    ).fetchone()
    goal_version = conn.execute(
        "SELECT goal_version FROM v6_goals WHERE run_id=?", (node["run_id"],)
    ).fetchone()[0]
    if objective and (objective["status"] != "active" or objective["version"] != goal_version):
        raise SchemaInvalid("The graph's Goal has been revised", details={"reason": "superseded_attempt"})
    plan = ids.sha256_of_obj(
        {
            "run": node["run_id"],
            "graph": node["version"],
            "node": node["node_id"],
            "spec": json.loads(node["spec_json"]),
        }
    )
    if (
        not attempt
        or attempt["request_sha256"] != request_sha
        or attempt["plan_key"] != plan
        or key != f"job:{node['run_id']}:{node['operation_key']}"
    ):
        raise IdempotencyConflict("Job request differs from its admitted graph assignment")
    identity = json.loads(attempt["identity_json"])
    if identity.get("method_dependencies", {}) != method_dependencies:
        raise SchemaInvalid("Method dependencies changed after the graph node was prepared")
    compiled_env = identity["runtime"]
    # GPU availability is a launch observation. Runtime environment identity is frozen separately.
    if _runtime_identity(compiled_env) != _runtime_identity(runtime):
        raise SchemaInvalid("Execution environment changed after the node was compiled")
    equivalent = conn.execute(
        "SELECT node_id FROM v6_nodes WHERE run_id=? AND exec_key=? AND node_id<>? "
        "AND (status IN ('RUNNING','SUCCEEDED','REUSED') OR (status='CANCEL_REQUESTED' AND job_id IS NOT NULL)) LIMIT 1",
        (node["run_id"], attempt["exec_key"], node["node_id"]),
    ).fetchone()
    if equivalent:
        raise ResourceDenied(
            "Equivalent workload already has an owner", details={"equivalent": equivalent[0]}
        )
    _branch_room(conn, node, amounts)
    conn.execute("UPDATE v6_nodes SET status='RUNNING',job_id=? WHERE node_id=?", (job_id, node["node_id"]))
    _consume(conn, node)


def _module_grant(conn, node, spec):
    """Submitted graph work has its own identity; honor the module and source grant at admission."""
    branch = json.loads(node["branch_spec"])
    if "module_instance_id" in branch:
        from . import v62_modules
        module = v62_modules.require_active(conn, node["run_id"], "module:" + branch["module_instance_id"])
        if module["branch_id"] != node["branch_id"] or spec["capability"] not in json.loads(module["grant_json"])["capabilities"]:
            from ..errors import ScopeDenied
            raise ScopeDenied("This module Job lost its capability grant")


def fence_launch(conn, request, job_id):
    node = _node(conn, request["run_id"], request["binding"]["v6_node_id"])
    _live(node, "RUNNING")
    _module_grant(conn, node, json.loads(node["spec_json"]))
    if node["job_id"] != job_id:
        raise SchemaInvalid("Node now belongs to another Job", details={"reason": "superseded_attempt"})


def _consume(conn, node):
    conn.execute(
        "UPDATE outbox SET consumed_at=?,consumer='V6Jobs' WHERE topic='v6.node' "
        "AND run_id=? AND consumed_at IS NULL AND json_extract(payload_json,'$.node_id')=?",
        (clock.now_iso(), node["run_id"], node["node_id"]),
    )
    conn.execute("DELETE FROM v6_node_waits WHERE node_id=?", (node["node_id"],))
    cpu_queue.admitted(conn, f"job:{node['run_id']}:{node['operation_key']}")


def _finish(conn, node, status, *, result=None, job_id=None, source=None, job_status=None, reason=None, evidence=None, condition=None, dependency=None):
    with _tx(conn, node["run_id"], "settle_node") as tx:
        current = _node(tx, node["run_id"], node["node_id"])
        if current["status"] in NODE_TERMINAL:
            return False
        if current["job_id"] and current["job_id"] != job_id:
            return False
        if condition and current["status"] != "CANCEL_REQUESTED":
            _live(current, "PENDING")
            _module_grant(tx, current, json.loads(current["spec_json"]))
            v62_evidence.fence_negative(tx, current, condition)
        if source:
            _live(current, "PENDING")
            spec = json.loads(current["spec_json"])
            _module_grant(tx, current, spec)
            if spec.get("evidence_gate"):
                v62_evidence.fence(tx, current, evidence)
            execution.require_current(tx, job_id)
        if current["status"] == "CANCEL_REQUESTED":
            status = "CANCELLED"  # Keep late result and actual Job status on their original identity.
        tx.execute(
            "UPDATE v6_nodes SET status=?,result_ref=?,job_id=COALESCE(job_id,?) WHERE node_id=?",
            (status, result, job_id, node["node_id"]),
        )
        tx.execute("UPDATE v6_job_attempts SET reused_from=? WHERE node_id=?", (source, node["node_id"]))
        _consume(tx, node)
        store.append_event(
            tx,
            kind="v6.node.settled",
            actor="gx",
            run_id=node["run_id"],
            subject_kind="node",
            subject_id=node["node_id"],
            prev_state=current["status"],
            new_state=status,
            reason=reason,
            payload={
                "node_id": node["node_id"],
                "branch_id": node["branch_id"],
                "status": status,
                "observed_job_status": job_status,
                "decision_ref": node["decision_ref"],
                "operation_key": node["operation_key"],
                "job_id": job_id,
                "result_ref": result,
                "exec_key": current["exec_key"],
                "reused_from": source,
                **({"condition": condition} if condition and status == "BLOCKED" else {}),
                **({"dependency": dependency} if dependency and status == "BLOCKED" else {}),
            },
        )
        return True


def _record_wait(conn, node, error):
    reason = {"code": error.code, "message": error.message, "details": error.details}
    # Keep volatile queue sampling out of the durable deduplication identity.
    wait = {k: reason[k] for k in ("code", "message")}
    details = reason["details"]
    if details.get("reason") in {"evidence_pending", "evidence_invalid"}:
        from . import v621_policy
        if v621_policy.enabled(conn, node["run_id"]):
            wait = reason
    if details.get("rule") == "cpu.threads_free":
        wait = {"code": error.code, "rule": "cpu.threads_free", "needed": details["needed"]}
    encoded = ids.canonical_json(wait)
    prior = conn.execute("SELECT wait_json FROM v6_node_waits WHERE node_id=?", (node["node_id"],)).fetchone()
    if prior and prior[0] == encoded:
        return
    with _tx(conn, node["run_id"], "park_node") as tx:
        cur = _node(tx, node["run_id"], node["node_id"])
        if cur["status"] not in ("PENDING", "RUNNING"):
            return
        changed = tx.execute(
            "INSERT INTO v6_node_waits VALUES(?,?) ON CONFLICT(node_id) DO UPDATE "
            "SET wait_json=excluded.wait_json WHERE wait_json<>excluded.wait_json",
            (node["node_id"], encoded),
        )
        if not changed.rowcount:
            return
        event = store.append_event(
            tx,
            kind="v6.node.waiting",
            actor="gx",
            run_id=node["run_id"],
            subject_kind="node",
            subject_id=node["node_id"],
            payload=reason,
        )
        if details.get("rule") == "cpu.threads_free" and details.get("job_key"):
            cpu_queue.park(
                tx,
                node["run_id"],
                node["node_id"],
                details["job_key"],
                details["needed"],
                clock.now_iso(),
                event,
            )


def _active(conn, node, service):
    """Recover by accepted execution identity. OUTCOME_UNKNOWN keeps its hold and remains active."""
    view = service.status(node["job_id"])
    if node["status"] == "CANCEL_REQUESTED" and not view["terminal"]:
        service.cancel(
            node["job_id"], reason="GX decision withdrew this node", idempotency_key=node["operation_key"]
        )
        view = service.status(node["job_id"])
    if view["terminal"]:
        result = None
        status = view["status"]
        failure = None
        if status == "SUCCEEDED":
            # Use the actual immutable Job completion, even when the package is no longer installed.
            completion = execution.read_manifest(conn, view["job_id"])["completion"]
            result = next((row["artifact_ref"] for row in completion["outputs"]), None)
            if not result:
                failure = "Job completed without a scientific output"
                status = "FAILED"
        finished = _finish(
            conn,
            node,
            status,
            job_id=view["job_id"],
            result=result,
            job_status=view["status"],
            reason=failure,
        )
        return "settled" if finished else "waiting"
    if view["status"] == "CREATED" and node["status"] == "RUNNING":
        # The process may have died immediately after Job adoption, before _start.
        attempt = conn.execute(
            "SELECT request_json FROM v6_job_attempts WHERE node_id=?", (node["node_id"],)
        ).fetchone()
        service.submit(**json.loads(attempt[0]), idempotency_key=node["operation_key"])
    return "waiting"


def _run_node(conn, node, service, window):
    if node["job_id"]:
        return _active(conn, node, service)
    if node["status"] == "CANCEL_REQUESTED":
        return "cancelled" if _finish(conn, node, "CANCELLED") else "waiting"
    _live(node, "PENDING")
    research_window.require_open(window)
    spec = json.loads(node["spec_json"])
    _module_grant(conn, node, spec)
    from . import v621_policy
    fixed_conditions = v621_policy.enabled(conn, node["run_id"])
    if fixed_conditions:
        refs = _dependencies(conn, node, spec, check_all=True)
    evidence = None
    if spec.get("evidence_gate"):
        try:
            evidence = v62_evidence.require_ready(conn, node, spec)
        except v62_evidence.ConditionFalse as error:
            settled = _finish(conn, node, "BLOCKED", condition=error.details, reason=error.details["reason"])
            return "settled" if settled else "waiting"
    if not fixed_conditions:
        refs = _dependencies(conn, node, spec)
    if refs is None:
        return "waiting"
    attempt = conn.execute("SELECT * FROM v6_job_attempts WHERE node_id=?", (node["node_id"],)).fetchone()
    if not attempt:
        _compile(conn, node, refs)
        attempt = conn.execute("SELECT * FROM v6_job_attempts WHERE node_id=?", (node["node_id"],)).fetchone()
    source = conn.execute(
        "SELECT * FROM v6_nodes WHERE run_id=? AND node_id<>? AND exec_key=? AND "
        "status='SUCCEEDED' AND result_ref IS NOT NULL ORDER BY rowid LIMIT 1",
        (node["run_id"], node["node_id"], attempt["exec_key"]),
    ).fetchone()
    if source:
        # A previously parked attempt may resume under a changed interpreter or SVD distribution.
        identity = json.loads(attempt["identity_json"])
        method = registry.candidate(conn, spec["capability"])
        active = environments.require_ready(conn, method["env_id"])
        current = execution._runtime(active, manifest.effective_resources(method["manifest"], {}), {})
        if (
            identity["manifest_sha256"] != method["manifest_sha256"]
            or identity["runtime"] != current
            or identity.get("method_dependencies", {})
            != v6_method_context.dependencies(conn, node["run_id"], spec["capability"], active["python_executable"])
        ):
            raise SchemaInvalid("Compiled attempt is incompatible with the currently installed method")
        # Hash the original bytes outside a write transaction; adoption retains the original Job identity.
        proof = execution.read_manifest(conn, source["job_id"])
        confirmed_result_identity(conn, source["node_id"], proof, verify_inputs=True)
        completion = proof["completion"]
        output = next(
            (r for r in (completion or {}).get("outputs", []) if r["artifact_ref"] == source["result_ref"]),
            None,
        )
        verified = storage.artifact_service(conn).verify(source["result_ref"])
        if (
            not output
            or completion["status"] != "SUCCEEDED"
            or not verified["ok"]
            or verified["observed_sha256"] != output["sha256"]
        ):
            raise SchemaInvalid("Prior execution has no verifiable primary artifact")
        execution.require_current(conn, source["job_id"])
        return (
            "reused"
            if _finish(
                conn,
                node,
                "REUSED",
                source=source["node_id"],
                evidence=evidence,
                job_id=source["job_id"],
                job_status="SUCCEEDED",
                result=source["result_ref"],
            )
            else "waiting"
        )
    result = service.submit(**json.loads(attempt["request_json"]), idempotency_key=node["operation_key"])
    return "waiting" if result["replayed"] else "dispatched"


def _finalize(conn, run_id):
    scopes = conn.execute(
        "SELECT branch_id FROM v6_branches WHERE run_id=? AND status='STOPPING'", (run_id,)
    ).fetchall()
    close = conn.execute("SELECT 1 FROM v6_goals WHERE run_id=? AND status='CLOSING'", (run_id,)).fetchone()
    if not scopes and not close:
        return
    with _tx(conn, run_id, "finish_withdrawal") as tx:
        for scope in scopes:
            branch = scope["branch_id"]
            if tx.execute(
                "SELECT 1 FROM v6_nodes WHERE branch_id=? AND status IN "
                "('PENDING','RUNNING','CANCEL_REQUESTED') LIMIT 1",
                (branch,),
            ).fetchone():
                continue
            stop = tx.execute(
                "UPDATE v6_branches SET status='STOPPED' WHERE branch_id=? AND status='STOPPING' "
                "RETURNING stop_decision_ref,stop_op_digest",
                (branch,),
            ).fetchone()
            if stop:
                store.append_event(
                    tx,
                    kind="v6.branch.stopped",
                    actor="gx",
                    run_id=run_id,
                    subject_kind="branch",
                    subject_id=branch,
                    prev_state="STOPPING",
                    new_state="STOPPED",
                    payload={"decision_id": stop[0], "op_digest": stop[1]},
                )
        if (
            close
            and not tx.execute(
                "SELECT 1 FROM v6_nodes WHERE run_id=? AND status IN "
                "('PENDING','RUNNING','CANCEL_REQUESTED') LIMIT 1",
                (run_id,),
            ).fetchone()
        ):
            job_states = tuple(TERMINAL)
            if tx.execute(
                "SELECT 1 FROM jobs WHERE run_id=? AND status NOT IN ("
                + ",".join("?" for _ in job_states)
                + ") LIMIT 1",
                (run_id, *job_states),
            ).fetchone():
                return
            stop = tx.execute(
                "UPDATE v6_goals SET status='CLOSED' WHERE run_id=? AND status='CLOSING' "
                "RETURNING stop_decision_ref,stop_op_digest",
                (run_id,),
            ).fetchone()
            if stop:
                store.append_event(
                    tx,
                    kind="v6.run.closed",
                    actor="gx",
                    run_id=run_id,
                    subject_kind="run",
                    subject_id=run_id,
                    prev_state="CLOSING",
                    new_state="CLOSED",
                    payload={"decision_id": stop[0], "op_digest": stop[1]},
                )
                run_status = tx.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
                if run_status not in machines.terminal_states("run"):
                    store.transition(
                        tx,
                        machine="run",
                        subject_id=run_id,
                        to_state="STOPPED",
                        actor="gx",
                        reason="Accepted research decision closed the run",
                        payload={"decision_id": stop[0], "op_digest": stop[1]},
                    )


def advance(conn, run_id):
    """Advance committed nodes once without a surrounding SQLite writer; other branches can proceed."""
    if conn.in_transaction:
        raise RuntimeError("Dynamic Jobs must be dispatched after the graph transaction commits")
    counts = dict.fromkeys(("dispatched", "settled", "reused", "cancelled", "waiting"), 0)
    work = conn.execute(
        "SELECT node_id FROM v6_nodes WHERE run_id=? AND status IN "
        "('CANCEL_REQUESTED','RUNNING','PENDING') ORDER BY CASE status WHEN "
        "'CANCEL_REQUESTED' THEN 0 WHEN 'RUNNING' THEN 1 ELSE 2 END,rowid",
        (run_id,),
    ).fetchall()
    service = JobService(conn)
    window = research_window.for_run(conn, run_id) if work else None
    for (node_id,) in work:
        node = _node(conn, run_id, node_id)
        if node["status"] in NODE_TERMINAL:
            continue
        try:
            counts[_run_node(conn, node, service, window)] += 1
        except (
            BudgetExceeded,
            ResourceDenied,
            EnvNotReady,
            HoldActive,
            ApprovalRequired,
            PhaseDenied,
        ) as error:
            _record_wait(conn, node, error)
            counts["waiting"] += 1
        except GxError as error:
            fresh = _node(conn, run_id, node_id)
            if error.details.get("reason") == "superseded_attempt" or fresh["status"] in NODE_TERMINAL:
                continue
            if fresh["job_id"]:
                _record_wait(conn, fresh, error)
                counts["waiting"] += 1
                continue
            status = "BLOCKED" if isinstance(error, DependencyUnmet) else "FAILED"
            dependency = error.details if error.details.get("reason") == "evidence_route_blocked" else None
            if _finish(conn, node, status, reason=f"{error.code}: {error.message}", dependency=dependency):
                counts["settled"] += 1
    _finalize(conn, run_id)
    return counts
