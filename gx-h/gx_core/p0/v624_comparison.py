"""Project enacted, scope-bound operations onto the registered scientific comparison."""
import json

from ..capabilities import manifest, registry
from ..domain import ids
from ..errors import SchemaInvalid
from . import v623_comparison, v623_endpoints


ADMINISTRATIVE = {"BUDGET", "TIME_LIMIT", "OPERATOR"}
SCIENTIFIC = ("SCIENTIFIC_COMPLETION", "INSUFFICIENT_DATA", "UNREAD_EVIDENCE")


def problem(reason, field, message, options=()):
    raise SchemaInvalid(message, details={"reason": reason, "field": field, "options": list(options)})


def _node_effect(conn, binding, node):
    method = binding["method"]
    if (node.get("branch") != binding["branch"] or not binding.get("reference_refs")
            or node.get("inputs") != binding["reference_refs"]):
        return None
    cap, ver = method["capability"].rsplit("@", 1)
    capability = node.get("capability", "")
    if capability.rpartition("@")[0] != cap:
        return None
    bundle = registry.load_bundle(conn, cap, ver, expected_sha256=binding["method_manifest_sha256"])
    if capability != method["capability"]:
        # A new version of this method can replace the original measurement only when
        # its pinned executable still publishes the same declared readout on those inputs.
        candidate = registry.candidate(conn, capability)["manifest"]
        def endpoint(card):
            output = next((o for o in card["outputs"] if o["name"] == method["output_name"]), {})
            spec = output.get("metric_specs", {}).get(method["metric_id"])
            roles = output.get("source_roles", manifest.input_roles(card))
            return (spec, roles) if spec and spec.get("unit") == method["unit"] else None

        original = endpoint(bundle["manifest"])
        return "CHANGE" if original and original == endpoint(candidate) else None
    effective = manifest.effective_parameters(bundle["manifest"], node.get("parameters", {}))
    expected = manifest.effective_parameters(bundle["manifest"], method["parameters"])
    return "MAINTAIN" if ids.canonical_json(effective) == ids.canonical_json(expected) else "CHANGE"


def _effects(conn, binding, ops):
    effects = set()
    for op in ops:
        kind = op.get("op")
        if kind == "stop_branch" and op.get("branch") == binding["branch"]:
            effects.add("CHANGE")
        elif kind == "add_node":
            if effect := _node_effect(conn, binding, op):
                effects.add(effect)
        elif kind == "cancel_node":
            existing = conn.execute("SELECT spec_json FROM v6_nodes WHERE run_id=? AND node_id=?",
                                    (binding["run_id"], op.get("node"))).fetchone()
            if existing and _node_effect(conn, binding, json.loads(existing[0])) == "MAINTAIN":
                effects.add("CHANGE")
    return effects


def _applicable_metrics(conn, task, binding, inventory):
    """The comparison's reference and later specimens assigned to its own opening decision."""
    targets = binding.get("reference_refs", [])
    assigned = conn.execute("""SELECT subject_id FROM events
        WHERE run_id=? AND kind='v621.material.assigned' AND event_id<=?
          AND json_extract(payload_json,'$.goal_version')=? AND json_extract(payload_json,'$.decision_ref')=?""",
        (task["run_id"], task["event_cursor"], task["goal_version"], binding["decision_ref"]))
    assigned_refs = {row[0] for row in assigned}
    jobs = {}
    eligible = []
    for m in inventory.values():
        if not any(p["role"] == binding["material_role"] for p in m.get("inputs", ())):
            continue
        job = (m["job_id"], m["admission_sha256"])
        if job not in jobs:
            # The named output can project only the specimen role. Other admitted inputs
            # such as a coordinate map are still part of this particular comparison.
            saved = conn.execute("""SELECT json_extract(admission_json,'$.inputs') FROM p0_execution_manifests
                WHERE job_id=? AND admission_sha256=?""", job).fetchone()
            admitted = json.loads(saved[0]) if saved and saved[0] else []
            if targets:
                jobs[job] = len(admitted) == len(targets) and all(
                    p["artifact_ref"] == ref or p["role"] == binding["material_role"] and p["artifact_ref"] in assigned_refs
                    for p, ref in zip(admitted, targets))
            else:
                jobs[job] = bool(admitted) and all(p["artifact_ref"] in assigned_refs for p in admitted)
        if jobs[job]:
            eligible.append(m)
    keys = v623_endpoints.comparison_keys(conn, eligible, [binding])
    return {m["metric_ref"] for m, row in zip(eligible, v623_endpoints.compact_rows(eligible), strict=True)
            if row["key"] in keys}


