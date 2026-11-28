"""Premises of a module amendment to the Run's single immutable graph."""
from collections import Counter
import json

from ..errors import BudgetExceeded, ScopeDenied, StaleRevision


def branch_spec(module):
    """Materialize the already funded private branch at the first accepted Job declaration."""
    return {"op": "open_branch", "branch": module["branch_id"],
            "module_instance_id": module["module_instance_id"],
            "budget_limits": json.loads(module["budget_json"])}


def require_operations(operations):
    if any(op["op"] not in ("add_node", "cancel_node") for op in operations):
        raise ScopeDenied("A module can only commission or withdraw work in its own branch")


def graph_at(conn, run_id, head):
    if head is None:
        return None
    return conn.execute("SELECT graph_json FROM v6_graph_versions WHERE run_id=? AND version=? AND sha256=?",
                        (run_id, head["version"], head["sha256"])).fetchone()[0]


def require_compatible(original, latest, branch, decision):
    """Compare only read/write graph subjects used by this draft, before taking the writer."""
    old = json.loads(original) if original else {"branches": {}, "nodes": {}, "closed": False}
    current = json.loads(latest) if latest else {"branches": {}, "nodes": {}, "closed": False}
    changed = set()
    ops = decision["ops"]
    watches = {w["subject_ref"] for w in decision["reconsider_when"]}
    branches = watches | ({branch} if branch is not None else {op["branch"] for op in ops if "branch" in op})
    nodes = watches | set(decision.get("approve_nodes", ()))
    if current["closed"]:
        changed.add("run")
    for op in ops:
        nodes.update(op.get("depends_on", ()))
        if op["op"] == "cancel_node":
            nodes.add(op["node"])
        elif op["op"] == "stop_branch":
            # Stopping a branch also withdraws work added to it since the Lead's snapshot.
            prior = {n: s for n, s in old["nodes"].items() if s["branch"] == op["branch"]}
            latest = {n: s for n, s in current["nodes"].items() if s["branch"] == op["branch"]}
            if prior != latest:
                changed.add(op["branch"])
    for kind, subjects in (("branches", branches), ("nodes", nodes)):
        changed.update(key for key in subjects if not key.startswith("local:")
                       and old[kind].get(key) != current[kind].get(key))
    if changed:
        reason = "module_graph_conflict" if branch is not None else "lead_graph_conflict"
        raise StaleRevision("A graph subject used by this decision changed after its frozen brief",
                            details={"reason": reason, "changed_subjects": sorted(changed)})


def job_demand(conn, run_id, jobs):
    """Only definite new work needs remaining child funds; JobService proves any actual reuse."""
    from . import harness_policy
    # New Goals allow a planned node to wait, reuse a result or never take its
    # branch. JobService alone acquires its actual units under the admission writer.
    if harness_policy.active(conn, run_id):
        return {}
    demand = Counter()
    # Input binding can resolve a node to its actual bytes later. Reuse here is
    # provisional. It never grants launch authority if the final fingerprint differs.
    keys = ("capability", "inputs", "input_ports", "parameters", "depends_on")
    seen = set()
    if jobs:
        for row in conn.execute("SELECT spec_json FROM v6_nodes WHERE run_id=? "
                                "AND status IN ('RUNNING','SUCCEEDED','REUSED','PENDING')", (run_id,)):
            spec = json.loads(row[0])
            seen.add(tuple(json.dumps(spec.get(k, []), sort_keys=True) for k in keys))
    for spec, amounts in jobs:
        identity = tuple(json.dumps(spec.get(k, []), sort_keys=True) for k in keys)
        if identity not in seen:
            demand.update(amounts)
            seen.add(identity)
    return dict(demand)


def require_budget(conn, module, demand):
    from . import v62_budget
    if not demand:
        return
    available = v62_budget.remaining(conn, module["budget_ref"])
    for key, amount in demand.items():
        if available.get(key, 0) < amount:
            raise BudgetExceeded("Module allowance cannot admit these new Jobs",
                                 details={"scope": module["scope"], "key": key,
                                          "requested": amount, "available": available.get(key, 0)})


def dependency_pins(conn, run_id, ops):
    new_nodes = {entry["spec"]["node"] for entry in ops if entry["spec"]["op"] == "add_node"}
    dependencies = {nid for entry in ops for nid in entry["spec"].get("depends_on", ())} - new_nodes
    result = {}
    for node_id in sorted(dependencies):
        row = conn.execute("SELECT status,result_ref,operation_key FROM v6_nodes WHERE run_id=? AND node_id=?",
                           (run_id, node_id)).fetchone()
        if not row or row["status"] in ("FAILED", "BLOCKED", "CANCEL_REQUESTED", "CANCELLED"):
            raise StaleRevision("A required module dependency was withdrawn or failed",
                                details={"reason": "module_graph_conflict", "changed_subjects": [node_id]})
        result[node_id] = (row["operation_key"], row["result_ref"])
    return result


def recheck_dependencies(conn, run_id, plan):
    """A pending input may complete. Never replace an already observed result."""
    current = dependency_pins(conn, run_id, plan.resolved_ops)
    for nid, (key, ref) in plan.module_dependencies.items():
        now_key, now_ref = current[nid]
        if key != now_key or (ref is not None and ref != now_ref):
            raise StaleRevision("The identity of a required module input changed during graph preparation",
                                details={"reason": "module_graph_conflict", "changed_subjects": [nid]})
