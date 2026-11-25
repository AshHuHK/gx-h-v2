"""Compile a Lead's operation list against one immutable instance graph, outside the writer."""
from collections import Counter
from graphlib import CycleError, TopologicalSorter
import json

from .. import budgets, contracts, schema
from ..domain import ids
from ..errors import BudgetExceeded, SchemaInvalid, ScopeDenied


def decision_schema():
    return contracts.native_goal_tool_spec("gx_decide_revision")["inputSchema"]["properties"]["decision"]


def _shape(value, spec):
    errors = schema.validate(value, spec)
    if errors:
        raise SchemaInvalid("; ".join(errors[:4]))
    # GX's general schema validator handles types, enum, required and extra properties.
    # These remaining constraints are the ones used by the V6 decision contract.
    if isinstance(value, str) and spec.get("minLength") and not value.strip():
        raise SchemaInvalid("Decision text and references must be nonempty")
    if isinstance(value, list) and "items" in spec:
        if len(value) < spec.get("minItems", 0):
            raise SchemaInvalid("A reconsideration event is required")
        if spec.get("uniqueItems") and len(set(map(ids.canonical_json, value))) != len(value):
            raise SchemaInvalid("Repeated decision reference or watch")
        for item in value:
            _shape(item, spec["items"])
    if isinstance(value, dict):
        if "oneOf" in spec:
            variant = next((candidate for candidate in spec["oneOf"]
                            if candidate["properties"]["op"]["const"] == value.get("op")), None)
            if not variant:
                raise SchemaInvalid("Unknown graph operation")
            _shape(value, variant)
        for name, child in spec.get("properties", {}).items():
            if name in value:
                _shape(value[name], child)
        if "propertyNames" in spec:
            if value.keys() - set(spec["propertyNames"]["enum"]):
                raise SchemaInvalid("Unknown branch budget unit")
            try:
                budgets.check_amounts(value, what="branch budget")
            except OverflowError as exc:
                raise SchemaInvalid("Branch budget exceeds the supported numeric range") from exc


