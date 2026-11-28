"""Fulfil accepted graph nodes with the existing GX Job owner, including after cold restart."""

from __future__ import annotations

from collections import Counter
import json

from ..capabilities import environments, input_domain, manifest, registry, shipped, storage
from ..domain import clock, ids, machines
from ..errors import (
    ApprovalRequired,
    ArtifactHashMismatch,
    ArtifactNotFound,
    BudgetExceeded,
    DependencyUnmet,
    EnvNotReady,
    GxError,
    HoldActive,
    IdempotencyConflict,
    PhaseDenied,
    ResourceDenied,
    SchemaInvalid,
    ScopeDenied,
)
from ..jobs import cpu_queue
from ..jobs import diagnostics
from ..jobs.service import JobService, PURPOSE_WORKFLOW, TERMINAL
from ..persistence import store
from . import manifest as execution
from . import harness_policy, module_methods, research_window, v6_method_context, v6_prospective, v62_evidence
from ..plugins import goal as goal_plugins

NODE_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "REUSED", "BLOCKED"}


def _tx(conn, run_id, operation):
    return store.transaction(conn, transaction_owner="V6Jobs", operation=operation, run_id=run_id)


def _roles(method, count):
    roles = manifest.input_roles(method)
    if "{inputs.json}" in method["entry"]["argv_template"]:
        extra = count - len(roles)
        if not 1 <= extra <= 8:
            raise SchemaInvalid("A grouped capability accepts one to eight additional inputs", details={
                "declared_input_roles": roles, "additional_input_range": [1, 8], "input_count": count})
        roles += ["input_" + chr(ord("A") + i) for i in range(extra)]
    if len(roles) != count:
        raise SchemaInvalid(f"Capability requires the ordered input roles {roles}", details={
            "declared_input_roles": roles, "input_count": count, "correction": "Supply each required role in order."})
    return roles


