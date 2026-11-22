"""Run-owned module tasks, per-module progress, and a project decision by the coordinator."""
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import GxError, SchemaInvalid
from gx_core.orchestration import InterruptRequest, NodeResult, expansion, stop
from gx_core.persistence import store
from gx_core.teams.service import MAX_INPUT_BYTES, backend_ctx

from . import products as p


def scope(ctx):
    modules = ctx.inputs.get("modules")
    p.entries(modules, role_key="lead_role")
    run = ctx.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()
    body = {**store.loads(run, "inputs_json"), "objective": ctx.inputs["objective"]}
    return NodeResult(outputs={"artifact_ref": p.publish(ctx, "module_project_scope", body)})


def _read(ctx, ref):
    return ArtifactService(ctx.conn).read_json(ref)


def _task(ctx, stage, module, module_index, role, dependencies, *, item=None, item_index=None, extra=None):
    identity = ids.sha256_of_obj([module["key"], stage, item["key"] if item else None])
    key = expansion.round_key(ctx.run_id, ctx.node, ctx.round, stage, identity)
    existing = expansion.replay(ctx.conn, key)
    if existing is not None:
        return existing["task_ids"][0]
    inputs = {"assignment": stage, "module": module, "round": ctx.round, "scope_ref": ctx.outputs["scope"]["artifact_ref"],
              "instruction": p.BRIEFS[stage] + p.HAND_IN, **(extra or {})}
    if item:
        inputs["item"] = item
    if len(ids.canonical_json(inputs).encode()) > MAX_INPUT_BYTES:
        # These original entries already exist in scope/plan. Indexes stay unambiguous
        # even for long keys sharing a display prefix.
        inputs["module"] = {"key": module["key"][:160], "scope_index": module_index}
        inputs["instruction"] += " Read the full module at scope_ref inputs.modules[scope_index]."
        if item:
            inputs["item"] = {"key": item["key"][:160], "plan_index": item_index}
            inputs["instruction"] += " Read your full work item at plan_ref items[plan_index]."
        if len(ids.canonical_json(inputs).encode()) > MAX_INPUT_BYTES:
            # Large result lists or coordinator messages are fetched with the existing artifact reader.
            ref = p.publish(ctx, "module_assignment", inputs, name=identity)
            inputs = {k: inputs[k] for k in ("assignment", "module", "round", "scope_ref", "instruction", "item")
                      if k in inputs}
            inputs["assignment_ref"] = ref
            inputs["instruction"] = f"Read the complete assignment at {ref} with gx_artifact_get. " + inputs["instruction"]
    spec = {"stage": ctx.node, "kind": "research_round", "role": role, "bundle_kind": None,
            "depends_on": dependencies, "parallel_group": f"modules:r{ctx.round}:{stage}",
            "acceptance": {"validate_workflow_product": True}, "required_artifacts": [p.KINDS[stage]],
            "input": inputs}
    made = expansion.expand(ctx.gx or backend_ctx(ctx.conn), ctx.run_id, rule=stage, key=key,
        trigger={"module_key": module["key"], "round": ctx.round}, tasks=[spec],
        stage=ctx.node, round_index=ctx.round, teams=ctx.teams)
    return made["task_ids"][0]


