"""Lead-owned logical research intents in the immutable execution graph.

An intent revision records a scientific choice. A node's original binding stays
on that node across subsequent intent revisions and external Job reconciliation.
"""
from copy import deepcopy
import json

from ..errors import SchemaInvalid, ScopeDenied, StaleRevision
from ..persistence import store

OPERATIONS = {"open_intent", "bind_intent", "resolve_intent", "converge_intent", "reopen_intent"}
TEXT = {"type": "string", "minLength": 1}
LIST = {"type": "array", "items": TEXT, "uniqueItems": True, "minItems": 1}
REVISION = {"type": "integer", "minimum": 1}


def with_operations(body, *, reinterpretation=False):
    """Extend only a newly pinned Run Lead's private copy of the decision schema."""
    body = deepcopy(body)
    props = body["properties"]
    variants = props["ops"]["items"]["oneOf"]
    specifications = [
        ("open_intent", "Declare a scientific question without choosing a method. Use a new local: intent alias.",
         ["question"], {"question": TEXT, "parent_intent": TEXT, "owner": TEXT, "expected_materials": LIST,
                        "wake_condition": props["reconsider_when"]["items"]}),
        ("bind_intent", "Bind the current intent revision to newly declared Job nodes or one commissioned module.",
         ["revision"], {"revision": REVISION, "nodes": LIST, "module_instance_id": TEXT}),
        ("resolve_intent", "Resolve from cited visible evidence; no new Job is required.",
         ["revision", "evidence_refs"], {"revision": REVISION, "evidence_refs": LIST}),
        ("converge_intent", "Provisionally settle only this route. Preserve its evidence and other open routes.",
         ["revision", "evidence_refs"], {"revision": REVISION, "evidence_refs": LIST}),
        ("reopen_intent", "Reopen this route on new cited evidence or a delivered PI feedback_id.",
         ["revision"], {"revision": REVISION, "evidence_refs": LIST, "feedback_id": TEXT}),
    ]
    for op, description, required, fields in specifications:
        if op == "reopen_intent" and reinterpretation:
            description += (" Or cite an already visible original that this route did not use and explain in "
                            "the decision rationale which previous conclusion changes and why.")
        variants.append({"type": "object", "description": description, "additionalProperties": False,
                         "required": ["op", "intent", *required],
                         "properties": {"op": {"const": op}, "intent": TEXT, **fields}})
    from . import research_closeout
    research_closeout.extend_decision(body)
    return body


def _module(conn, rid, goal_version, module, route_id):
    row = conn.execute("SELECT goal_version,status,grant_json FROM v62_modules WHERE run_id=? AND module_instance_id=?",
                       (rid, module)).fetchone()
    if not row or row["goal_version"] != goal_version:
        raise ScopeDenied("Bind a commissioned module of this Goal version")
    target = json.loads(row["grant_json"]).get("route_intent")
    if target and target != route_id:
        raise ScopeDenied("This module belongs to another route intent")
    if row["status"] != "OPEN":
        # The designated worker may have reported between commissioning and
        # graph adoption. Retain that original assignment and execution.
        reported = target == route_id and conn.execute(
            "SELECT 1 FROM v62_module_reports WHERE run_id=? AND module_instance_id=?", (rid, module)).fetchone()
        if not reported:
            raise ScopeDenied("Bind a current assignment or cite a delivered report as evidence")


def recheck_bindings(conn, request, ops):
    for item in ops:
        spec = item["spec"]
        if spec["op"] == "bind_intent" and "module_instance_id" in spec:
            _module(conn, request["run_id"], request["goal_version"], spec["module_instance_id"], spec["intent"])


def _evidence_ref(conn, rid, ref):
    # A source ID and its material name the same observation. New control records
    # (including a paginated brief) do not supply a measurement or new material.
    material = conn.execute("""SELECT json_extract(payload_json,'$.artifact_ref') FROM events
             WHERE subject_kind='source' AND subject_id=? AND run_id=? AND kind='v6.observation.added'
             ORDER BY event_id LIMIT 1""", (ref, rid)).fetchone()
    ref = material[0] if material else ref
    kind = conn.execute("SELECT kind FROM artifacts WHERE artifact_ref=? AND run_id=?", (ref, rid)).fetchone()
    return None if kind and kind[0] in ("research_brief", "research_decision") else ref


