"""Adopt an authenticated Scientific Lead decision and its dispatch intents atomically."""
from dataclasses import dataclass
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..artifacts import ArtifactService, delivery
from ..domain import clock, ids
from ..errors import BudgetExceeded, IdempotencyConflict, SchemaInvalid, ScopeDenied, StaleRevision
from ..persistence import store
from . import intent, v6_graph, v62_graph

INSTANCE = "gx.v6.instance"
decision_schema = v6_graph.decision_schema


def head(conn, run_id):
    row = conn.execute("SELECT version,sha256 FROM v6_graph_versions WHERE run_id=? "
                       "ORDER BY version DESC LIMIT 1", (run_id,)).fetchone()
    return dict(row) if row else None


def replay(conn, *, run_id, thread_id, turn_id, call_id, decision):
    """Read a committed receipt after disconnect, lease retirement, or Run closure."""
    row = conn.execute("SELECT run_id,turn_id,request_json,receipt_json FROM v6_decisions "
                       "WHERE thread_id=? AND call_id=?", (thread_id, call_id)).fetchone()
    if row is None:
        return None
    original = json.loads(row["request_json"])
    if (row["run_id"], row["turn_id"], original["draft_sha256"]) != (
            run_id, turn_id, ids.sha256_of_obj(decision)):
        raise IdempotencyConflict("A native decision call was replayed with different identity or content")
    return json.loads(row["receipt_json"])


@dataclass(frozen=True)
class Prepared:
    request: dict
    thread_id: str
    turn_id: str
    call_id: str
    manifest: object
    graph_json: str
    result_head: dict | None
    resolved_ops: list
    aliases: dict
    watches: list
    funding_pin: tuple | None
    config_pin: tuple
    capability_pins: dict
    module_demand: dict
    module_dependencies: dict
    endpoint_inventory: tuple | None
    comparison_registration: dict | None
    selected_receipts: dict | None


def authority(conn, *, run_id, task_id, lease_id, thread_id, turn_id):
    return _authority(conn, run_id=run_id, task_id=task_id, lease_id=lease_id,
                      thread_id=thread_id, turn_id=turn_id)[:4]


def module_authority(conn, **identity):
    """Bind module reads and decisions to the same current native turn and active grant."""
    _, task, _, _, instance = _authority(conn, **identity)
    if instance is None:
        raise ScopeDenied("This native task has no module delegation")
    return task, instance


