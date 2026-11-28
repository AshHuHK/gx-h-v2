"""Expand only the mechanical parts of a decision before canonical graph validation."""
from collections import Counter
from copy import deepcopy
import json

from .. import config
from ..capabilities import manifest, registry
from ..errors import SchemaInvalid, ScopeDenied
from ..jobs.service import JobService
from ..plugins import goal as goal_plugins
from . import harness_policy


def _alias(names, label):
    alias = "local:" + label
    index = 1
    while alias in names:
        index += 1
        alias = f"local:{label}-{index}"
    names.add(alias)
    return alias


def canonical_labels(body):
    """Accept a local label in a known alias field when its meaning is unique."""
    if not isinstance(body, dict) or not isinstance(body.get("ops"), list):
        return body
    edited = None
    for index, op in enumerate(body["ops"]):
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            continue
        key = {"open_branch": "branch", "add_node": "node"}.get(op.get("op"))
        if not key:
            continue
        spellings = [k for k in (key + "_id", "ref", "id", "name") if k in op]
        if not spellings:
            continue
        values = [op[k] for k in spellings] + ([op[key]] if key in op else [])
        if not all(isinstance(v, str) and v.startswith("local:") and v[6:].strip() for v in values):
            continue  # Let the wire report the invalid field and preserve the original request.
        if len(set(values)) > 1:
            raise SchemaInvalid(f"Use one local label in ops[{index}].{key}", details={
                "field": f"ops[{index}].{key}", "reason": "ambiguous_local_label", "options": sorted(set(values))})
        if edited is None:
            edited = deepcopy(body)
        target = edited["ops"][index]
        for alias in spellings:
            del target[alias]
        target[key] = values[0]
    return edited if edited is not None else body


def _choice(field, message, options=()):
    return SchemaInvalid(message, details={"reason": "branch_selection_required",
                                          "field": field, "options": sorted(options)})


def raise_choices(issues):
    """Keep the first owner's error and list all independently decidable selections."""
    first = issues[0]
    if len(issues) > 1:
        choices = []
        for exc in issues:
            d = exc.details
            choices.extend(d.get("choices") or [{
                "field": d.get("public_field", d["field"]), "options": d.get("allowed_units", d.get("options", [])),
                **({"unknown_units": d["unknown_units"]} if "unknown_units" in d else {})}])
        first.details["choices"] = choices
    raise first


def _job_requirements(conn, allowed, node):
    capability = node.get("capability")
    if not isinstance(capability, str):
        raise SchemaInvalid("Specify the measurement's pinned capability in ops[].capability")
    if capability not in allowed:
        raise ScopeDenied("Capability is outside the assigned method catalog")
    method = registry.candidate(conn, capability)["manifest"]
    return method, JobService._budget_amounts(manifest.effective_resources(method, {}))


def _probe_budget(conn, allowed, jobs):
    """Account for every proposed Job using the Job owner's resource calculation."""
    if not jobs:
        return None
    ceiling = config.limits()["budget_profiles"]["native_mvp"]
    totals = Counter()
    for job in jobs:
        method, demand = _job_requirements(conn, allowed, job)
        if (method["kind"] == manifest.STDIO_KIND or "gpu_jobs" in demand or
                any(quantity > ceiling.get(unit, 0) for unit, quantity in demand.items())):
            return None
        totals.update(demand)
    return dict(totals)