def _source_identities(conn, rid, refs):
    """Compare the underlying bytes of artifact and observation aliases at a frozen decision."""
    return {row[0] for row in conn.execute("""SELECT DISTINCT a.sha256 FROM json_each(?) selected
        LEFT JOIN events e ON e.subject_kind='source' AND e.subject_id=selected.value
          AND e.run_id=? AND e.kind='v6.observation.added'
        JOIN artifacts a ON a.artifact_ref=coalesce(json_extract(e.payload_json,'$.artifact_ref'),selected.value)
          AND a.run_id=? AND a.kind NOT IN ('research_brief','research_decision')""",
        (json.dumps(sorted(refs)),rid,rid))}


def _new_basis(conn, request, route, spec):
    from . import harness_policy
    rid, basis = request["run_id"], route["last_context_ref"]
    if basis == request["context_ref"]:
        raise SchemaInvalid("A reopened route needs evidence received after its settled revision")
    policy = harness_policy.current(conn, rid)
    if feedback := spec.get("feedback_id"):
        current = conn.execute("SELECT snapshot_json FROM research_contexts WHERE run_id=? AND context_ref=?",
                               (rid, request["context_ref"])).fetchone()
        delivered = next((entry for entry in json.loads(current[0]).get("pi_dialogue", ())
                          if entry.get("feedback_id") == feedback and entry["kind"] == "feedback"), None)
        last = conn.execute("""SELECT event_id FROM events WHERE run_id=? AND subject_kind='research_intent'
                            AND subject_id=? AND kind='v6.route.revised' ORDER BY event_id DESC LIMIT 1""",
                            (rid, route["intent"])).fetchone()
        if delivered and last and delivered["event_id"] > last[0]:
            return {"kind":"pi_feedback","feedback_id":feedback} if policy else None
        raise ScopeDenied("Reopening requires new PI feedback delivered to this Lead after the route decision")
    prior = conn.execute("""SELECT t.visible_refs_json,d.request_json,d.decision_ref FROM v6_decision_tasks t
                         JOIN v6_decisions d ON d.task_id=t.task_id WHERE t.run_id=? AND t.context_ref=?""",
                         (rid, basis)).fetchone()
    if not prior:
        raise StaleRevision("The settled route's original decision is unavailable")
    previous = json.loads(prior["request_json"])["decision"]
    seen = set(json.loads(prior["visible_refs_json"])) | set(previous["evidence_refs"])
    references = spec.get("evidence_refs", [])
    if not policy:
        new_refs = {_evidence_ref(conn, rid, ref) for ref in references} - {None}
        if new_refs - seen:
            return None
        raise SchemaInvalid("Cite new evidence or identify new PI feedback for this route")
    cited = _source_identities(conn,rid,references)
    previous_sources = route.get("evidence_refs", [])
    reason = request["decision"]["rationale"]
    if cited - _source_identities(conn,rid,seen):
        kind = "new_evidence"
    elif (cited - _source_identities(conn,rid,previous_sources)
          and reason.split() != previous["rationale"].split()):
        kind = "reinterpretation"
    else:
        raise SchemaInvalid("Cite new evidence or a previously visible source unused by this route, "
                            "and explain how its interpretation changes the settled conclusion.")
    return {"kind":kind, "prior_revision":route["revision"], "prior_decision_ref":prior["decision_ref"],
            "prior_evidence_refs":previous_sources, "change_reason":reason}