def _module(ctx, module, module_index, previous, feedback):
    role = module.get("lead_role", "planner")
    previous_synthesis = previous.get("synthesis")
    dependencies = [previous_synthesis["task_id"]] if previous_synthesis else []
    inputs = {"previous_handoff_ref": feedback["handoff_ref"], "return_decision": feedback["decision"],
              "lead_subject": previous_synthesis["author"],
              "previous_synthesis_ref": previous_synthesis["artifact_ref"]} if previous_synthesis else {}
    plan_id = _task(ctx, p.PLAN, module, module_index, role, dependencies, extra=inputs)
    plan = p.accepted(ctx.conn, ctx.run_id, plan_id, p.PLAN)
    if plan is None:
        return [plan_id], None

    results = list(previous.get("results", []))
    pending = []
    for item_index, item in enumerate(_read(ctx, plan["artifact_ref"])["items"]):
        task_id = _task(ctx, p.WORK, module, module_index, item["role"], [plan_id], item=item, item_index=item_index,
                        extra={**inputs, "plan_ref": plan["artifact_ref"], "lead_subject": plan["author"]})
        result = p.accepted(ctx.conn, ctx.run_id, task_id, p.WORK)
        if result is None:
            pending.append(task_id)
        else:
            results.append({**result, "item": item["key"], "round": ctx.round})
    if pending:
        return pending, None

    result_refs = [r["artifact_ref"] for r in results]
    synthesis_id = _task(ctx, p.SYNTHESIS, module, module_index, role, [plan_id, *(r["task_id"] for r in results)],
        extra={**inputs, "plan_ref": plan["artifact_ref"], "lead_subject": plan["author"], "result_refs": result_refs})
    synthesis = p.accepted(ctx.conn, ctx.run_id, synthesis_id, p.SYNTHESIS)
    if synthesis is None:
        return [synthesis_id], None
    return [], {"plan": plan, "results": results, "synthesis": synthesis}


def modules(ctx):
    feedback = ctx.state.get("module_return", {})
    previous = _read(ctx, feedback["handoff_ref"])["modules"] if feedback else {}
    selected = set(feedback["decision"]["modules"]) if feedback else set()
    pending, collected = [], {}
    for module_index, module in enumerate(ctx.inputs["modules"]):
        key = module["key"]
        if feedback and key not in selected:
            collected[key] = previous[key]
            continue
        outstanding, record = _module(ctx, module, module_index, previous.get(key, {}), feedback)
        pending.extend(outstanding)
        if record is not None:
            collected[key] = record
    if pending:
        return NodeResult(interrupt=InterruptRequest(kind="stage", tasks=tuple(pending),
            reason="Dispatch each available module task. Resume after a module plan or its workers finish.",
            payload={"stage": ctx.node, "waiting_state": "WAITING_CLIENT"}))
    return NodeResult(outputs={"modules": collected})


def _checkpoint(conn, run_id):
    return store.loads(store.latest_checkpoint(conn, run_id), "state_json", {}) or {}


def modules_rule(conn, run_id, round_index):
    state = _checkpoint(conn, run_id)
    feedback = state.get("scratch", {}).get("module_return")
    selected = feedback["decision"]["modules"] if feedback else state.get("inputs", {}).get("modules", [])
    count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE run_id=? AND stage='modules' AND json_extract(input_json,'$.assignment')=? "
        "AND status='ACCEPTED' AND json_extract(input_json,'$.round')=?", (run_id, p.SYNTHESIS, round_index)).fetchone()[0]
    return {"complete": bool(selected) and count == len(selected), "needs": []}


def handoff(ctx):
    body = {"run_id": ctx.run_id, "round": ctx.round, "scope_ref": ctx.outputs["scope"]["artifact_ref"],
            "modules": ctx.outputs["modules"]["modules"]}
    if ctx.round:
        body["previous"] = ctx.state["module_return"]
    ref = p.publish(ctx, "module_project_handoff", body)
    return NodeResult(outputs={"artifact_ref": ref})


def _decision(conn, run_id, handoff_ref, keys):
    # TeamManager persisted the sender. The decision applies to one immutable handoff.
    rows = conn.execute("SELECT m.message_id,m.sender_subject,m.payload_json FROM team_messages m "
        "JOIN coordinator_leases c ON c.run_id=m.run_id AND c.owner=m.sender_subject "
        "WHERE m.run_id=? AND m.kind='response' AND json_extract(m.payload_json,'$.artifact_refs[0]')=? "
        "ORDER BY m.rowid DESC", (run_id, handoff_ref))
    for row in rows:
        payload = store.loads(row, "payload_json")
        body = payload.get("body")
        if (not isinstance(body, dict) or payload["artifact_refs"] != [handoff_ref]
                or body.get("action") not in ("accept", "return", "stop") or not p.text(body.get("reason"))):
            continue
        selected = body.get("modules", [])
        if not isinstance(selected, list):
            continue
        # Long module keys need not fit the team's bounded message. Scope order is fixed
        # by the original run input and each supplied index identifies exactly one key.
        selected = [keys[key] if type(key) is int and 0 <= key < len(keys) else key for key in selected]
        if not all(p.text(key) for key in selected) or len(set(selected)) != len(selected) or set(selected) - set(keys):
            continue
        if bool(selected) != (body["action"] == "return"):
            continue
        decision = {**body, "message_id": row["message_id"], "author": row["sender_subject"]}
        if selected:
            decision["modules"] = selected
        return decision
    return None