def _probe_fields(conn, run_id, snapshot, ops, allowed, local):
    """Resolve only references whose identity follows from this actor's visible choices."""
    choices, producers = [], {}
    wanted = {ref for op in ops if op.get("op") == "add_node" for ref in op.get("inputs", ())}
    own_branch = snapshot.get("module", {}).get("branch_id")
    for row in snapshot.get("nodes", ()):
        ref = row["node_id"]
        if ref not in wanted:
            continue
        if cap := row.get("commission", {}).get("capability"):
            producers[ref] = cap
        elif own_branch and row.get("branch_id") == own_branch:
            node = conn.execute("SELECT spec_json FROM v6_nodes WHERE run_id=? AND node_id=? AND branch_id=?",
                                (run_id, ref, own_branch)).fetchone()
            if node:
                producers[ref] = json.loads(node[0])["capability"]
    for index, op in enumerate(ops):
        if op["op"] != "add_node":
            continue
        cap = op["capability"]
        if "@" not in cap:
            options = sorted(k for k in allowed if k.rpartition("@")[0] == cap)
            if len(options) == 1:
                cap = op["capability"] = options[0]
            elif options:
                choices.append({"field": f"ops[{index}].capability", "options": options})
                continue
        if cap not in allowed:
            raise ScopeDenied("Capability is outside the assigned method catalog")
        producers[op["node"]] = cap

    for index, op in enumerate(ops):
        if op["op"] != "add_node" or op["capability"] not in allowed:
            continue
        prefix = f"ops[{index}]"
        refs = op.setdefault("inputs", [])
        if not refs:
            method = registry.candidate(conn, op["capability"])["manifest"]
            roles = manifest.input_roles(method)
            if roles or "{inputs.json}" in method["entry"]["argv_template"]:
                visible = snapshot.get("materials", [])
                binding = local.get(op["capability"])
                provider = goal_plugins.method(conn, run_id, op["capability"],
                                               module_binding={"descriptor": binding} if binding else None)
                simple = not (provider and provider.bind) and "{inputs.json}" not in method["entry"]["argv_template"]
                automatic = []
                for role in roles:
                    matches = [item["artifact_ref"] for item in visible if item.get("role") == role]
                    if len(matches) == 1:
                        automatic.extend(matches)
                if simple and roles and len(automatic) == len(roles) and len(set(automatic)) == len(roles):
                    op["inputs"] = refs = automatic
                else:
                    choices.append({"field": prefix + ".inputs", "roles": roles,
                                    "options": [item["artifact_ref"] for item in visible]})
        selected = {p["index"] for p in op.get("input_ports", [])}
        for i, ref in enumerate(refs):
            if i in selected or ref not in producers:
                continue
            parent = registry.candidate(conn, producers[ref])["manifest"]
            names = sorted(out["name"] for out in parent["outputs"] if not manifest.is_dir_output(out))
            if len(names) == 1:
                op.setdefault("input_ports", []).append({"index": i, "output": names[0]})
            elif names:
                choices.append({"field": f"{prefix}.input_ports[{i}]", "producer": ref,
                                "options": names, "input_index": i})
    return choices