def compile_intents(conn, request, graph, ops, aliases):
    changed = [item for item in ops if item["spec"]["op"] in OPERATIONS]
    if not changed:
        return
    if request["scope"] != "run":
        raise ScopeDenied("Only the Lead may revise Run research intents")
    intents = graph.setdefault("intents", {})
    new_nodes = {item["spec"]["node"]: item["spec"] for item in ops if item["spec"]["op"] == "add_node"}
    decision = request["decision"]
    for item in changed:
        spec = item["spec"]
        op = spec["op"]
        key = spec["intent"] = aliases.get(spec["intent"], spec["intent"])
        if op == "open_intent":
            if key in intents:
                raise SchemaInvalid("The new route intent already exists")
            route = {name: spec[name] for name in ("question", "owner", "expected_materials", "wake_condition")
                     if name in spec}
            if wake := spec.get("wake_condition"):
                spec["wake_condition"] = route["wake_condition"] = dict(wake, subject_ref=aliases.get(
                    wake["subject_ref"], wake["subject_ref"]))
            if parent := spec.get("parent_intent"):
                parent = aliases.get(parent, parent)
                if parent not in intents or intents[parent]["goal_version"] != request["goal_version"]:
                    raise ScopeDenied("The parent intent must belong to this Goal version and precede its child")
                route["parent_intent"] = parent
            route.update(intent=key, phase="OPEN", binding_state="INTENT_UNBOUND", goal_version=request["goal_version"],
                         revision=1, last_context_ref=request["context_ref"])
            intents[key] = route
            continue
        route = intents.get(key)
        if not route or route["goal_version"] != request["goal_version"]:
            raise ScopeDenied("The route intent is outside this Goal version")
        if route["revision"] != spec["revision"]:
            raise StaleRevision("The route intent changed; use the current revision",
                                details={"intent": key, "current_revision": route["revision"], "phase": route["phase"]})
        if op == "reopen_intent" and route["phase"] == "OPEN":
            raise SchemaInvalid("This route is already open")
        if op != "reopen_intent" and route["phase"] != "OPEN":
            raise SchemaInvalid("Reopen the settled route before changing it")
        rev = route["revision"] + 1
        if op == "bind_intent":
            if ("nodes" in spec) == ("module_instance_id" in spec):
                raise SchemaInvalid("Select one module or a nonempty list of newly declared nodes")
            if "nodes" in spec:
                targets = spec["nodes"] = [aliases.get(node, node) for node in spec["nodes"]]
                for node in targets:
                    if node not in new_nodes or new_nodes[node].get("route_intent"):
                        raise SchemaInvalid("A route may bind a new node only once; cite existing work as evidence")
                    new_nodes[node].update(route_intent=key, intent_revision=rev)
                    graph["nodes"][node].update(route_intent=key, intent_revision=rev)
                route["binding"] = {"nodes": targets}
            else:
                module = spec["module_instance_id"]
                _module(conn, request["run_id"], request["goal_version"], module, key)
                if any(pin["module_instance_id"] == module for other in intents.values()
                       for pin in other.get("module_bindings", ())):
                    raise ScopeDenied("The commissioned module is already bound; preserve its original revision")
                route.setdefault("module_bindings", []).append({"module_instance_id": module, "intent_revision": rev})
                route["binding"] = {"module_instance_id": module}
            route["binding_state"] = "BOUND_EXECUTABLE"
        else:
            refs = spec.get("evidence_refs", [])
            if not set(refs).issubset(decision["evidence_refs"]):
                raise ScopeDenied("Cite each route's supporting material in the decision evidence_refs")
            if refs and not any(_evidence_ref(conn, request["run_id"], ref) for ref in refs):
                raise SchemaInvalid("Cite an original material, observation, report or scientific finding")
            if op == "reopen_intent":
                basis = _new_basis(conn, request, route, spec)
                if basis:
                    route["reopen_basis"] = basis
                    item["reopen_basis"] = basis
            route["phase"] = {"resolve_intent": "RESOLVED", "converge_intent": "CONVERGED", "reopen_intent": "OPEN"}[op]
            route["evidence_refs"] = refs
            if "feedback_id" in spec:
                route["feedback_id"] = spec["feedback_id"]
        route.update(revision=rev, last_context_ref=request["context_ref"])
    for item in changed:
        operation = item["spec"]
        if operation["op"] in ("open_intent", "reopen_intent"):
            final_revision = intents[operation["intent"]]["revision"] == operation.get("revision", 0) + 1
            item["needs_decision"] = final_revision and (
                operation["op"] == "reopen_intent" or not operation.get("wake_condition"))


