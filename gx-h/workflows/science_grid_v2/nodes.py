"""Grid judgments against current sources and task-specific research requirements."""
from gx_core.artifacts import ArtifactService
from gx_core.orchestration import InterruptRequest, NodeResult, expansion
from gx_core.persistence import store
from gx_core.p0 import science, science_inputs, science_refresh_v2, science_v2
from workflows.science_grid.nodes import BRIEFS, _validate_grid, checked


def _accepted(conn, run, stage, round_index):
    return conn.execute("SELECT t.task_id,t.input_json,s.submission_id,s.artifact_refs_json FROM tasks t JOIN submissions s "
        "ON s.task_id=t.task_id AND s.result='ACCEPTED' AND s.attempt=t.attempt "
        "WHERE t.run_id=? AND t.stage=? AND t.status='ACCEPTED' AND json_extract(t.input_json,'$.round')=?"
        " AND json_extract(t.input_json,'$.output_kind')=? ORDER BY s.rowid DESC",
        (run, stage, round_index, "science_"+stage)).fetchone()


def assessment_rule(conn, run, round_index):
    return {"complete": bool(_accepted(conn, run, "assessment", round_index))}


def decision_rule(conn, run, round_index):
    row = _accepted(conn, run, "decision", round_index)
    if row and store.loads(row, "input_json").get("p0"):
        decision = next(science_inputs.accepted_products(conn, run, "decision", round_index), None)
        if decision:
            covered = {r["source_id"] for r in decision["body"].get("source_decisions", [])}
            required = {s["source_id"] for s in science_inputs.sources_for(conn, run, round_index)}
            return {"complete": required == covered}
    return {"complete": bool(row)}


def _judgment(ctx, role, prior=None):
    kind = "science_" + ctx.node
    saved = _accepted(ctx.conn, ctx.run_id, ctx.node, ctx.round)
    if saved and ctx.node == "decision" and ctx.inputs.get("p0") and not decision_rule(ctx.conn, ctx.run_id, ctx.round)["complete"]:
        # The human source and this old decision overlapped. Its outbox work commissions
        # one successor. Keep the historical judgment from closing this stage.
        return NodeResult(interrupt=InterruptRequest(kind="stage", reason="Await a decision covering current evidence.",
                    payload={"stage": ctx.node, "waiting_state": "WAITING_CLIENT"}))
    if saved:
        refs = store.loads(saved, "artifact_refs_json")
        ref = next(ref for ref in refs if ArtifactService(ctx.conn).describe(ref)["kind"] == kind)
        result = {"artifact_ref": ref, "task_id": saved["task_id"], "submission_id": saved["submission_id"]}
        if ctx.inputs.get("p0"):
            inputs = store.loads(saved, "input_json")
            result.update(proposal_ref=inputs["proposal_ref"], evidence_ref=inputs["evidence_refs"][0])
            if ctx.node == "decision":
                result["assessment_ref"] = inputs["assessment_ref"]
        return result
    evidence_ref = ctx.outputs["experiment"]["evidence_ref"]
    inputs = {"round": ctx.round, "max_rounds": ctx.inputs["max_rounds"], "output_kind": kind,
              "instruction": BRIEFS[ctx.node], "proposal_ref": ctx.outputs["proposal"]["proposal_ref"],
              "evidence_refs": [evidence_ref]}
    if ctx.inputs.get("p0"):
        inputs["p0"] = True
        if ctx.node == "assessment":
            inputs["instruction"] += (
                " Read each frozen evaluation and its original grid observations. Compare the threshold crossing"
                " to the path's expected_reaches_threshold. Set supported=null when outcome_kind is not"
                " valid_scientific_outcome, including a failed evaluation of a successful Job. If accepted or unresolved supplementary evidence"
                " prevents a valid interpretation, give supported=null and name the qualifying_source_ids for that path.")
        else:
            inputs["instruction"] += (
                " You may also pivot. Supply optional pivots:[{from_path,id,prediction,grid}] for a new"
                " testable prediction and fresh route identifier replacing its source. Omit a path to prune it."
                " With no continuation or pivot, explain stop_reason. Respect the maximum round count.")
    if prior:
        inputs["assessment_ref"] = prior["artifact_ref"]
        if ctx.inputs.get("p0"):
            inputs["proposal_ref"] = prior["proposal_ref"]
            inputs["evidence_refs"] = [prior["evidence_ref"]]
    if ctx.inputs.get("p0"):
        inputs = science_inputs.with_context(ctx.conn, ctx.run_id, inputs)
    spec = {"stage": ctx.node, "kind": "research_round", "role": role, "bundle_kind": None,
            "input": inputs, "acceptance": {"validate_workflow_product": True}, "required_artifacts": [kind],
            "depends_on": [prior["task_id"]] if prior else []}
    key = expansion.round_key(ctx.run_id, ctx.node, ctx.round, kind, inputs["proposal_ref"])
    with store.transaction(ctx.conn):
        tasks = expansion.replay(ctx.conn, key) or expansion.expand(ctx.gx, ctx.run_id, rule=kind, key=key, trigger={"evidence_ref":inputs["evidence_refs"][0]},
                                 tasks=[spec], stage=ctx.node, round_index=ctx.round, teams=ctx.teams)
        if ctx.inputs.get("p0"):
            proposal = ArtifactService(ctx.conn).read_json(inputs["proposal_ref"])
            for task_id in tasks["task_ids"]:
                science_refresh_v2.bind_task(ctx.conn, ctx.run_id, task_id, proposal, ctx.node)
    return NodeResult(interrupt=InterruptRequest(kind="stage", tasks=tuple(tasks["task_ids"]),
        reason=BRIEFS[ctx.node], payload={"stage":ctx.node,"waiting_state":"WAITING_CLIENT"}))


