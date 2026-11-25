"""Expand only the mechanical parts of a decision before canonical graph validation."""
from copy import deepcopy
import json

from .. import config
from ..capabilities import manifest, registry
from ..errors import SchemaInvalid, ScopeDenied
from ..jobs.service import JobService


def _alias(names, label):
    alias = "local:" + label
    index = 1
    while alias in names:
        index += 1
        alias = f"local:{label}-{index}"
    names.add(alias)
    return alias


def _choice(field, message, options=()):
    return SchemaInvalid(message, details={"reason": "branch_selection_required",
                                          "field": field, "options": sorted(options)})


def _single_job_budget(conn, goal, node):
    """Use the Job owner's reservation calculation and the shipped single-CPU-Job profile."""
    capability = node.get("capability")
    if not isinstance(capability, str):
        raise SchemaInvalid("Specify the measurement's pinned capability in ops[].capability")
    if capability not in json.loads(goal["capabilities_json"]):
        raise ScopeDenied("Capability is outside the Run's pinned catalog")
    method = registry.candidate(conn, capability)["manifest"]
    minimum = JobService._budget_amounts(manifest.effective_resources(method, {}))
    single = config.limits()["budget_profiles"]["native_mvp"]
    if (method["kind"] == manifest.STDIO_KIND or "gpu_jobs" in minimum
            or any(required > single.get(unit, 0) for unit, required in minimum.items())):
        return None
    return minimum


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
    for op in opens:
        if "branch" not in op:
            if len(opens) != 1:
                raise _choice("ops[].branch", "Assign a distinct local: branch name to each new branch")
            op["branch"] = _alias(names, "branch")
    for job in jobs:
        if "node" not in job:
            job["node"] = _alias(names, "measurement")
        job.setdefault("parameters", {})
    snapshot = json.loads(task["snapshot_json"]) if jobs else {}
    nodes = {row["node_id"] for row in snapshot.get("nodes", ())}
    nodes.update(job["node"] for job in jobs if isinstance(job["node"], str))
    for job in jobs:
        if isinstance(job.get("inputs"), list):
            job.setdefault("depends_on", list(dict.fromkeys(
                ref for ref in job["inputs"] if isinstance(ref, str) and ref in nodes)))

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
            if (not candidates and len(ops) == 1 and "branch" not in jobs[0]
                    and json.loads(task["base_json"] or "null") is None):
                minimum = _single_job_budget(conn, goal, jobs[0])
                if minimum is not None:
                    implicit = _alias(names, "measurement-branch")
                    ops.insert(0, {"op": "open_branch", "branch": implicit, "budget_limits": minimum})
                    candidates.append(implicit)
        for job in jobs:
            if "branch" not in job:
                if len(candidates) != 1:
                    msg = ("Choose the measurement branch from options" if candidates else
                           "Open an explicit funded branch for this measurement")
                    raise _choice("ops[].branch", msg, candidates)
                job["branch"] = candidates[0]
        for op in opens:
            if "budget_limits" not in op:
                same_branch = [job for job in jobs if job["branch"] == op["branch"]]
                minimum = _single_job_budget(conn, goal, same_branch[0]) if len(jobs) == len(same_branch) == 1 else None
                if minimum is None:
                    raise _choice("ops[].budget_limits", "Set budget_limits for this branch's work")
                op["budget_limits"] = minimum
    if "reconsider_when" not in decision:
        watches = [{"event": "job_settled", "subject_ref": job["node"]} for job in jobs]
        # Existing owner coalesces these events and filters private module observations.
        watches += [{"event": "evidence_added", "subject_ref": "run"},
                    {"event": "budget_changed", "subject_ref": "run"}]
        decision["reconsider_when"] = watches
    return decision, implicit