def record(conn, plan, item, decision_ref):
    spec = item["spec"]
    payload = {"scope": "run", "goal_version": plan.request["goal_version"], "context_ref": plan.request["context_ref"],
               "op": spec["op"], "revision": spec.get("revision", 0) + 1, "decision_ref": decision_ref,
               "graph_version": plan.result_head["version"]}
    if "reopen_basis" in item:
        payload["reopen_basis"] = item["reopen_basis"]
    if "needs_decision" in item:
        payload["needs_decision"] = item["needs_decision"]
    store.append_event(conn, run_id=plan.request["run_id"], kind="v6.route.revised", actor=plan.thread_id,
                       subject_kind="research_intent", subject_id=spec["intent"], payload=payload)


def require_open(conn, rid, route_intent):
    """A small indexed event lookup also works inside the module admission transaction."""
    from . import v621_goal_scope
    row = conn.execute("""SELECT payload_json FROM events WHERE run_id=? AND subject_kind='research_intent'
        AND subject_id=? AND kind='v6.route.revised' ORDER BY event_id DESC LIMIT 1""",
        (rid, route_intent)).fetchone()
    status = json.loads(row[0]) if row else {}
    if (status.get("goal_version") != v621_goal_scope.active(conn, rid)
            or status.get("op") not in ("open_intent", "bind_intent", "reopen_intent")):
        raise ScopeDenied("This route is settled, absent or belongs to another Goal version")


def locally_bound_openings(conn, rid, operations):
    """A whole-Run convergence choice can continue an explicitly open route."""
    openings = {op["branch"] for op in operations if op["op"] == "open_branch"}
    nodes = {op["node"] for op in operations if op["op"] == "add_node" and op["branch"] in openings}
    routes = [op for op in operations if op["op"] == "bind_intent"]
    reopening = {op["intent"] for op in operations if op["op"] == "reopen_intent"}
    for route in routes:
        # The same atomic draft may already have validated a late reopening
        # against the pinned route revision and the delivered evidence.
        if route["intent"] not in reopening:
            require_open(conn, rid, route["intent"])
    return (all(any(op["op"] == "add_node" and op["branch"] == branch for op in operations) for branch in openings)
            and nodes <= {node for route in routes for node in route.get("nodes", ())})


def projection(conn, rid, head=None, *, version=None):
    if head is None:
        row = conn.execute("SELECT graph_json FROM v6_graph_versions WHERE run_id=? ORDER BY version DESC LIMIT 1",
                           (rid,)).fetchone()
    else:
        row = conn.execute("SELECT graph_json FROM v6_graph_versions WHERE run_id=? AND version=? AND sha256=?",
                           (rid, head["version"], head["sha256"])).fetchone()
    if not row:
        return []
    graph = json.loads(row[0])
    intents = graph.get("intents", {})
    if not intents:
        return []
    for route in intents.values():
        route["executions"] = []
    # Jobs keep their original graph version and route pin after a rebind. Do not
    # include scoped workers' private Job results in the Run Lead's projection.
    for node in conn.execute("""SELECT node_id,version,job_id,status,result_ref,spec_json FROM v6_nodes
            WHERE run_id=? AND version<=? AND json_type(spec_json,'$.route_intent')='text' ORDER BY rowid""",
            (rid, graph["version"])):
        spec = json.loads(node["spec_json"])
        if spec["route_intent"] in intents:
            intents[spec["route_intent"]]["executions"].append({
                **{k: node[k] for k in ("node_id", "version", "job_id", "status", "result_ref")},
                "intent_revision": spec["intent_revision"]})
    return [route for route in intents.values() if version is None or route["goal_version"] == version]