def assessment(ctx):
    result = _judgment(ctx, "evidence_reviewer")
    if isinstance(result, NodeResult):
        return result
    if ctx.inputs.get("p0"):
        science_v2.settle_assessment(ctx, result)
    return NodeResult(outputs=result)


def decision(ctx):
    result = _judgment(ctx, "planner", ctx.outputs["assessment"])
    if isinstance(result, NodeResult):
        return result
    read = ArtifactService(ctx.conn).read_json
    decision_body = read(result["artifact_ref"])
    summary = {**result, "round": ctx.round, "assessment_ref": ctx.outputs["assessment"]["artifact_ref"],
               **ctx.outputs["proposal"], **ctx.outputs["experiment"]}
    if ctx.inputs.get("p0"):
        summary.update({k: result[k] for k in ("proposal_ref","assessment_ref","evidence_ref")})
    ctx.state.setdefault("history", []).append(summary)
    if ctx.inputs.get("p0"):
        body = read(summary["proposal_ref"])
        paths, actions = science.decision_routes(body, decision_body)
        if paths and (ctx.round + 1 >= ctx.inputs["max_rounds"] or not ctx.rounds_left):
            paths = []
            actions = [{"action": "stop", "path_id": p["id"]} for p in body["paths"]]
            stop = "round_budget"
        else:
            stop = None if paths else decision_body["stop_reason"]
        paths, stop = science.record_decision(ctx, result, actions, stop, paths)
        independent = science_refresh_v2.review_for(ctx.conn, ctx.run_id, result["assessment_ref"])
        science_inputs.save_decision(ctx.conn, ctx.run_id, ctx.round, result["submission_id"],
                                    independent["submission_id"], result["artifact_ref"], actions,
                                    decision_body["reason"], stop)
        if not paths:
            return NodeResult(outputs={"stop_reason": stop})
        ctx.state["next_proposal"] = {"previous_decision_ref": result["artifact_ref"],
                                      "threshold": body["threshold"], "paths": paths}
        return NodeResult(reenter="proposal", reason=decision_body["reason"])
    selected = decision_body["continue_paths"]
    if selected and ctx.round + 1 < ctx.inputs["max_rounds"] and ctx.rounds_left:
        body = read(summary["proposal_ref"])
        grids = decision_body["next_grids"]
        ctx.state["next_proposal"] = {"previous_decision_ref": result["artifact_ref"], "threshold":body["threshold"],
            "paths":[{"id": p["id"], "prediction": p["prediction"], "grid": grids[p["id"]]}
                     for p in body["paths"] if p["id"] in selected]}
        return NodeResult(reenter="proposal", reason=decision_body["reason"])
    return NodeResult(outputs={"stop_reason": "round_budget" if selected else decision_body["stop_reason"]})