def independent_choice(conn, task, body, packet):
    """Check independent, delivered selections while another part of the draft awaits clarification."""
    if (len(packet.get("comparisons", ())) < 2 or body.get("metric_refs") or
            body.get("comparison_plan") or body.get("comparison_id")):
        return None
    frozen = json.loads(task["snapshot_json"])
    branches = {b["branch_id"] for b in frozen["branches"] if b["status"] == "OPEN"}
    visible = set(json.loads(task["visible_refs_json"]))
    pins = {c["capability_version"] for c in frozen["capabilities"]}
    commissions = {n["node_id"] for n in frozen["nodes"] if n.get("commission")}
    ops = []
    for op in body["ops"]:
        if op["op"] == "cancel_node" and op["node"] in commissions:
            ops.append(op)
        if op.get("branch") not in branches:
            continue
        if op["op"] == "stop_branch" or (op["op"] == "add_node" and op["capability"] in pins and
                op.get("inputs") and set(op["inputs"]) <= visible):
            ops.append(op)
    if not ops:
        return None
    try:
        normalize(conn, task, dict(body, ops=ops), {}, {}, packet=packet)
    except SchemaInvalid as exc:
        if exc.details.get("reason") == "comparison_choice_required":
            return exc
    return None


def normalize(conn, task, body, inventory, received, *, packet):
    """Return an explicit action only when grounded by this actor's operation and received evidence."""
    chosen = body.get("comparison_id")
    stated = body.get("disposition")
    if task["scope"] != "run" or body.get("comparison_plan") and not (chosen or stated):
        return body
    registered = v623_comparison.bindings(conn, task["run_id"], task["goal_version"], task["event_cursor"])
    shown = {c["comparison_id"] for c in packet.get("comparisons", ())}
    active = {row[0] for row in conn.execute("SELECT branch_id FROM v6_branches WHERE run_id=? AND status='OPEN'",
                                             (task["run_id"],))}
    choices = {c["comparison_id"]: c for c in registered if c["comparison_id"] in shown and c.get("branch") in active}
    if chosen and chosen not in choices:
        problem("comparison_not_found", "comparison_id", "Choose a comparison from this Goal's delivered brief.", choices)
    if body.get("comparison_plan") and (chosen or stated):
        problem("comparison_action_conflict", "comparison_plan", "Submit an opening or an action on an existing comparison.")
    ops = body["ops"]
    closing = any(op.get("op") == "close_run" for op in ops)
    cause = body.get("stop_reason")
    if stated == "STOP" and not cause:
        problem("stop_reason_required", "stop_reason", "Choose the scientific reason for stopping this comparison.", SCIENTIFIC)
    if cause and not closing:
        problem("comparison_action_conflict", "stop_reason", "A Run stop reason requires an enacted close_run operation.")
    if cause in ADMINISTRATIVE:
        if chosen or stated:
            problem("comparison_action_conflict", "disposition", "Record this administrative close with its stop_reason alone.")
        return body
    # A normal close, including a close after maintaining or changing the comparison, is unscored.
    if closing and not (stated or chosen):
        return body
    explicit_stop = closing and cause in SCIENTIFIC and (stated == "STOP" or chosen and not stated)
    candidates = {}
    for cid, binding in choices.items():
        if chosen and cid != chosen:
            continue
        effects = {"STOP"} if explicit_stop else _effects(conn, binding, ops)
        if effects:
            candidates[cid] = effects
    if not candidates:
        if chosen or stated:
            problem("comparison_action_ambiguous", "ops", "Choose an operation on the comparison, or omit its action fields.")
        return body
    used = set(body.get("metric_refs", ()))
    if used:
        available = inventory.keys() & received.keys()
        relevant = {ref: inventory[ref] for ref in available}
        by_choice = {cid: _applicable_metrics(conn, task, choices[cid], relevant) for cid in candidates}
        candidates = {cid: effects for cid, effects in candidates.items() if used <= by_choice[cid]}
        if not candidates:
            options = sorted(set().union(*by_choice.values()))
            problem("comparison_metric_mismatch", "metric_refs",
                    "Choose delivered rows for the registered material, method, settings and endpoint.", options)
    if len(candidates) != 1:
        problem("comparison_choice_required", "comparison_id",
                "These comparisons match the proposed operation. Choose one.", candidates)
    cid, effects = next(iter(candidates.items()))
    if len(effects) != 1:
        problem("comparison_action_ambiguous", "ops",
                "These operations both continue and withdraw the selected comparison. Choose one course.", sorted(effects))
    effect = next(iter(effects))
    if stated and effect != stated:
        problem("comparison_action_conflict", "disposition",
                "The submitted disposition conflicts with the actual graph operation.", [effect])
    return {**body, "comparison_id": cid, "disposition": effect,
            "affected_scope": body.get("affected_scope", choices[cid]["branch"])}