def normalize(conn, task, goal, body, *, module=None):
    decision = deepcopy(body)
    if not isinstance(decision, dict) or not isinstance(decision.get("ops"), list):
        raise SchemaInvalid("Supply the decision's operations in ops")
    ops = decision["ops"]
    if any(not isinstance(op, dict) for op in ops):
        raise SchemaInvalid("Each graph operation must be an object")
    decision.setdefault("mode", "amend" if ops else "resolve")
    decision.setdefault("evidence_refs", [])
    opens = [op for op in ops if op.get("op") == "open_branch"]
    jobs = [op for op in ops if op.get("op") == "add_node"]
    names = {op[k] for op in ops for k in ("branch", "node") if isinstance(op.get(k), str)}
    modern = bool(jobs) and harness_policy.active(conn, goal["run_id"])
    referenced = {j["branch"] for j in jobs if isinstance(j.get("branch"), str) and j["branch"].startswith("local:")}
    referenced -= {op.get("branch") for op in opens}
    for op in opens:
        if "branch" not in op:
            if len(opens) != 1 and not (modern and sum("branch" not in b for b in opens) == len(referenced) == 1):
                raise _choice("ops[].branch", "Assign a distinct local: branch name to each new branch")
            op["branch"] = next(iter(referenced)) if modern and len(referenced) == 1 else _alias(names, "branch")
    for job in jobs:
        if "node" not in job:
            job["node"] = _alias(names, "measurement")
        job.setdefault("parameters", {})
    snapshot = json.loads(task["snapshot_json"]) if jobs else {}
    allowed = set(json.loads(goal["capabilities_json"]))
    local = {}
    if modern and module is not None:
        from . import module_methods, v62_catalog
        local = {r["capability_version"]: r for r in module_methods.catalog(conn, goal["run_id"], task["scope"])}
        allowed.intersection_update(v62_catalog.effective_methods(conn, goal["run_id"], json.loads(module["grant_json"])))
        allowed.update(local)
    choices = _probe_fields(conn, goal["run_id"], snapshot, ops, allowed, local) if modern else []

    nodes = {row["node_id"] for row in snapshot.get("nodes", ())}
    nodes.update(job["node"] for job in jobs if isinstance(job["node"], str))
    for job in jobs:
        if isinstance(job.get("inputs"), list):
            inferred = list(dict.fromkeys(ref for ref in job["inputs"] if isinstance(ref, str) and ref in nodes))
            declared = job.setdefault("depends_on", inferred)
            if modern and isinstance(declared, list):
                declared.extend(ref for ref in inferred if ref not in declared)

    implicit = None
    if jobs:
        if module is not None:
            candidates = [module["branch_id"]]
        elif task["scope"] != "run":
            candidates = [task["scope"].removeprefix("branch:")]
        else:
            candidates = [row["branch_id"] for row in snapshot.get("branches", ())
                          if row["status"] == "OPEN" and not row.get("spec", {}).get("module_instance_id")]
            candidates.extend(op["branch"] for op in opens if isinstance(op["branch"], str))
            if (not candidates and not choices and all("branch" not in j for j in jobs) and
                    ((modern and all(op["op"] in ("add_node", "open_intent", "bind_intent") for op in ops))
                     or (len(ops) == 1 and json.loads(task["base_json"] or "null") is None))):
                minimum = _probe_budget(conn, allowed, jobs)
                if minimum is not None:
                    implicit = _alias(names, "measurement-branch")
                    ops.insert(0, {"op": "open_branch", "branch": implicit, "budget_limits": minimum})
                    candidates.append(implicit)
        if choices:
            if len(candidates) > 1:
                choices.extend({"field": f"ops[{i}].branch", "options": candidates} for i, op in enumerate(ops)
                               if op["op"] == "add_node" and "branch" not in op)
            first = choices[0]
            raise SchemaInvalid("Choose the scientific inputs or outputs listed in choices", details={
                "reason": "selection_required", "field": first["field"], "options": first["options"], "choices": choices})
        for job in jobs:
            if "branch" not in job:
                if len(candidates) != 1:
                    if candidates:
                        msg = "Choose the measurement branch from options"
                    else:
                        _, needed = _job_requirements(conn, allowed, job)
                        msg = ("Open an explicit funded branch for this measurement. Include open_branch "
                               "and add_node in the same ops list. Supply your hypothesis, method_family, "
                               "alternative, discriminating_observation and stop_condition as text. Give the "
                               "branch and node distinct local: names. Set open_branch.branch=local:study, "
                               "add_node.branch=local:study and add_node.node=local:measurement, or omit all three. "
                               "This Job requires budget_limits of at least " + json.dumps(needed, sort_keys=True))
                    raise _choice("ops[].branch", msg, candidates)
                job["branch"] = candidates[0]
        for op in opens:
            if "budget_limits" not in op:
                same_branch = [job for job in jobs if job["branch"] == op["branch"]]
                minimum = _probe_budget(conn, allowed, same_branch) if modern or len(jobs) == len(same_branch) == 1 else None
                if minimum is None:
                    msg = "Set budget_limits for this branch's work"
                    if len(same_branch) == 1:
                        _, needed = _job_requirements(conn, allowed, same_branch[0])
                        msg += ". The attached Job requires at least " + json.dumps(needed, sort_keys=True)
                    raise _choice("ops[].budget_limits", msg)
                op["budget_limits"] = minimum
    if "reconsider_when" not in decision:
        watches = [{"event": "job_settled", "subject_ref": job["node"]} for job in jobs]
        # Existing owner coalesces these events and filters private module observations.
        watches += [{"event": "evidence_added", "subject_ref": "run"},
                    {"event": "budget_changed", "subject_ref": "run"}]
        decision["reconsider_when"] = watches
    return decision, implicit