def validate(conn, task, bundle):
    inputs = task["input"]
    assets = ArtifactService(conn)
    matches = [assets.describe(ref) for ref in task["submission_artifact_refs"]]
    matches = [r for r in matches if r["task_id"] == task["task_id"] and r["kind"] == inputs["output_kind"]]
    checked(len(matches) == 1, "supply one product under this task and kind")
    lease = conn.execute("SELECT principal FROM leases WHERE lease_id=?", (task["lease_id"],)).fetchone()
    root = conn.execute("SELECT owner FROM coordinator_leases WHERE run_id=?", (task["run_id"],)).fetchone()
    if inputs.get("p0"):
        proposal_author = assets.read_json(inputs["proposal_ref"])["author_principal"]
        main = conn.execute("SELECT thread_id FROM p0_intent_main WHERE run_id=?", (task["run_id"],)).fetchone()
        checked(lease and main and lease["principal"] and lease["principal"] != main["thread_id"]
                and (inputs["output_kind"] != "science_assessment" or lease["principal"] != proposal_author),
                "commission an independent worker of the proposal")
    else:
        checked(lease and root and lease["principal"] != root["owner"], "a scientific judgment needs its assigned worker")
    if inputs["output_kind"] == "science_decision":
        assessment = (science_refresh_v2.review_for(conn, task["run_id"], inputs["assessment_ref"])
                      if inputs.get("p0") else _accepted(conn, task["run_id"], "assessment", inputs["round"]))
        checked(assessment is not None, "decision needs an accepted independent assessment")
        prior = conn.execute("SELECT l.principal FROM submissions s JOIN leases l ON l.lease_id=s.lease_id "
            "WHERE s.submission_id=?", (assessment["submission_id"],)).fetchone()
        checked(prior and prior["principal"] != lease["principal"], "delegate decision and evidence review separately")
    body = assets.read_json(matches[0]["artifact_ref"])
    checked(isinstance(body, dict) and body.get("proposal_ref")==inputs["proposal_ref"], "wrong proposal reference")
    proposal_body = assets.read_json(inputs["proposal_ref"])
    routes = {p["id"]:p for p in proposal_body["paths"]}
    if inputs.get("p0"):
        evidence = set(inputs["evidence_refs"])
        for ref in inputs["evidence_refs"]:
            for route in assets.read_json(ref)["routes"]:
                evidence.update(route[k] for k in ("input_ref", "observations_ref", "evaluation_ref") if route.get(k))
        science_inputs.validate_product(conn, task, body, evidence)
    if inputs["output_kind"] == "science_assessment":
        findings = body.get("findings")
        checked(body.get("evidence_refs") == inputs["evidence_refs"] and isinstance(findings,list)
                and len(findings)==len(routes) and all(isinstance(f,dict) and isinstance(f.get("reason"),str)
                and f["reason"].strip() and "supported" in f for f in findings)
                and {f.get("path_id") for f in findings}==set(routes), "cover every route and cite its evidence")
        outcomes = {r["path_id"]:r for r in assets.read_json(inputs["evidence_refs"][0])["routes"]}
        for f in findings:
            checked(f.get("evaluation_ref") == outcomes[f["path_id"]].get("evaluation_ref"),
                    "cite the frozen evaluator output for this path")
            route = outcomes[f["path_id"]]
            if inputs.get("p0"):
                if route["outcome_kind"] == "valid_scientific_outcome":
                    summary = assets.read_json(route["evaluation_ref"])
                    reaches = summary["count_at_or_above"] > 0
                    qualifications = f.get("qualifying_source_ids", [])
                    caveats = {s["source_id"] for s in body.get("source_reviews", [])
                               if s["disposition"] in ("accepted", "pending")}
                    uncertain = (f["supported"] is None and isinstance(qualifications, list) and qualifications
                                 and all(isinstance(k, str) and k in caveats for k in qualifications))
                    checked(summary["input_ref"] == route["observations_ref"] and
                            summary["threshold"] == proposal_body["threshold"] and
                            (uncertain or f["supported"] is (reaches == routes[f["path_id"]]["expected_reaches_threshold"])),
                            "review judgment contradicts the frozen result or cites a different measurement")
                else:
                    checked(f["supported"] is None, "an invalid measurement cannot support or refute")
                continue
            checked(type(f["supported"]) is bool if route["status"] == "SUCCEEDED" else f["supported"] is None,
                    "distinguish negative evidence from a failed measurement")
    else:
        selected, grids = body.get("continue_paths"), body.get("next_grids", {})
        pivots = body.get("pivots", []) if inputs.get("p0") else []
        checked(isinstance(pivots, list) and all(isinstance(p, dict) for p in pivots), "invalid pivot list")
        checked(body.get("assessment_ref") == inputs["assessment_ref"] and isinstance(body.get("reason"),str)
                and body["reason"].strip() and isinstance(selected,list) and all(isinstance(v,str) for v in selected)
                and len(set(selected))==len(selected) and set(selected)<=routes.keys()
                and isinstance(grids,dict) and set(grids)==set(selected)
                and (selected or pivots or isinstance(body.get("stop_reason"),str) and body["stop_reason"].strip()),
                "cite the assessment and justify a next grid or a stop")
        if inputs.get("p0"):
            seen = set(routes)
            used = set(selected)
            for pivot in pivots:
                source, name = pivot.get("from_path"), pivot.get("id")
                checked(isinstance(source, str) and source in routes and source not in used and
                        isinstance(name, str) and name.strip() and name not in seen and
                        isinstance(pivot.get("prediction"), str) and pivot["prediction"].strip()
                        and pivot["prediction"] != routes[source]["prediction"]
                        and type(pivot.get("expected_reaches_threshold", True)) is bool,
                        "a pivot needs a distinct source, new route and a new testable prediction")
                _validate_grid(pivot.get("grid"))
                checked(not conn.execute("SELECT 1 FROM science_settlements WHERE run_id=? AND path_id=?",
                                         (task["run_id"], name)).fetchone(), "pivot route already exists")
                seen.add(name)
                used.add(source)
        for name, grid in grids.items():
            _validate_grid(grid)
            checked(grid["candidates"] != assets.read_json(routes[name]["input_ref"])["candidates"],
                    "the next grid must test new candidates")