def decision_rule(conn, run_id, round_index):
    state = _checkpoint(conn, run_id)
    handoff_ref = state.get("outputs", {}).get("handoff", {}).get("artifact_ref")
    keys = [m["key"] for m in state.get("inputs", {}).get("modules", [])]
    return {"complete": bool(handoff_ref and _decision(conn, run_id, handoff_ref, keys)), "needs": []}


def project_decision(ctx):
    ref = ctx.outputs["handoff"]["artifact_ref"]
    keys = [m["key"] for m in ctx.inputs["modules"]]
    decision = _decision(ctx.conn, ctx.run_id, ref, keys)
    if decision is None:
        action = (f"Root: read handoff {ref} and its original products with gx_artifact_get. "
                  "Send gx_team_message action=send with the coordinator lease and existing team IDs, "
                  f'kind=response, a fresh correlation_key, artifact_refs=["{ref}"], and body '
                  '{"action":"accept","reason":"your project conclusion"}. For return, give action=return, '
                  'the affected modules in modules:["key"] or their zero-based scope indexes, and the missing work. For stop, '
                  'give action=stop and the reason. Then gx_run_resume.')
        return NodeResult(interrupt=InterruptRequest(kind="stage", reason=action,
            payload={"stage": ctx.node, "waiting_state": "WAITING_CLIENT", "handoff_ref": ref,
                     "coordinator_action": action}))
    if decision["action"] == "return":
        if not ctx.rounds_left:
            return stop("The project still needs work and its return budget is exhausted. " + decision["reason"],
                        rule="module_return_budget")
        ctx.state["module_return"] = {"handoff_ref": ref, "decision": decision}
        return NodeResult(reenter="modules", reason=decision["reason"])
    if decision["action"] == "stop":
        return stop("Coordinator ended the module collaboration. " + decision["reason"], rule="module_project_stop")
    return NodeResult(outputs=decision)


def report(ctx):
    body = {"run_id": ctx.run_id, "scope": "Coordinator acceptance of the module handoff.",
            "handoff_ref": ctx.outputs["handoff"]["artifact_ref"], "decision": ctx.outputs["project_decision"]}
    return NodeResult(outputs={"artifact_ref": p.publish(ctx, "module_project_report", body)})


def completion_evidence(conn, run):
    try:
        state = _checkpoint(conn, run["run_id"])
        outputs = state["outputs"]
        assets = ArtifactService(conn)
        report_body = assets.read_json(outputs["report"]["artifact_ref"])
        handoff_ref = outputs["handoff"]["artifact_ref"]
        handoff_body = assets.read_json(handoff_ref)
        decision = outputs["project_decision"]
        keys = {m["key"] for m in state["inputs"]["modules"]}
        if (report_body["run_id"] != run["run_id"] or handoff_body["run_id"] != run["run_id"]
                or report_body["handoff_ref"] != handoff_ref or report_body["decision"] != decision
                or decision["action"] != "accept" or set(handoff_body["modules"]) != keys
                or handoff_body["modules"] != outputs["modules"]["modules"]):
            raise SchemaInvalid("the project report must cover the accepted handoff of every selected module")
    except (GxError, KeyError, TypeError, ValueError, OSError) as error:
        return False, [str(error)]
    return True, []