def validate_body(decision):
    try:
        json.dumps(decision, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SchemaInvalid("Expected finite JSON decision values") from exc
    _shape(decision, decision_schema())
    ops = decision["ops"]
    if any(not isinstance(op, dict) for op in ops):
        raise SchemaInvalid("Expected a graph operation object")
    if (decision["mode"] == "amend") != bool(ops):
        raise SchemaInvalid("An amendment requires operations; a resolve carries an empty list")
    if any(op["op"] == "close_run" for op in ops) and len(ops) != 1:
        raise SchemaInvalid("close_run must be the sole operation")


def _completed_dependency(conn, node):
    row = conn.execute("SELECT status,result_ref FROM v6_nodes WHERE node_id=?", (node,)).fetchone()
    return bool(row and row["status"] in ("SUCCEEDED", "REUSED") and row["result_ref"])


def require_cancellable(conn, run_id, node_id):
    from ..jobs.service import TERMINAL
    row = conn.execute("""SELECT n.status,j.status AS job_status FROM v6_nodes n
        LEFT JOIN jobs j ON j.job_id=n.job_id WHERE n.run_id=? AND n.node_id=?""",
        (run_id, node_id)).fetchone()
    if not row or row["status"] not in ("PENDING", "RUNNING") or row["job_status"] in TERMINAL:
        raise SchemaInvalid("Only pending or in-flight work can be cancelled")


def _aliases(operations, request_key):
    aliases = {}
    for op in operations:
        target = {"open_branch": "branch", "add_node": "node"}.get(op["op"])
        if not target:
            continue
        local = op[target]
        if not local.startswith("local:") or not local[6:].strip() or local in aliases:
            raise SchemaInvalid("New nodes and branches require distinct local: names")
        aliases[local] = f"{target}_" + ids.sha256_of_obj([request_key, local])[:32]
    return aliases


def compile_graph(conn, request, previous, visible_refs, capabilities, *, module=None, module_jobs=None):
    from . import v6_jobs

    scope, run_id = request["scope"], request["run_id"]
    decision = request["decision"]
    aliases = _aliases(decision["ops"], [run_id, request["context_ref"], request["idempotency_key"]])
    graph = json.loads(previous) if previous else {"branches": {}, "nodes": {}, "closed": False}
    branches, nodes = graph["branches"], graph["nodes"]
    own_branch = module["branch_id"] if module is not None else scope.removeprefix("branch:")
    if module is not None:
        from . import v62_graph
        v62_graph.require_operations(decision["ops"])
        if own_branch in branches and branches[own_branch].get("module_instance_id") != module["module_instance_id"]:
            raise ScopeDenied("The allocated branch is bound to a different owner")
        if own_branch not in branches and any(op["op"] == "add_node" for op in decision["ops"]):
            branches[own_branch] = dict(v62_graph.branch_spec(module), state="OPEN")
    resolved_ops = []
    changed = set()
    for index, source in enumerate(decision["ops"]):
        spec = dict(source)
        op = spec["op"]
        for key in ("branch", "node"):
            if key in spec:
                spec[key] = aliases.get(spec[key], spec[key])
        for key in ("inputs", "depends_on"):
            if key in spec:
                spec[key] = [aliases.get(ref, ref) for ref in spec[key]]
        if op == "add_node" and "evidence_gate" in spec:
            gate = dict(spec["evidence_gate"])
            gate["producer_node"] = aliases.get(gate["producer_node"], gate["producer_node"])
            if gate["source_ref"] not in visible_refs or gate["producer_node"] not in spec["depends_on"]:
                raise ScopeDenied("A conditional Job needs a visible source and a declared producing dependency")
            spec["evidence_gate"] = gate
        node = spec.get("node")
        branch = spec.get("branch") or nodes.get(node, {}).get("branch")
        if op == "add_node" and (owner := branches.get(branch, {}).get("module_instance_id")):
            if module is None or module["module_instance_id"] != owner:
                raise ScopeDenied("Only the commissioned module may spend its branch allocation")
        if scope != "run" and (op in ("open_branch", "close_run") or own_branch != branch):
            raise ScopeDenied("Operation exceeds the delegated branch")
        target = node or branch or run_id
        if target in changed:
            raise SchemaInvalid("Conflicting operations target the same subject")
        changed.add(target)
        if op == "open_branch":
            branches[branch] = dict(spec, state="OPEN")
        elif op == "add_node":
            if spec["capability"] not in capabilities:
                raise ScopeDenied("Capability is outside the Run's pinned catalog")
            nodes[node] = dict(spec, state="ACTIVE")
        elif op == "stop_branch":
            if branch not in branches or branches[branch]["state"] != "OPEN":
                raise SchemaInvalid("Only an open branch can be stopped")
            branches[branch] = dict(branches[branch], state="STOPPED")
        elif op == "cancel_node":
            if node not in nodes or nodes[node]["state"] != "ACTIVE":
                raise SchemaInvalid("Only an active graph node can be cancelled")
            require_cancellable(conn, run_id, node)
            nodes[node] = dict(nodes[node], state="CANCELLED")
        elif op == "close_run":
            graph["closed"] = True
        resolved_ops.append(dict(source_index=index, op_digest=ids.sha256_of_obj(source), spec=spec))

    producers = {nid: entry["capability"] for nid, entry in nodes.items()}
    for item in resolved_ops:
        spec = item["spec"]
        if spec["op"] != "add_node":
            continue
        if branches.get(spec["branch"], {}).get("state") != "OPEN":
            raise SchemaInvalid("A new node requires an open branch")
        for ref in spec["depends_on"]:
            if ref not in nodes:
                raise SchemaInvalid("New dependency references an absent node")
            if scope != "run" and nodes[ref]["branch"] != own_branch:
                raise ScopeDenied("A branch may reuse granted artifacts; a cross-branch dependency requires the Lead")
        for ref in spec["inputs"]:
            if ref in nodes:
                if ref not in spec["depends_on"]:
                    raise SchemaInvalid("An input supplied by another node needs a dependency edge")
            elif ref not in visible_refs:
                raise ScopeDenied("Node input was not granted in this decision context")
        minimum = v6_jobs.validate_node(conn, run_id, spec, node_ids=producers,
                                        limits=branches[spec["branch"]]["budget_limits"])
        if module_jobs is not None:
            module_jobs.append((spec, minimum))
    for node, spec in nodes.items():
        if spec["state"] == "ACTIVE" and branches[spec["branch"]]["state"] == "OPEN":
            for dep in spec["depends_on"]:
                if nodes[dep]["state"] != "ACTIVE" or branches[nodes[dep]["branch"]]["state"] != "OPEN":
                    # Completed evidence is still available even after a branch is stopped.
                    if not _completed_dependency(conn, dep):
                        current = conn.execute("SELECT status FROM v6_nodes WHERE node_id=?", (node,)).fetchone()
                        if current is None or current["status"] not in v6_jobs.NODE_TERMINAL:
                            raise SchemaInvalid("The amendment leaves a live consumer of withdrawn work")
    try:
        list(TopologicalSorter({node: spec["depends_on"] for node, spec in nodes.items()}).static_order())
    except CycleError as exc:
        raise SchemaInvalid("Execution dependencies contain a cycle") from exc
    watches = [dict(watch, subject_ref=aliases.get(watch["subject_ref"], watch["subject_ref"]))
               for watch in decision["reconsider_when"]]
    subjects = {*branches, *nodes, *visible_refs, run_id, "run", "objective"}
    if scope != "run":
        subjects = {*visible_refs, own_branch, "run", run_id, "objective",
                    *(nid for nid, spec in nodes.items() if spec["branch"] == own_branch)}
    for watch in watches:
        subject = watch["subject_ref"]
        if watch["event"] == "module_report":
            from . import v62_maturity
            if scope != "run" or not v62_maturity.enrolled(conn, run_id):
                raise ScopeDenied("Only the Run Lead can request an early module report")
            if subject not in ("run", run_id) and not conn.execute(
                "SELECT 1 FROM v62_modules WHERE run_id=? AND ? IN (module_instance_id,scope)",
                (run_id, subject),
            ).fetchone():
                raise SchemaInvalid("The report watch must identify this Run or one of its assigned modules")
        elif subject not in subjects:
            raise SchemaInvalid(f"Watch subject is outside the current graph and visible evidence: {subject}")
    return graph, resolved_ops, aliases, watches


def branch_demand(graph, resolved):
    active, additional = {}, Counter()
    for branch in graph["branches"].values():
        if branch["state"] == "OPEN" and not branch.get("module_instance_id"):
            active[branch["branch"]] = branch["budget_limits"]
    for item in resolved:
        if item["spec"]["op"] == "open_branch":
            additional.update(item["spec"]["budget_limits"])
    return active, additional


def funding_pin(conn, run_id):
    # Only parent funding events can change original Job exposure in this Run.
    # The indexed latest event also fences an ABA transfer between branches.
    row = conn.execute("""SELECT b.limits_json,b.spent_json,b.reserved_json,
        (SELECT event_id FROM events WHERE subject_kind IN ('budget','project_budget','change')
         ORDER BY event_id DESC LIMIT 1)
        FROM budgets b JOIN runs r USING(budget_ref) WHERE r.run_id=?""",
        (run_id,)).fetchone()
    return tuple(row) if row else None


def check_funding(conn, run_id, demand):
    active, additional = demand
    if not additional:
        return
    row = conn.execute("SELECT b.limits_json,b.spent_json,b.reserved_json,b.budget_ref FROM budgets b "
                       "JOIN runs r USING(budget_ref) WHERE r.run_id=?", (run_id,)).fetchone()
    if not row:
        raise BudgetExceeded("A Run budget is required for a funded branch")
    limits, spent, reserved = map(json.loads, tuple(row)[:3])
    available = budgets._available(limits, spent, reserved)
    # A branch ceiling promises its still-unused portion. Closed-branch and other Run
    # charges already reduced the parent room. Count each original Job once.
    from .v6_branch_budget import exposure
    promised = Counter()
    for branch_id, ceilings in active.items():
        used = exposure(conn, branch_id)
        promised.update({key: max(0, limit - used.get(key, 0)) for key, limit in ceilings.items()})
    # Formal project caps and workspace limits span Run boundaries; use the admission
    # owner's calculation, including other Runs' cumulative spending and held exposure.
    requested = {key: promised[key] for key in additional}
    for refusal in budgets._project_reservation_errors(conn, row["budget_ref"], requested):
        for line in refusal.details.get("shortfalls", [refusal.details]):
            key = line["key"]
            available[key] = min(available.get(key, 0), line["available"])
    for resource in additional:
        room = available.get(resource, 0)
        if promised[resource] > room:
            current = max(0, promised[resource] - additional[resource])
            remaining = max(0, room - current)
            raise BudgetExceeded(
                f"New branches requested {additional[resource]} {resource}; at most {remaining} "
                f"{resource} can be newly committed. Revise the branch ceilings and resubmit.",
                details={"resource": resource, "requested": additional[resource],
                         "available_for_new_branches": remaining, "prior_open_obligations": current,
                         "parent_remaining": room})