def _authority(conn, *, run_id, task_id, lease_id, thread_id, turn_id):
    from .roots import require_binding
    root = require_binding(conn, run_id, thread_id)
    if (root["task_id"], root["lease_id"]) != (task_id, lease_id):
        raise ScopeDenied("The native callback does not own this decision task and lease")
    task = conn.execute("""SELECT dt.*,t.kind,c.event_cursor,c.snapshot_json,c.run_id AS context_run
        FROM v6_decision_tasks dt JOIN tasks t USING(task_id)
        JOIN research_contexts c USING(context_ref) WHERE dt.task_id=? AND dt.run_id=?""",
                        (task_id, run_id)).fetchone()
    if task is None or task["kind"] != "decide_revision" or task["context_run"] != run_id:
        raise ScopeDenied("This native task has no decision delegation for this Run")
    turn = conn.execute("""SELECT d.turn_id,u.terminal_status FROM p0_root_deliveries d
        LEFT JOIN p0_root_turn_usage u USING(thread_id,turn_id)
        WHERE d.run_id=? AND d.thread_id=? AND d.method='turn/start' AND d.state='acknowledged'
        ORDER BY d.rowid DESC LIMIT 1""", (run_id, thread_id)).fetchone()
    if turn is None or turn["turn_id"] != turn_id or turn["terminal_status"]:
        raise ScopeDenied("The native turn is unacknowledged, superseded, or terminal")
    goal = conn.execute("SELECT * FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
    objective = conn.execute("SELECT version,status FROM p0_intent_versions WHERE run_id=? AND item_id='objective' "
                             "ORDER BY version DESC LIMIT 1", (run_id,)).fetchone()
    if (not goal or goal["status"] != "OPEN" or goal["goal_version"] != task["goal_version"] or
            not objective or objective["status"] != "active" or objective["version"] != task["goal_version"]):
        raise StaleRevision("The human Goal or decision delegation changed before adoption")
    module = None
    if task["scope"].startswith("module:"):
        from . import v62_modules
        module = v62_modules.require_active(conn, run_id, task["scope"])
    elif task["scope"] != "run":
        from .v6_delegation import require_grant
        require_grant(conn, run_id, task_id, task["scope"])
        branch = conn.execute("SELECT status FROM v6_branches WHERE run_id=? AND branch_id=?",
                              (run_id, task["scope"].removeprefix("branch:"))).fetchone()
        if not branch or branch[0] != "OPEN":
            raise ScopeDenied("This decision branch is no longer open")
    base = json.loads(task["base_json"]) if task["base_json"] else None
    current = head(conn, run_id)
    if base != current and not task["scope"].startswith("module:"):
        raise StaleRevision("The scoped brief is based on an older graph", details={"current_head": current})
    return root, task, goal, base, module


def _visible(conn, task, lease_id, thread_id, turn_id, decision):
    from . import v6_claims
    rid, tid, context = task["run_id"], task["task_id"], task["context_ref"]
    refs = set(json.loads(task["visible_refs_json"]))
    refs.update(v6_claims.evaluation_refs(conn, rid, tid, lease_id, thread_id, turn_id))
    claim_refs = {ref for op in decision["ops"] for ref in op.get("claim_refs", [])}
    cited = set(decision["evidence_refs"]) | claim_refs
    staged = set(v6_claims.staged_refs(conn, rid, tid, lease_id, context, cited))
    if cited - refs - staged:
        raise ScopeDenied("The decision cites evidence outside its frozen context and scoped tool receipts")
    for ref in claim_refs - staged:
        if not v6_claims.read_revision(conn, rid, ref):
            raise SchemaInvalid("A close_run claim reference must identify an adopted or staged claim")
    return refs


def _check_bans(conn, run_id, ops):
    forbidden = {cap for rule in intent.active_items(conn, run_id, intent.BAN_TYPES).values()
                 for cap in rule["value"]["forbidden_capabilities"]}
    for op in ops:
        cap = op["spec"].get("capability")
        if cap and (cap in forbidden or cap.split("@")[0] in forbidden):
            raise ScopeDenied("This capability is prohibited by current human intent")


def _capability_pins(conn, capabilities):
    pins = {}
    for capability in capabilities:
        name, version = capability.rsplit("@", 1)
        row = conn.execute("""SELECT c.manifest_sha256,a.content_sha256,a.status,a.content_artifact_ref
            FROM capabilities c JOIN assets a ON a.asset_id=c.capability_id AND a.version=c.version
            WHERE c.capability_id=? AND c.version=? AND a.kind='TOOL_ADAPTER'""", (name, version)).fetchone()
        pins[capability] = tuple(row) if row else None
    return pins


def _decision_artifact(conn, request, root, identity):
    artifacts = ArtifactService(conn)
    with NamedTemporaryFile(dir=artifacts.inbox_dir(request["run_id"]), suffix=".json", mode="w",
                            encoding="utf-8") as source:
        source.write(ids.canonical_json(request["decision"]))
        source.flush()
        params = dict(run_id=request["run_id"], task_id=root["task_id"], attempt=root["attempt"])
        ref = delivery.prepare_manifest(artifacts, **params,
                    entries=[dict(inbox_path=Path(source.name).name, kind="research_decision",
                                  media_type="application/json")], provenance=identity)
        return delivery.load_manifest(artifacts, ref, **params)


def prepare(conn, *, run_id, task_id, lease_id, thread_id, turn_id, call_id, decision):
    if conn.in_transaction:
        raise RuntimeError("Prepare the instance graph and decision original outside the writer")
    if not all(isinstance(x, str) and x.strip() for x in (run_id, task_id, lease_id, thread_id, turn_id, call_id)):
        raise ScopeDenied("The callback needs a complete native, Run, Task and Lease identity")
    identity = dict(run_id=run_id, thread_id=thread_id, turn_id=turn_id, call_id=call_id)
    saved = replay(conn, **identity, decision=decision)
    if saved is not None:
        if (saved["task_id"], saved["lease_id"]) != (task_id, lease_id):
            raise IdempotencyConflict("Replay supplied a different task or lease")
        return saved
    root, task, goal, base, module = _authority(conn, run_id=run_id, task_id=task_id, lease_id=lease_id,
                                        thread_id=thread_id, turn_id=turn_id)
    from . import v621_policy, v623_endpoint_use
    execution = v621_policy.science_execution(conn, run_id)
    science = v621_policy.live_science(conn, run_id)
    original_decision = decision
    inventory, selected = None, None
    if execution:
        from . import v624_operations, v62_native, v6_native
        wire = v62_native.evidence_tool(v6_native.DECIDE, v621_policy.VERSION)
        v6_graph._shape(decision, wire["inputSchema"]["properties"]["decision"])
        decision, implicit = v624_operations.normalize(conn, task, goal, decision, module=module)
        if task["scope"] == "run":
            from . import v624_comparison
            metrics, received, selected = {}, {}, {}
            if decision.get("metric_refs") or any(op["op"] == "close_run" for op in decision["ops"]):
                inventory = v623_endpoint_use.prepare(conn, task, metric_details=metrics)
                selected = v623_endpoint_use.resolve_delivered(conn, task, root, turn_id, decision, inventory,
                                                               received_metrics=received)
            delivered = v6_native.acknowledged_brief(conn, root, turn_id)
            decision = v624_comparison.normalize(conn, task, decision, metrics, received,
                                                 packet=delivered[0] if delivered else {})
        v6_graph.validate_body(decision, measurement_branch=implicit)
    else:
        v6_graph.validate_body(decision)
        if {"prediction", "reference_refs"}.intersection(decision.get("comparison_plan", {})):
            raise ScopeDenied("These comparison plan fields require the science execution policy")
    declared = any(key in decision for key in v623_endpoint_use.FIELDS)
    if declared and not (science and task["scope"] == "run"):
        raise ScopeDenied("Structured endpoint and comparison decisions require a V6.2.3 Run Lead")
    original = base
    if module is not None:
        base = head(conn, run_id)
        v62_graph.require_operations(decision["ops"])
    if base is None:
        if module is None:
            limit = json.loads(task["snapshot_json"]).get("budget", {}).get("initial_job_limit")
        else:
            limit = conn.execute("SELECT json_extract(inputs_json,'$.inputs.p0_spec.initial_job_limit') "
                                 "FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
        if limit is not None and sum(op["op"] == "add_node" for op in decision["ops"]) > limit:
            raise BudgetExceeded(f"The first graph may commission at most {limit} Jobs")
    request = dict(schema_version="gx.v6.decision.1", run_id=run_id, decision_task_id=task_id, lease_id=lease_id,
                   scope=task["scope"], goal_version=task["goal_version"], context_ref=task["context_ref"],
                   base=base, idempotency_key="v6.decision:" + ids.sha256_of_obj([thread_id, call_id]),
                   draft_sha256=ids.sha256_of_obj(original_decision), decision=json.loads(ids.canonical_json(decision)))
    if execution:
        request.update(original_decision=json.loads(ids.canonical_json(original_decision)),
                       normalized_sha256=ids.sha256_of_obj(decision))
    refs = _visible(conn, task, lease_id, thread_id, turn_id, decision)
    if decision.get("resolved_gaps"):
        from . import v621_gaps, v621_policy
        if v621_policy.enabled(conn, run_id):
            request["gap_resolutions"] = v621_gaps.prepare(conn, task, decision)
    previous = v62_graph.graph_at(conn, run_id, base)
    allowed = json.loads(goal["capabilities_json"])
    config_pin = (goal["capabilities_json"], goal["materials_json"])
    if module is not None:
        request.update(original_base=original, module_instance_id=module["module_instance_id"])
        if base != original:
            v62_graph.require_compatible(v62_graph.graph_at(conn, run_id, original), previous,
                                        module["branch_id"], decision["ops"])
        grant = json.loads(module["grant_json"])
        if "gx_decide_revision" not in grant["tools"]:
            raise ScopeDenied("This module's grant excludes graph decisions")
        from .v62_catalog import effective_methods
        allowed = set(allowed) & effective_methods(conn, run_id, grant)
        config_pin = (module["grant_json"], module["snapshot_json"], module["budget_json"])
    caps = {op["capability"] for op in decision["ops"] if op["op"] == "add_node"}
    if any("@" not in cap for cap in caps):
        raise SchemaInvalid("A node requires a pinned capability id@version")
    cap_pins = _capability_pins(conn, caps)
    module_jobs = [] if module is not None else None
    graph, ops, aliases, watches = v6_graph.compile_graph(conn, request, previous, refs,
                                                         allowed, module=module, module_jobs=module_jobs)
    costs = v62_graph.job_demand(conn, run_id, module_jobs) if module_jobs else {}
    dependencies = v62_graph.dependency_pins(conn, run_id, ops) if module is not None else {}
    if costs:
        v62_graph.require_budget(conn, module, costs)
    demand = v6_graph.branch_demand(graph, ops)
    _check_bans(conn, run_id, ops)
    funding = v6_graph.funding_pin(conn, run_id) if demand[1] else None
    v6_graph.check_funding(conn, run_id, demand)
    manifest = _decision_artifact(conn, request, root, dict(identity, context_ref=task["context_ref"],
                                                             lease_id=lease_id, base=base))
    result = base
    if decision["mode"] == "amend":
        version = base["version"] + 1 if base else 1
        graph.update(run_id=run_id, version=version, parent=base, decision_ref=manifest.entries[0]["artifact_ref"])
        result = dict(version=version, sha256=ids.sha256_of_obj(graph))
    comparison = None
    if science and task["scope"] == "run":
        from . import v623_comparison
        comparison = v623_comparison.prepare(conn, task, decision)
        if inventory is None and (declared or any(op["op"] == "close_run" for op in decision["ops"])):
            inventory = v623_endpoint_use.prepare(conn, task)
    return Prepared(request, thread_id, turn_id, call_id, manifest, ids.canonical_json(graph), result, ops,
                    aliases, watches, funding, config_pin, cap_pins, costs, dependencies, inventory, comparison, selected)


def _revoke(conn, run_id, where, key):
    # SQL predicate is supplied by this module. Jobs already accepted keep their identity and receipts.
    conn.execute(f"UPDATE v6_nodes SET status='CANCEL_REQUESTED' WHERE run_id=? AND {where}=? "
                 "AND status IN ('PENDING','RUNNING')", (run_id, key))


def _apply_ops(conn, plan, decision_ref, module, decision_facts):
    run_id = plan.request["run_id"]
    dispatches = []
    if module is not None and any(item["spec"]["op"] == "add_node" for item in plan.resolved_ops):
        branch = v62_graph.branch_spec(module)
        conn.execute("INSERT INTO v6_branches(branch_id,run_id,decision_ref,spec_json) VALUES(?,?,?,?) "
                     "ON CONFLICT(branch_id) DO NOTHING",
                     (module["branch_id"], run_id, decision_ref, ids.canonical_json(branch)))
    # Declarations may be forward referenced in the same atomic draft.
    for item in plan.resolved_ops:
        spec = item["spec"]
        if spec["op"] == "open_branch":
            conn.execute("INSERT INTO v6_branches(branch_id,run_id,decision_ref,spec_json) VALUES(?,?,?,?)",
                         (spec["branch"], run_id, decision_ref, ids.canonical_json(spec)))
    for item in plan.resolved_ops:
        spec, digest = item["spec"], item["op_digest"]
        op = spec["op"]
        if op == "add_node":
            node = spec["node"]
            key = "v6.node:" + ids.sha256_of_obj([run_id, node, plan.result_head["sha256"]])
            conn.execute("INSERT INTO v6_nodes(node_id,run_id,branch_id,version,decision_ref,"
                         "spec_json,operation_key) VALUES(?,?,?,?,?,?,?)",
                         (node, run_id, spec["branch"], plan.result_head["version"], decision_ref,
                          ids.canonical_json(spec), key))
            dispatch = dict(run_id=run_id, node_id=node, branch=spec["branch"], operation_key=key,
                            decision_ref=decision_ref, op_digest=digest)
            store.enqueue_outbox(conn, topic="v6.node", idempotency_key=key, payload=dispatch)
            dispatches.append(dispatch)
        elif op == "cancel_node":
            v6_graph.require_cancellable(conn, run_id, spec["node"])
            _revoke(conn, run_id, "node_id", spec["node"])
            store.append_event(conn, run_id=run_id, kind="v6.node.cancel_requested", actor=plan.thread_id,
                subject_kind="node", subject_id=spec["node"], new_state="CANCEL_REQUESTED",
                payload=dict(decision_id=decision_ref, op_digest=digest))
        elif op == "stop_branch":
            conn.execute("UPDATE v6_branches SET status='STOPPING',stop_decision_ref=?,stop_op_digest=? "
                         "WHERE run_id=? AND branch_id=?",
                         (decision_ref, digest, run_id, spec["branch"]))
            store.append_event(conn, run_id=run_id, kind="v6.branch.stop_requested", actor=plan.thread_id,
                subject_kind="branch", subject_id=spec["branch"], prev_state="OPEN", new_state="STOPPING",
                payload=dict(decision_id=decision_ref, op_digest=digest))
            _revoke(conn, run_id, "branch_id", spec["branch"])
        elif op == "close_run":
            conn.execute("UPDATE v6_goals SET status='CLOSING',stop_decision_ref=?,stop_op_digest=? WHERE run_id=?",
                         (decision_ref, digest, run_id))
            store.append_event(conn, run_id=run_id, kind="v6.run.close_requested", actor=plan.thread_id,
                subject_kind="run", subject_id=run_id, prev_state="OPEN", new_state="CLOSING",
                payload=dict(decision_id=decision_ref, op_digest=digest, **decision_facts))
            _revoke(conn, run_id, "run_id", run_id)
    return dispatches


def accept_product(conn, root, content_sha256, product_ref):
    """Accept one native research product and retire just its short task/lease."""
    task_id = root["task_id"]
    states = ["SUBMITTED", "VALIDATING", "ACCEPTED"]
    if root["task_status"] == "CLAIMED":
        states.insert(0, "RUNNING")
    submission_id = ids.new_id("submission")
    conn.execute("""INSERT INTO submissions(submission_id,task_id,lease_id,attempt,brief_sha256,
        content_sha256,artifact_refs_json,accepted_count,result,created_at)
        VALUES(?,?,?,?,?,?,?,1,'ACCEPTED',?)""",
        (submission_id, task_id, root["lease_id"], root["attempt"], ids.sha256_of_obj(root["brief"]),
         content_sha256, ids.canonical_json([product_ref]), clock.now_iso()))
    for state in states:
        store.transition(conn, machine="task", subject_id=task_id, to_state=state, actor="v6.decision",
                         bump=False, reason="Scoped native research product accepted",
                         extra_updates={"owner": None, "lease_id": None} if state == "ACCEPTED" else None,
                         payload=dict(decision_ref=product_ref, submission_id=submission_id))
    store.release_lease(conn, root["lease_id"])
    return submission_id


def _require_close_context(conn, run_id, task, root, turn_id):
    # Early watches and sealed rounds are the only ways a module report enters a Run brief.
    # A report may already exist even while its commissioning Lead keeps the round unsealed.
    from . import v621_goal_scope
    version = v621_goal_scope.active(conn, run_id)
    unseen = conn.execute("""
        SELECT m.module_instance_id FROM v62_modules m JOIN v62_module_reports r USING(module_instance_id)
        WHERE m.run_id=? AND (? IS NULL OR m.goal_version=?) AND NOT EXISTS (
            SELECT 1 FROM v62_module_wakes w JOIN events e USING(event_id), json_each(e.payload_json,'$.module_instance_ids') member
            WHERE w.run_id=m.run_id AND w.round=m.round AND w.event_id<=? AND member.value=m.module_instance_id
        ) LIMIT 1""", (run_id, version, version, task["event_cursor"])).fetchone()
    if unseen:
        raise StaleRevision("A completed module report needs a fresh Lead decision before closure",
                            details={"reason": "unseen_module_report", "module_instance_id": unseen[0],
                                     "brief_cursor": task["event_cursor"]})
    if conn.execute("SELECT 1 FROM v62_modules WHERE run_id=? AND status='OPEN' "
                    "AND (? IS NULL OR goal_version=?) LIMIT 1", (run_id, version, version)).fetchone():
        raise ScopeDenied("Collect pending module reports or explicitly cancel their delegations before closure")
    from . import v621_policy, v62_rounds, v6_native
    if v621_policy.current(conn, run_id):
        delivered = v6_native.acknowledged_brief(conn, root, turn_id)
        if delivered is None:
            raise ScopeDenied("Closing requires an acknowledged research brief on the deciding Lead thread")
        packet, native_receipt = delivered
        cursor = packet["trigger"]["through_cursor"]
        if (packet["run_id"], packet["goal_version"], cursor) != (run_id, version, task["event_cursor"]):
            raise ScopeDenied("The delivered research brief and current Goal decision context disagree")
        late = v62_rounds.late_evidence(conn, run_id, cursor)
        if late is not None:
            raise StaleRevision("New research evidence arrived after the delivered brief; reconsider before closing",
                                details={"reason": "close_context_changed", "event_id": late, "goal_version": version,
                                         "brief_cursor": cursor, "context_ref": task["context_ref"]})
        return {"brief_cursor": cursor, "goal_version": version, "context_ref": task["context_ref"],
                "brief_sha256": native_receipt["text_sha256"], "brief_delivery": native_receipt["event_id"],
                "brief_turn": native_receipt["turn_id"], "closing_turn": turn_id}
    return None


def commit(conn, plan):
    """Fence the prepared result and write only durable identities, receipts and dispatch intents."""
    from . import v6_claims
    if isinstance(plan, dict):  # Historical receipt returned by prepare.
        return plan
    request = plan.request
    run_id, task_id, lease_id = request["run_id"], request["decision_task_id"], request["lease_id"]
    with store.transaction(conn, transaction_owner="V6Decision", operation="adopt_revision", run_id=run_id):
        saved = replay(conn, run_id=run_id, thread_id=plan.thread_id, turn_id=plan.turn_id, call_id=plan.call_id,
                       decision=request.get("original_decision", request["decision"]))
        if saved is not None:
            return saved
        root, task, goal, base, module = _authority(conn, run_id=run_id, task_id=task_id, lease_id=lease_id,
                                            thread_id=plan.thread_id, turn_id=plan.turn_id)
        close_context = None
        if any(item["spec"]["op"] == "close_run" for item in plan.resolved_ops):
            close_context = _require_close_context(conn, run_id, task, root, plan.turn_id)
        from . import v623_endpoint_use
        decision = request["decision"]
        decision_facts = {k: decision[k] for k in v623_endpoint_use.FIELDS if k in decision}
        if plan.selected_receipts is not None and decision.get("disposition") and close_context is None:
            from . import v62_rounds
            late = v62_rounds.late_evidence(conn, run_id, task["event_cursor"])
            if late:
                raise StaleRevision("New research evidence arrived after the delivered comparison brief",
                                    details={"reason": "comparison_context_changed", "event_id": late,
                                             "field": "comparison_id", "brief_cursor": task["event_cursor"]})
        if decision_facts:
            decision_facts.update(rationale=decision["rationale"], evidence_refs=decision["evidence_refs"])
        if plan.endpoint_inventory is not None:
            endpoint_use = v623_endpoint_use.verify(conn, task, root, plan.turn_id, decision, plan.endpoint_inventory,
                                                    closing=close_context is not None, selected=plan.selected_receipts)
            decision_facts["endpoint_use"] = endpoint_use
            if close_context is not None:
                close_context.update({k: v for k, v in endpoint_use.items() if k != "receipts"})
        config = (goal["capabilities_json"], goal["materials_json"])
        if module is not None:
            config = (module["grant_json"], module["snapshot_json"], module["budget_json"])
            actual = head(conn, run_id)
            if base != request["original_base"] or actual != request["base"]:
                raise StaleRevision("The graph moved after module preparation; obtain a fresh decision",
                                    details={"reason": "module_graph_conflict", "current_head": actual})
            if plan.module_dependencies:
                v62_graph.recheck_dependencies(conn, run_id, plan)
            if plan.module_demand:
                v62_graph.require_budget(conn, module, plan.module_demand)
            base = actual
        if ((base, task["goal_version"], task["context_ref"], task["scope"]) !=
                (request["base"], request["goal_version"], request["context_ref"], request["scope"]) or
                plan.config_pin != config):
            raise StaleRevision("The approved materials, scope or decision premises changed")
        if _capability_pins(conn, plan.capability_pins) != plan.capability_pins:
            raise StaleRevision("A capability approval or content pin changed before adoption")
        _check_bans(conn, run_id, plan.resolved_ops)
        if plan.funding_pin is not None and v6_graph.funding_pin(conn, run_id) != plan.funding_pin:
            raise StaleRevision("Funding changed while preparing the amendment; retry against the current ledger")
        decision_ref, = delivery.register_manifest(ArtifactService(conn), plan.manifest, actor=plan.thread_id)
        from . import v62_rounds
        convergence = v62_rounds.adopt(conn, request, decision_ref, task)
        claims = v6_claims.adopt(conn, run_id, task_id, lease_id, request["context_ref"],
                                 decision_ref, request["decision"])
        checkpoint, dispatches = None, []
        if request["decision"]["mode"] == "amend":
            result = plan.result_head
            conn.execute("""INSERT INTO v6_graph_versions(run_id,version,sha256,parent_sha,
                graph_json,decision_ref) VALUES(?,?,?,?,?,?)""",
                (run_id, result["version"], result["sha256"], base["sha256"] if base else None,
                 plan.graph_json, decision_ref))
            dispatches = _apply_ops(conn, plan, decision_ref, module, decision_facts)
            checkpoint = store.save_checkpoint(conn, run_id=run_id, workflow_id=INSTANCE,
                workflow_version=str(result["version"]), node="decision", state=dict(head=result, decision_ref=decision_ref))
        if request["decision"].get("approve_nodes"):
            from . import v62_evidence
            v62_evidence.record_approvals(conn, request, decision_ref)
        submission = accept_product(conn, root, request.get("normalized_sha256", request["draft_sha256"]), decision_ref)
        receipt = dict(run_id=run_id, task_id=task_id, lease_id=lease_id, task_status="ACCEPTED",
                       decision_ref=decision_ref, decision_id=decision_ref, scope=task["scope"],
                       goal_version=task["goal_version"], context_ref=task["context_ref"], base=base,
                       result_head=plan.result_head, outcome="adopted" if checkpoint else "maintained",
                       draft_sha256=request["draft_sha256"], resolved_ops=plan.resolved_ops,
                       aliases=plan.aliases, reconsider_when=plan.watches, checkpoint_id=checkpoint,
                       dispatches=dispatches, submission_id=submission, claim_revisions=claims,
                       consumed_event_cursor=task["event_cursor"])
        receipt.update(decision_facts)
        if "normalized_sha256" in request:
            receipt["normalized_sha256"] = request["normalized_sha256"]
        if "gap_resolutions" in request:
            receipt["gap_resolutions"] = request["gap_resolutions"]
        if close_context is not None:
            receipt["close_context"] = close_context
        if convergence is not None:
            receipt["convergence"] = convergence
        if "module_instance_id" in request:
            receipt.update(module_instance_id=request["module_instance_id"],
                           original_base=request["original_base"], adopted_base=base)
        receipt["event_id"] = store.append_event(conn, run_id=run_id, kind="v6.decision.committed",
            actor=plan.thread_id, subject_kind="decision", subject_id=decision_ref,
            payload=dict(decision_id=decision_ref, result_head=plan.result_head, task_id=task_id,
                         turn_id=plan.turn_id, call_id=plan.call_id, draft_sha256=request["draft_sha256"],
                         **decision_facts))
        from . import v621_policy
        if task["scope"] == "run" and v621_policy.live_science(conn, run_id):
            from . import v623_comparison
            receipt.update(v623_comparison.adopt(conn, request, task, decision_ref, plan.resolved_ops,
                                                 receipt["event_id"], registered=plan.comparison_registration))
        conn.execute("""INSERT INTO v6_decisions(decision_ref,run_id,task_id,context_ref,lease_id,thread_id,
            turn_id,call_id,request_json,receipt_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (decision_ref, run_id, task_id, task["context_ref"], lease_id, plan.thread_id, plan.turn_id,
             plan.call_id, ids.canonical_json(request), ids.canonical_json(receipt)))
        if checkpoint:
            from .v6_delegation import retire_stale
            retire_stale(conn, run_id, cause_scope=task["scope"])
            for item in plan.resolved_ops:
                op = item["spec"]
                if op["op"] == "stop_branch":
                    from . import v62_modules
                    v62_modules.cancel_for_graph(conn, run_id, op["branch"], reason=op["reason"])
        if task["scope"] == "run":
            from . import v62_reports
            v62_reports.release_parent_rounds(conn, run_id, task_id)
        return receipt


def reconsider_module_conflict(conn, identity, error):
    """After rollback retire the authenticated brief that can no longer support this decision."""
    reason = error.details.get("reason")
    if reason in ("convergence_context_changed", "close_context_changed", "comparison_context_changed"):
        from . import v62_rounds
        return v62_rounds.refresh(conn, identity, error)
    if reason not in ("module_graph_conflict", "unseen_module_report"):
        return
    from . import v6_goal
    from ..errors import GxError
    run_id = identity["run_id"]
    with store.transaction(conn, transaction_owner="V6Decision", operation=reason, run_id=run_id):
        try:
            _, task, _, original = authority(conn, **{key: identity[key] for key in
                ("run_id", "thread_id", "turn_id", "task_id", "lease_id")})
        except GxError:
            return  # A competing decision, revocation or prior conflict already retired the lease.
        if reason == "unseen_module_report" and task["scope"] == "run":
            from . import v62_reports
            v6_goal.cancel_decision(conn, run_id, task["task_id"], reason=error.message)
            v62_reports.release_parent_rounds(conn, run_id, task["task_id"])
            store.append_event(conn, run_id=run_id, kind="v62.module.close_deferred", actor=identity["thread_id"],
                               subject_kind="task", subject_id=task["task_id"], payload=error.details)
            return
        if reason != "module_graph_conflict" or not task["scope"].startswith("module:"):
            return
        v6_goal.cancel_decision(conn, run_id, task["task_id"], reason=error.message)
        store.append_event(conn, run_id=run_id, kind="v62.module.graph_conflict", actor=identity["thread_id"],
                           subject_kind="task", subject_id=task["task_id"],
                           payload={"scope": task["scope"], "original_base": original,
                                    "current_head": head(conn, run_id), **error.details})


def adopt(conn, **native_call):
    try:
        return commit(conn, prepare(conn, **native_call))
    except StaleRevision as exc:
        reconsider_module_conflict(conn, native_call, exc)
        raise