def validate_node(conn, run_id, spec, *, node_ids=(), limits=None):
    """Check the declared call before adoption. The decision owner checks reference visibility."""
    capability = spec["capability"]
    local = module_methods.for_spec(conn, run_id, spec)
    execution.check_capability(conn, run_id, capability, module_binding=local)
    view = registry.candidate(conn, capability)
    method = view["manifest"]
    goal = conn.execute("SELECT capabilities_json FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
    catalog = json.loads(goal[0]) if goal else {}
    expected = local["descriptor"]["portable_sha256"] if local else catalog.get(capability)
    if expected != shipped.portable_digest(view["bundle"]):
        raise SchemaInvalid("Capability differs from this Goal's granted content pin")
    registry.active_view(conn, view, run_id=run_id, module_binding=local)
    producers = dict(node_ids)
    ports = _ports(spec)
    for index, name in ports.items():
        producer = spec["inputs"][index]
        if producer not in spec["depends_on"] or producer not in producers:
            raise SchemaInvalid("A named output needs a declared upstream node dependency", details={
                "input_index": index, "upstream": producer, "requested_output": name,
                "expected_type": "upstream_node",
                "correction": "Use an upstream node with a depends_on edge, or omit input_ports for an artifact."})
        declaration = registry.candidate(conn, producers[producer])["manifest"]
        available = [out for out in declaration["outputs"] if not manifest.is_dir_output(out)]
        if not any(out["name"] == name for out in available):
            raise SchemaInvalid(f"{name!r} is not a declared file output of {producers[producer]}", details={
                "input_index": index, "upstream": producer, "requested_output": name,
                "expected_type": "named_file_output",
                "available_file_outputs": [{k: out[k] for k in ("name", "kind", "media_type") if k in out}
                                           for out in available],
                "correction": "Bind a declared file output by its exact name."})
    assets = storage.artifact_service(conn)
    for ref in spec["inputs"]:
        if ref in producers:
            continue
        meta = assets.describe(ref)
        if meta["run_id"] != run_id or not assets.verify(ref)["ok"]:
            raise SchemaInvalid("Input is not an intact artifact in this Run")
    parameters = manifest.effective_parameters(method, spec["parameters"])
    provider = goal_plugins.method(conn, run_id, capability, module_binding=local)
    if provider and provider.validate:
        ctx = v6_method_context.MethodContext(conn, run_id, spec["inputs"])
        try:
            v6_method_context.invoke(provider.validate, ctx, capability, spec["inputs"], parameters,
                                     producers=producers)
        except SchemaInvalid as exc:
            exc.details.setdefault("declared_input_roles", manifest.input_roles(method))
            exc.details.setdefault("input_count", len(spec["inputs"]))
            raise
    else:
        _roles(method, len(spec["inputs"]))
        if set(parameters) != set(manifest.parameter_names(method)):
            raise SchemaInvalid(f"{capability} expects parameters {manifest.parameter_names(method)}")
        for name, value in parameters.items():
            manifest.coerce_parameter(name, value, whole_token=True)
    domains = input_domain.declarations(method["method_card"].get("applicable_domain"), method.get("applicable_domain"))
    if domains:
        if not (provider and provider.bind):
            bindings = dict(zip(_roles(method, len(spec["inputs"])), spec["inputs"], strict=True))
            available = {role: ref for role, ref in bindings.items() if ref not in producers}
            input_domain.require(domains, available, input_domain.InputFacts(assets, run_id), resolved=len(available) == len(bindings))
        elif not any(ref in producers for ref in spec["inputs"]):
            try:
                bind_inputs(conn, run_id, capability, view, spec["inputs"], parameters, writable=False, module_binding=local)
            except ScopeDenied as error:
                if error.details.get("reason") != "binding_needs_materialization":
                    raise
    if limits is not None:
        required = JobService._budget_amounts(manifest.effective_resources(method, {}))
        short = {key: value for key, value in required.items() if value > limits.get(key, 0)}
        # A matched completed or incoming result costs this branch no new Job. The ready-time
        # execution fingerprint and actual ledger decide whether it can take that path.
        has_other_producer = any(
            name != spec["node"] and cap == capability for name, cap in producers.items()
        )
        from . import harness_policy
        if short and not has_other_producer and not harness_policy.active(conn, run_id):
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
            or (identity["version"] == 1 and identity.get("module_provider")
                != admission.get("module_capability", {}).get("plugin"))
            or (identity["version"] == 2 and (identity["provider"] != admission.get("execution_provider")
                                               or identity["program_sha256"] != admission.get("program_sha256")))
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


def _ports(spec):
    ports = {}
    inputs = spec["inputs"]
    for entry in spec.get("input_ports", ()):
        index = entry["index"]
        if type(index) is not int or not 0 <= index < len(inputs) or index in ports:
            raise SchemaInvalid("Each input port needs one distinct in-range input index", details={
                "input_index": index, "input_count": len(inputs),
                "correction": "Number inputs from zero and bind at most one named output per position."})
        ports[index] = entry["output"]
    if len({(ref, ports.get(i)) for i, ref in enumerate(inputs)}) != len(inputs):
        raise SchemaInvalid("Repeated graph input: select different outputs or supply distinct inputs")
    return ports


def _dependencies(conn, node, spec, *, check_all=False):
    resolved = {}
    waiting = False
    for ref in dict.fromkeys([*spec["depends_on"], *spec["inputs"]]):
        dep = conn.execute(
            "SELECT status,result_ref,job_id FROM v6_nodes WHERE run_id=? AND node_id=?", (node["run_id"], ref)
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
            resolved[ref] = dep
    if waiting:
        return None
    ports = _ports(spec)
    outputs = {}
    refs = []
    for index, ref in enumerate(spec["inputs"]):
        dep = resolved.get(ref)
        if index not in ports:
            refs.append(dep["result_ref"] if dep else ref)
            continue
        if not dep or not dep["job_id"]:
            raise DependencyUnmet("Named input has no successful upstream Job", details={"node": ref})
        if ref not in outputs:
            from . import v62_outputs
            proof = execution.read_manifest(conn, dep["job_id"])
            confirmed_result_identity(conn, ref, proof)
            inventory, _ = v62_outputs.recorded_outputs(conn, node["run_id"], dep["job_id"],
                                                        record=proof, check_publications=True)
            outputs[ref] = inventory
        matching = [row for row in outputs[ref] if row["output_name"] == ports[index]]
        if len(matching) != 1 or matching[0].get("runner_sha256", matching[0]["sha256"]) != matching[0]["sha256"]:
            raise DependencyUnmet(f"Upstream Job has no unique verified output {ports[index]!r}",
                                  details={"node": ref, "consumer": node["node_id"], "input_index": index,
                                           "output": ports[index],
                                           "recorded_outputs": sorted(row["output_name"] for row in outputs[ref])})
        refs.append(matching[0]["artifact_ref"])
    return refs


def _small_data_pins(paths):
    # Hash small release assets without adding a scan of a shared dataset or
    # model-weight directory to ordinary Job admission.
    remaining = 1024 * 1024
    pins = {}
    for item in paths:
        try:
            path = manifest.resolve_path(item)
            if not path.is_file() or path.stat().st_size > remaining:
                return None
            remaining -= path.stat().st_size
            pins[item] = ids.sha256_of_file(path)
        except (GxError, OSError, KeyError):
            return None
    return pins


def program_pins(method, environment):
    """Only executables used by this method participate in its scientific identity."""
    view = manifest.EnvView(env_id=environment["env_id"], python=environment["python_executable"],
                            binaries=environment["binary_paths"], prefix=environment["prefix"])
    names = {name.removeprefix("env.bin.") for arg in method["entry"]["argv_template"]
             for name in manifest.placeholders(arg) if name.startswith("env.bin.")}
    pins = {}
    try:
        for name in sorted(names):
            pins[name] = ids.sha256_of_file(view.binaries[name])
        source = method["source"]
        if source.get("path") and source.get("sha256"):
            path = manifest.resolve_path(source["path"], env=view)
            if not path.is_file():
                return None
            pins["source_sha256"] = ids.sha256_of_file(path)
            if pins["source_sha256"] != source["sha256"]:
                return None
    except (OSError, KeyError):
        return None
    return pins


def execution_provider(conn, run_id, cap, module_binding):
    descriptor = module_binding["descriptor"] if module_binding else next(
        (d for d in goal_plugins.catalog(conn, run_id) if d["capability_version"] == cap), {})
    return goal_plugins.effective_provider(descriptor)


def method_identity(conn, run_id, cap, method, entries, params, *, module_binding=None):
    runtime = environments.require_ready(conn, method["env_id"])
    resources = manifest.effective_resources(method["manifest"], {})
    env = execution._runtime(runtime, resources, {})
    identity = {
        "version": 1,
        "capability": cap,
        "manifest_sha256": method["manifest_sha256"],
        "roles": [[entry["role"], entry["sha256"]] for entry in entries],
        "parameters": params,
        **({"module_provider": module_binding["descriptor"].get("plugin")} if module_binding else {}),
        "runtime": env,
        "method_dependencies": v6_method_context.dependencies(conn, run_id, cap, runtime["python_executable"], module_binding=module_binding),
        **({"required_data_sha256": _small_data_pins(method["manifest"]["required_data"])}
           if method["manifest"].get("required_data") else {}),
    }
    if not harness_policy.current(conn, run_id):
        return identity
    declared = method["manifest"]
    domain = input_domain.declarations(declared["method_card"].get("applicable_domain"),
                                       declared.get("applicable_domain"))
    randomness = []
    for rule in domain.values():
        if random := rule.get("randomness"):
            if random["mode"] == "deterministic":
                randomness.append({"mode": "deterministic"})
            else:
                seed = random.get("fixed_seed", params.get(random.get("seed_parameter")))
                randomness.append({"mode": "seeded" if type(seed) is int else "unknown", "seed": seed})
    portable = shipped.portable_digest(method["bundle"])
    if not randomness:
        pinned = cap in shipped.MANIFESTS and portable == shipped.pin(cap)
        randomness = [{"mode": "deterministic" if pinned else "unknown"}]
    identity.pop("module_provider", None)
    identity.update(version=2, provider=execution_provider(conn, run_id, cap, module_binding), executable=portable,
                    program_sha256=program_pins(declared, runtime),
                    weights=[{k: w[k] for k in ("sha256", "bytes", "load_mode")} for w in declared["weights"]],
                    licence=declared.get("licence"), randomness=randomness)
    return identity


def _reuse_scope(conn, node, refs, identity):
    """Public Run originals can move between seats. A derived or private input stays within its branch."""
    spec = json.loads(node["spec_json"])
    if (spec.get("independent_repeat") or any(rule["mode"] == "unknown" for rule in identity["randomness"])
            or identity.get("required_data_sha256", {}) is None or identity["program_sha256"] is None):
        return {"independent_node": node["node_id"]}
    module = conn.execute("SELECT grant_json FROM v62_modules WHERE run_id=? AND branch_id=?",
                          (node["run_id"], node["branch_id"])).fetchone()
    if module and json.loads(module[0]).get("communication") == "isolated":
        return {"private_branch": node["branch_id"]}
    public = {r[0] for r in conn.execute("""SELECT json_extract(f.value,'$.artifact_ref') FROM runs r,
            json_each(r.inputs_json,'$.files') f WHERE r.run_id=?""", (node["run_id"],))}
    if set(refs) - public:
        return {"private_branch": node["branch_id"]}
    return {"public_inputs": list(refs)}


def bind_inputs(conn, run_id, cap, method, refs, parameters, *, writable, module_binding=None):
    params = manifest.effective_parameters(method["manifest"], parameters)
    provider = goal_plugins.method(conn, run_id, cap, module_binding=module_binding)
    if provider and provider.bind:
        ctx = v6_method_context.MethodContext(conn, run_id, refs, writable=writable)
        bound, params = v6_method_context.invoke(provider.bind, ctx, cap, refs, params)
    else:
        bound = dict(zip(_roles(method["manifest"], len(refs)), refs, strict=True))
    declared = method["manifest"]
    domains = input_domain.declarations(declared["method_card"].get("applicable_domain"), declared.get("applicable_domain"))
    if domains:
        input_domain.require(domains, bound, input_domain.InputFacts(storage.artifact_service(conn), run_id), resolved=True)
    return bound, params


def _compile(conn, node, refs):
    spec = json.loads(node["spec_json"])
    cap = spec["capability"]
    method = registry.candidate(conn, cap)
    local = module_methods.for_spec(conn, node["run_id"], spec)
    inputs, params = bind_inputs(conn, node["run_id"], cap, method, refs, spec["parameters"], writable=True, module_binding=local)
    artifacts = storage.artifact_service(conn)
    entries = [{"role": key, "artifact_ref": ref} for key, ref in inputs.items()]
    inputs_ref = artifacts.make_input_manifest(
        namespace=f"{node['run_id']}/v6/inputs", run_id=node["run_id"], entries=entries
    ).artifact_ref
    entries = artifacts.read_input_manifest(inputs_ref)["entries"]
    identity = method_identity(conn, node["run_id"], cap, method, entries, params, module_binding=local)
    if identity["version"] == 2:
        identity.update(_reuse_scope(conn, node, refs, identity))
    elif spec.get("independent_repeat"):
        identity["independent_node"] = node["node_id"]
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


def _branch_usage(conn, branch_id):
    rows = conn.execute(
        "SELECT r.reservation_ref,r.subject_id,r.amounts_json,r.status,e.payload_json FROM v6_nodes n "
        "JOIN v6_job_attempts a ON a.node_id=n.node_id AND a.reused_from IS NULL "
        "JOIN jobs j ON j.job_id=n.job_id JOIN budget_reservations r ON r.reservation_ref=j.reservation_ref "
        "LEFT JOIN events e ON e.kind='budget.settled' AND e.subject_kind='budget' AND e.subject_id=r.budget_ref "
        "AND json_extract(e.payload_json,'$.reservation_ref')=r.reservation_ref "
        "WHERE n.branch_id=? ORDER BY r.reservation_ref", (branch_id,))
    spent, held, reservations = Counter(), Counter(), []
    for row in rows:
        if row["status"] == "HELD":
            amounts = json.loads(row["amounts_json"])
            held.update(amounts)
            reservations.append({"reservation_ref": row["reservation_ref"], "subject_kind": "job",
                                 "subject_id": row["subject_id"], "amounts": amounts})
        elif row["status"] == "SETTLED":
            spent.update(json.loads(row["payload_json"])["actual"] if row["payload_json"]
                         else json.loads(row["amounts_json"]))
    return spent, held, reservations


def branch_exposure(conn, branch_id):
    """Booked usage and held maximum from original JobService reservations in this branch."""
    spent, held, _ = _branch_usage(conn, branch_id)
    return spent + held


def _branch_room(conn, node, amounts):
    spent, held, reservations = _branch_usage(conn, node["branch_id"])
    limits = json.loads(node["branch_spec"])["budget_limits"]
    shortfalls = []
    for key, amount in amounts.items():
        available = limits.get(key, 0) - spent[key] - held[key]
        if amount > available:
            shortfalls.append({"key": key, "requested": amount, "limit": limits.get(key, 0),
                               "available": available, "spent": spent[key], "reserved": held[key],
                               "short_by": amount - available})
    if shortfalls:
        first = shortfalls[0]
        raise BudgetExceeded("Branch allowance cannot fund this Job", details={
            "branch_id": node["branch_id"], "line": first["key"], "needed": first["requested"],
            "limit": first["limit"], "held_or_spent": first["reserved"] + first["spent"],
            "shortfalls": shortfalls, "held_reservations": reservations})


def fence_admission(conn, request, request_sha, key, job_id, amounts, runtime, method_dependencies, required_data,
                    provider=None, program_sha256=None):
    """The Job writer calls this after inserting the row, inside its own admission transaction."""
    binding = request.get("binding") or {}
    node = _node(conn, request["run_id"], binding.get("v6_node_id"))
    _live(node, "PENDING")
    from . import research_dialogue
    if research_dialogue.blocked(conn, node["run_id"], branch_id=node["branch_id"]):
        raise HoldActive("This branch is waiting for a PI answer")
    from . import capability_views
    if not module_methods.for_request(conn, request):
        capability_views.require_for_node(conn, node["run_id"], request["capability_version"])
    _prospective(conn, request)
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
    if identity["version"] == 2 and (identity["provider"] != provider or identity["program_sha256"] != program_sha256):
        raise SchemaInvalid("The executing provider changed after the node was prepared")
    if "required_data_sha256" in identity and identity["required_data_sha256"] != required_data:
        raise SchemaInvalid("Required method data changed after the graph node was prepared")
    if identity.get("method_dependencies", {}) != method_dependencies:
        raise SchemaInvalid("Method dependencies changed after the graph node was prepared")
    compiled_env = identity["runtime"]
    # GPU availability is a launch observation. Runtime environment identity is frozen separately.
    if _runtime_identity(compiled_env) != _runtime_identity(runtime):
        raise SchemaInvalid("Execution environment changed after the node was compiled")
    # In-flight equivalents still serialize. A completed node owns no future
    # measurement when the effective scientific side data could not be pinned.
    may_reuse = required_data == {} or identity.get("required_data_sha256") is not None
    equivalent = conn.execute(
        "SELECT node_id FROM v6_nodes WHERE run_id=? AND exec_key=? AND node_id<>? "
        "AND (status='RUNNING' OR (? AND status IN ('SUCCEEDED','REUSED')) "
        "OR (status='CANCEL_REQUESTED' AND job_id IS NOT NULL)) LIMIT 1",
        (node["run_id"], attempt["exec_key"], node["node_id"], may_reuse),
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
        grant = json.loads(module["grant_json"])
        event = spec.get("module_method_event")
        if event is not None:
            if node["job_id"] is None:
                module_methods.require_available(conn, node["run_id"], spec["capability"])
                if grant.get("local_bindings", {}).get(spec["capability"]) != event:
                    raise DependencyUnmet("The module changed this selection before Job admission",
                                          details={"reason": "capability_disabled", "capability": spec["capability"]})
            allowed = True
        else:
            allowed = spec["capability"] in grant["capabilities"]
        if module["branch_id"] != node["branch_id"] or not allowed:
            from ..errors import ScopeDenied
            raise ScopeDenied("This module Job lost its capability grant")


def _prospective(conn, request):
    def input_refs():
        bound = storage.artifact_service(conn).read_input_manifest(request["input_manifest_ref"])
        return {item["role"]: item["artifact_ref"] for item in bound["entries"]}
    v6_prospective.require_registration(conn, request["run_id"], request["capability_version"],
                                        request["parameters"], input_refs)


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
    from . import acquired_material
    acquisition = None
    if status in ("SUCCEEDED", "REUSED") and job_id and node["status"] != "CANCEL_REQUESTED":
        try:
            acquisition = acquired_material.prepare(conn, node, job_id)
            if acquisition:
                result = acquisition["artifact_ref"]
        except (ArtifactHashMismatch, ArtifactNotFound, SchemaInvalid, ScopeDenied, ValueError, KeyError) as exc:
            status, result, reason = "FAILED", None, f"Archive acquisition could not be verified: {exc}"
    failure = diagnostics.preserved(conn, node["run_id"], job_id) if job_status == "FAILED" and job_id else None
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
        if source and current["status"] == "CANCEL_REQUESTED" and not current["job_id"]:
            return False
        if source and current["status"] == "PENDING":
            _live(current, "PENDING")
            attempt = tx.execute("SELECT request_json FROM v6_job_attempts WHERE node_id=?", (node["node_id"],)).fetchone()
            from . import capability_views
            if not module_methods.for_request(tx, json.loads(attempt[0])):
                capability_views.require_for_node(tx, node["run_id"], json.loads(attempt[0])["capability_version"])
            _prospective(tx, json.loads(attempt[0]))
            spec = json.loads(current["spec_json"])
            _module_grant(tx, current, spec)
            if spec.get("evidence_gate"):
                v62_evidence.fence(tx, current, evidence)
            execution.require_current(tx, job_id)
        if source and current["status"] == "RUNNING":
            subscribed = tx.execute("SELECT reused_from FROM v6_job_attempts WHERE node_id=?",
                                    (current["node_id"],)).fetchone()[0]
            if subscribed != source:
                raise SchemaInvalid("The result belongs to a different physical Job subscription")
        if current["status"] == "CANCEL_REQUESTED":
            status = "CANCELLED"  # Keep late result and actual Job status on their original identity.
        tx.execute(
            "UPDATE v6_nodes SET status=?,result_ref=?,job_id=COALESCE(job_id,?) WHERE node_id=?",
            (status, result, job_id, node["node_id"]),
        )
        tx.execute("UPDATE v6_job_attempts SET reused_from=COALESCE(reused_from,?) WHERE node_id=?", (source, node["node_id"]))
        _consume(tx, node)
        if acquisition and status in ("SUCCEEDED", "REUSED"):
            acquired_material.attach(tx, current, acquisition)
        store.append_event(
            tx,
            kind="v6.node.settled",
            actor="gx",
            run_id=node["run_id"],
            subject_kind="node",
            subject_id=node["node_id"],
            prev_state=current["status"],
            new_state=status,
            reason=reason or (failure["summary"] if failure and status == "FAILED" else None),
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
                **({"failure": failure} if failure and status == "FAILED" else {}),
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
    from . import harness_policy
    funding_wait = isinstance(error, BudgetExceeded) and harness_policy.active(conn, node["run_id"])
    if funding_wait:
        # Preflight uses a provisional Job ID. Only durable reservations belong in
        # a Lead's brief and in the identity that suppresses duplicate wait events.
        wait = reason = {**reason, "details": {k: v for k, v in details.items() if k != "subject"}}
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
        if funding_wait:
            account = tx.execute(
                "SELECT scope FROM v62_modules WHERE run_id=? AND branch_id=?",
                (node["run_id"], cur["branch_id"])).fetchone()
            reason = {**reason, "funding_scope": account["scope"] if account else "run",
                      "decision_ref": cur["decision_ref"]}
        event = store.append_event(
            tx,
            kind="v6.node.waiting",
            actor="gx",
            run_id=node["run_id"],
            subject_kind="node",
            subject_id=node["node_id"],
            payload=reason,
        )
        if details.get("reason") == "prospective_opening":
            version, = tx.execute("SELECT goal_version FROM v6_goals WHERE run_id=?", (node["run_id"],)).fetchone()
            prior = tx.execute("SELECT 1 FROM events WHERE run_id=? AND kind='v6.prospective.opening_wait' "
                               "AND json_extract(payload_json,'$.goal_version')=?", (node["run_id"], version)).fetchone()
            if not prior:
                store.append_event(tx, kind="v6.prospective.opening_wait", actor="gx", run_id=node["run_id"],
                                   subject_kind="run", subject_id=node["run_id"], payload={"goal_version": version})
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


def _subscribe(conn, node, source, spec, request, evidence):
    """Adopt a launched physical Job under the receiver's still-active delegation."""
    from . import capability_views, research_dialogue
    with _tx(conn, node["run_id"], "subscribe_node_to_job") as tx:
        receiver = _node(tx, node["run_id"], node["node_id"])
        if receiver["status"] != "PENDING":
            return "waiting"
        _live(receiver, "PENDING")
        sender = _node(tx, node["run_id"], source["node_id"])
        if sender["status"] != "RUNNING" or sender["job_id"] != source["job_id"]:
            return "waiting"
        if research_dialogue.blocked(tx, node["run_id"], branch_id=receiver["branch_id"]):
            return "waiting"
        if "module_method_event" not in spec:
            capability_views.require_for_node(tx, node["run_id"], spec["capability"])
        _module_grant(tx, receiver, spec)
        _prospective(tx, request)
        if spec.get("evidence_gate"):
            v62_evidence.fence(tx, receiver, evidence)
        execution.require_current(tx, sender["job_id"])
        physical_owner = source["reused_from"] or sender["node_id"]
        tx.execute("UPDATE v6_nodes SET status='RUNNING',job_id=? WHERE node_id=?",
                   (sender["job_id"], receiver["node_id"]))
        tx.execute("UPDATE v6_job_attempts SET reused_from=? WHERE node_id=?",
                   (physical_owner, receiver["node_id"]))
        _consume(tx, receiver)
        store.append_event(tx, kind="v6.node.subscribed", actor="gx", run_id=receiver["run_id"],
            subject_kind="node", subject_id=receiver["node_id"], prev_state="PENDING", new_state="RUNNING",
            payload={"job_id": sender["job_id"], "physical_owner": physical_owner,
                     "branch_id": receiver["branch_id"], "exec_key": receiver["exec_key"],
                     "operation_key": receiver["operation_key"]})
    return "subscribed"


def _active(conn, node, service):
    """Recover by accepted execution identity. OUTCOME_UNKNOWN keeps its hold and remains active."""
    view = service.status(node["job_id"])
    if node["status"] == "CANCEL_REQUESTED" and not view["terminal"]:
        other = conn.execute("SELECT 1 FROM v6_nodes WHERE job_id=? AND node_id<>? AND status='RUNNING' LIMIT 1",
                             (node["job_id"], node["node_id"])).fetchone()
        if other:
            return "settled" if _finish(conn, node, "CANCELLED", job_id=node["job_id"],
                    job_status=view["status"], reason="Only this scientific subscription was withdrawn") else "waiting"
        cancel_key = "cancel:" + node["job_id"] if harness_policy.current(conn, node["run_id"]) else node["operation_key"]
        service.cancel(node["job_id"], reason="GX decision withdrew the last active node", idempotency_key=cancel_key)
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
        accepted = conn.execute("SELECT reused_from FROM v6_job_attempts WHERE node_id=?",
                                (node["node_id"],)).fetchone()
        subscribed = accepted["reused_from"] if accepted else None
        if subscribed and status == "SUCCEEDED" and node["status"] != "CANCEL_REQUESTED":
            proof = execution.read_manifest(conn, view["job_id"])
            output = proof["completion"]["outputs"][0]
            verified = storage.artifact_service(conn).verify(result)
            if (proof["admission"]["owner"].get("node_id") != subscribed
                    or proof["admission"]["owner"].get("exec_key") != node["exec_key"]
                    or not verified["ok"] or verified["observed_sha256"] != output["sha256"]):
                raise SchemaInvalid("Subscribed Job result differs from its frozen execution")
            execution.require_current(conn, view["job_id"])
            status = "REUSED"
        finished = _finish(
            conn,
            node,
            status,
            job_id=view["job_id"],
            result=result,
            job_status=view["status"],
            source=subscribed,
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
    from . import research_dialogue
    if research_dialogue.blocked(conn, node["run_id"], branch_id=node["branch_id"]):
        return "waiting"
    research_window.require_open(window)
    spec = json.loads(node["spec_json"])
    from . import capability_views
    if "module_method_event" not in spec:
        capability_views.require_for_node(conn, node["run_id"], spec["capability"])
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
    request = json.loads(attempt["request_json"])
    # A parked node should not restage a Job workspace while its Lead is still deciding.
    _prospective(conn, request)
    identity = json.loads(attempt["identity_json"])
    current_contract = identity["version"] == 2
    source = conn.execute(
        "SELECT n.*,a.reused_from FROM v6_nodes n JOIN v6_job_attempts a USING(node_id) "
        "WHERE n.run_id=? AND n.node_id<>? AND n.exec_key=? AND n.job_id IS NOT NULL AND "
        "(((n.status='SUCCEEDED' OR (? AND n.status='REUSED')) AND n.result_ref IS NOT NULL) OR "
        "(? AND n.status='RUNNING' AND EXISTS (SELECT 1 FROM jobs j WHERE j.job_id=n.job_id "
        "AND j.status <> 'CREATED'))) ORDER BY (n.result_ref IS NULL),n.rowid LIMIT 1",
        (node["run_id"], node["node_id"], attempt["exec_key"], current_contract, current_contract),
    ).fetchone()
    # A large mutable repository or directory has no byte pin. A successful
    # earlier result cannot establish that this node would use the same data.
    if source:
        method = registry.candidate(conn, spec["capability"])
        paths = method["manifest"].get("required_data", ())
        if paths and identity.get("required_data_sha256") is None:
            source = None
    if source:
        # A previously parked attempt may resume with different data, interpreter or SVD.
        if paths and _small_data_pins(paths) != identity["required_data_sha256"]:
            raise SchemaInvalid("Required method data changed before the previous result could be reused")
        active = environments.require_ready(conn, method["env_id"])
        local = module_methods.for_spec(conn, node["run_id"], spec)
        if current_contract:
            if (identity["provider"] != execution_provider(conn, node["run_id"], spec["capability"], local)
                    or identity["executable"] != shipped.portable_digest(method["bundle"])
                    or identity["program_sha256"] != program_pins(method["manifest"], active)):
                raise SchemaInvalid("Prior execution has a different provider or executable")
            for part in ("entry_script", "parser"):
                if script := method["bundle"].get(part):
                    manifest.script_path(script)
            if method["manifest"]["weights"]:
                env = manifest.EnvView(env_id=active["env_id"], python=active["python_executable"],
                                       binaries=dict(active["binary_paths"]), prefix=active["prefix"])
                for weight in method["manifest"]["weights"]:
                    path = manifest.resolve_path(weight["path"], env=env)
                    if not path.is_file() or ids.sha256_of_file(path) != weight["sha256"]:
                        raise SchemaInvalid("Prior execution has different or unavailable model weights")
        current = execution._runtime(active, manifest.effective_resources(method["manifest"], {}), {})
        if (
            identity["manifest_sha256"] != method["manifest_sha256"]
            or identity["runtime"] != current
            or identity.get("method_dependencies", {})
            != v6_method_context.dependencies(conn, node["run_id"], spec["capability"], active["python_executable"],
                                              module_binding=local)
        ):
            raise SchemaInvalid("Compiled attempt is incompatible with the currently installed method")
        if source["status"] == "RUNNING":
            return _subscribe(conn, node, source, spec, request, evidence)
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
                source=source["reused_from"] or source["node_id"],
                evidence=evidence,
                job_id=source["job_id"],
                job_status="SUCCEEDED",
                result=source["result_ref"],
            )
            else "waiting"
        )
    result = service.submit(**request, idempotency_key=node["operation_key"])
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
            outcome = _run_node(conn, node, service, window)
            counts[outcome] = counts.get(outcome, 0) + 1
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
            dependency = error.details if error.details.get("reason") in {"evidence_route_blocked", "capability_disabled"} else None
            if _finish(conn, node, status, reason=f"{error.code}: {error.message}", dependency=dependency):
                counts["settled"] += 1
    _finalize(conn, run_id)
    return counts
