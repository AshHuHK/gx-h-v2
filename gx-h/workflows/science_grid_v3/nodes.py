"""Freeze claims, evaluate measurements and commission scientific judgment where needed."""
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import GxError
from gx_core.orchestration import InterruptRequest, NodeResult, expansion, stop
from gx_core.orchestration.dispatch_preparation import DispatchPreparation
from gx_core.p0 import intent, science, science_inputs, science_refresh, science_refresh_v2
from gx_core.persistence import store
from gx_core.teams.service import backend_ctx
from workflows.compat_v20 import grid_dispatch
from workflows.science_grid import nodes as original
from workflows.science_grid_v2 import nodes as prior
from . import contract, products, settlement


def proposal(ctx):
    requested = ctx.state.get("next_proposal") or ctx.inputs["proposal"]
    routes = requested["paths"]
    original.checked(ctx.inputs.get("p0") is True, "Grid@3 requires the research entry with a frozen evaluator and Job manifests.")
    original.checked(isinstance(routes, list) and 0 < len(routes) <= contract.MAX_PATHS,
                     f"A frozen grid assessment accepts 1–{contract.MAX_PATHS} paths.")
    original.checked(type(requested.get("requires_interpretation", False)) is bool,
                     "requires_interpretation must be boolean")
    original.checked(all(isinstance(p, dict) and isinstance(p.get("id"), str) and p["id"]
                         and isinstance(p.get("prediction"), str) and p["prediction"].strip() for p in routes)
                     and len({p["id"] for p in routes}) == len(routes),
                     "Each path needs a unique identifier and a testable prediction.")
    if science_refresh.removed_requirements(ctx.conn, ctx.run_id):
        return stop("Supply a new research proposal for the changed objective or criterion.", rule="required_intent_removed")
    main = ctx.conn.execute("SELECT thread_id FROM p0_intent_main WHERE run_id=?", (ctx.run_id,)).fetchone()
    original.checked(main, "The grid proposal requires the project's authorized coordinator.")
    author = main["thread_id"]
    previous = requested.get("previous_decision_ref")
    if previous:
        row = ctx.conn.execute("SELECT l.principal FROM science_decisions d JOIN submissions s USING(submission_id) "
                               "JOIN leases l USING(lease_id) WHERE d.run_id=? AND d.decision_ref=?",
                               (ctx.run_id, previous)).fetchone()
        original.checked(row, "A revised proposal needs an accepted research decision.")
        author = row["principal"]
    evaluator, item, threshold = science.prospective_spec(ctx.conn, ctx.run_id, ctx.inputs, requested["threshold"])
    objective = science_refresh.objective_item(ctx.conn, ctx.run_id, ctx.inputs.get("objective_item_id"))
    body = {"run_id": ctx.run_id, "round": ctx.round, "objective": objective["value"] if objective else ctx.inputs["objective"],
            "method": "fixture-grid@1", "threshold": threshold, "evaluator": evaluator, "evaluation_item": item,
            "author_principal": author, "previous_decision_ref": previous,
            "requires_interpretation": requested.get("requires_interpretation", False), "paths": []}
    if objective:
        body["objective_item"] = {"item_id": objective["item_id"], "version": objective["version"]}
    for p in routes:
        scope = p.get("claim_scope", "interpretation")
        original.checked(scope in ("finite_grid_threshold", "interpretation"),
                         "claim_scope must name finite_grid_threshold or interpretation")
        original._validate_grid(p["grid"])
        body["paths"].append({"id": p["id"], "prediction": p["prediction"], "claim_scope": scope,
            "input_ref": original.publish(ctx, "grid_input", p["grid"]),
            "expected_reaches_threshold": p.get("expected_reaches_threshold", True),
            "hypothesis_key": p.get("hypothesis_id", p["id"]),
            "hypothesis_id": science.register_hypothesis(ctx.conn, ctx.run_id, p, author)})
    return NodeResult(outputs={"proposal_ref": original.publish(ctx, "research_proposal", body)})


def assessment_rule(conn, run, round_index):
    pending = conn.execute("SELECT 1 FROM tasks WHERE run_id=? AND stage='assessment' "
                           "AND json_extract(input_json,'$.round')=? AND status IN "
                           "('PENDING','READY','CLAIMED','RUNNING','SUBMITTED','VALIDATING','BLOCKED')",
                           (run, round_index)).fetchone()
    if pending:
        return {"complete": False}
    accepted = prior.assessment_rule(conn, run, round_index)
    if accepted["complete"]:
        return accepted
    computed = conn.execute("SELECT 1 FROM artifacts WHERE run_id=? AND kind='science_assessment' AND namespace=?",
                            (run, f"{run}/science/automatic/r{round_index}")).fetchone()
    return {"complete": bool(computed)}


def _waiting(stage, task_id, reason):
    return NodeResult(interrupt=InterruptRequest(kind="stage", reason=reason, tasks=(task_id,),
                      payload={"stage": stage, "waiting_state": "WAITING_CLIENT"}))


def _commission(conn, run, stage, inputs, *, previous=None, teams=None):
    kind = inputs["output_kind"]
    role = "evidence_reviewer" if kind == "science_assessment" else "planner"
    selector = inputs["product_contract"]["digest"]
    key = expansion.round_key(run, stage, inputs["round"], kind, selector)
    with store.transaction(conn):
        expanded = expansion.replay(conn, key)
        if expanded is None:
            spec = {"kind": "research_round", "role": role, "stage": stage, "bundle_kind": None, "input": inputs,
                    "required_artifacts": [kind], "acceptance": {"validate_workflow_product": True},
                    "depends_on": [previous] if previous else []}
            expanded = expansion.expand(backend_ctx(conn), run, rule=kind, key=key, stage=stage,
                                        round_index=inputs["round"], trigger={"evidence_refs": inputs["evidence_refs"]},
                                        tasks=[spec], teams=teams)
        task_id = expanded["task_ids"][0]
        body = ArtifactService(conn).read_json(inputs["proposal_ref"])
        science_refresh_v2.bind_task(conn, run, task_id, body, "assessment" if role == "evidence_reviewer" else "decision")
    return task_id


def _review(conn, run, stage, inputs, *, teams=None):
    scoped, rules = products.for_review(conn, run, inputs)
    basis = {"proposal_ref": scoped["proposal_ref"], "evidence_ref": scoped["evidence_refs"][0]}
    for candidate in science_inputs.accepted_products(conn, run, "review", inputs["round"]):
        if candidate["body"].get("contract_digest") == rules.digest:
            result = {key: candidate[key] for key in ("artifact_ref", "task_id", "submission_id")}
            settlement.settle(conn, run, inputs["round"], scoped, result)
            return {**result, **basis, "assessment_mode": "independent_review"}
    if rules.requires_review:
        task_id = _commission(conn, run, stage, scoped, teams=teams)
        return _waiting(stage, task_id, "Independent scientific interpretation is required.")
    with store.transaction(conn):
        body = rules.evaluate()
        ref = science._publish(conn, run, f"automatic/r{inputs['round']}", "science_assessment", body)
        result = {"artifact_ref": ref, "task_id": None, "submission_id": None}
        settlement.settle(conn, run, inputs["round"], scoped, result)
    return {**result, **basis, "assessment_mode": "deterministic"}


def _experiment_inputs(ctx):
    return {"round": ctx.round, "max_rounds": ctx.inputs["max_rounds"],
            "proposal_ref": ctx.outputs["proposal"]["proposal_ref"], "evidence_refs": [ctx.outputs["experiment"]["evidence_ref"]]}


def assessment(ctx):
    inputs = _experiment_inputs(ctx)
    # Admission may have rebound the worker to a later, rescored criterion.
    latest = next(science_inputs.accepted_products(ctx.conn, ctx.run_id, "review", ctx.round), None)
    if latest:
        inputs = latest["input"]
    result = _review(ctx.conn, ctx.run_id, ctx.node, inputs, teams=ctx.teams)
    return result if isinstance(result, NodeResult) else NodeResult(outputs=result)


def prepare_dispatch(conn, run, task):
    inputs = store.loads(task, "input_json")
    kind = inputs.get("output_kind")
    if kind not in ("science_assessment", "science_decision"):
        return True
    basis = grid_dispatch._basis(conn, run, task, inputs["round"])

    def prepare():
        proposal = ArtifactService(conn).read_json(inputs["proposal_ref"])
        if science_refresh.removed_requirements(conn, run):
            return grid_dispatch._Pause("Supply a replacement for the removed research objective or criterion.")
        if pin := proposal.get("objective_item"):
            current = intent.read_item(conn, run, pin["item_id"])
            if current["status"] != "active" or current["version"] != pin["version"]:
                return grid_dispatch._Pause("The changed objective requires a new research proposal.",
                                            {"historical_proposal_ref": inputs["proposal_ref"], "objective_item": pin})
        updated = inputs
        if pin := proposal.get("evaluation_item"):
            current = intent.read_item(conn, run, pin["item_id"])
            if current["status"] != "active":
                return None
            if current["version"] != pin["version"]:
                updated = grid_dispatch._revise(conn, run, inputs, proposal, current)
        if kind == "science_assessment":
            return products.for_review(conn, run, updated)[0]
        # Recheck the current read-set inside the same writer as a replacement assessment or dependency.
        with store.transaction(conn):
            fresh = conn.execute("SELECT * FROM tasks WHERE task_id=? AND status='READY'", (task["task_id"],)).fetchone()
            if not fresh or grid_dispatch._basis(conn, run, fresh, inputs["round"]) != basis:
                return None
            assessment = _review(conn, run, task["stage"], updated)
            if isinstance(assessment, NodeResult):
                reviewer = assessment.interrupt.tasks[0]
                conn.execute("INSERT OR IGNORE INTO task_deps(task_id,depends_on) VALUES(?,?)", (task["task_id"], reviewer))
                return None
            return products.for_decision(conn, run, updated, assessment["artifact_ref"])

    def apply(updated):
        if isinstance(updated, grid_dispatch._Pause):
            store.transition(conn, machine="run", subject_id=run, to_state="PAUSED", actor="backend",
                             reason=updated.reason, payload=updated.evidence)
            return False
        if updated is None:
            return False
        if updated != inputs:
            conn.execute("UPDATE tasks SET input_json=?,brief_ref=NULL,brief_sha256=NULL WHERE task_id=? AND status='READY'",
                         (ids.canonical_json(updated), task["task_id"]))
        return True
    return DispatchPreparation(basis, prepare, apply)


def _worker(conn, task):
    lease = conn.execute("SELECT principal FROM leases WHERE lease_id=?", (task["lease_id"],)).fetchone()
    main = conn.execute("SELECT thread_id FROM p0_intent_main WHERE run_id=?", (task["run_id"],)).fetchone()
    original.checked(main and lease and lease["principal"] and lease["principal"] != main["thread_id"],
                     "The assigned scientific worker must be independent of the project coordinator.")
    return lease["principal"]


def _judgment(conn, task, body, proposal):
    inputs, run = task["input"], task["run_id"]
    principal = _worker(conn, task)
    # Validate the operator's current requirements and exact cited evidence, including mid-run updates.
    read = ArtifactService(conn).read_json
    allowed = set(inputs["evidence_refs"])
    for ref in inputs["evidence_refs"]:
        for route in read(ref)["routes"]:
            allowed.update(route[k] for k in ("input_ref", "observations_ref", "evaluation_ref") if route.get(k))
    science_inputs.validate_product(conn, task, body, allowed)
    if inputs["output_kind"] == "science_assessment":
        original.checked(principal != proposal["author_principal"], "The proposal author cannot be its reviewer.")
        rules = products.frozen(conn, run, inputs)
        original.checked(body.get("assessment_mode") == "independent_review" and
                         rules.digest == inputs["product_contract"]["digest"], "The review has the wrong authority or task binding.")
        try:
            rules.validate_expanded(body)
        except contract.SemanticError as exc:
            products.semantic(str(exc))
    else:
        assessment = settlement.assessment_owner(conn, run, inputs["assessment_ref"], inputs["round"])
        src = read(assessment["artifact_ref"])
        original.checked((src.get("proposal_ref"), src.get("evidence_refs")) == (inputs["proposal_ref"], inputs["evidence_refs"]),
                         "The decision cites an assessment of a different proposal or measurement.")
        if assessment["task_id"]:
            reviewer = science._reviewed_submission(conn, assessment["task_id"])
            original.checked(principal != reviewer["principal"], "The planner and independent reviewer must differ.")
        else:
            # Also protects direct gx_task_submit against an outdated machine-only assessment.
            review_inputs = science_inputs.with_context(conn, run, {**inputs, "output_kind": "science_assessment"})
            rules = products.frozen(conn, run, review_inputs)
            original.checked(not rules.requires_review, "The updated evidence requires independent scientific judgment.")
            try:
                rules.validate_expanded(src)
            except contract.SemanticError as exc:
                products.semantic(str(exc))
        _check_decision(conn, run, inputs, body, proposal)


def _check_decision(conn, run, inputs, body, proposal):
    routes = {p["id"]: p for p in proposal["paths"]}
    selected, grids, pivots = body.get("continue_paths"), body.get("next_grids", {}), body.get("pivots", [])
    original.checked(body.get("assessment_ref") == inputs["assessment_ref"] and isinstance(body.get("reason"), str)
        and body["reason"].strip() and isinstance(pivots, list) and all(isinstance(p, dict) for p in pivots)
        and isinstance(selected, list) and all(isinstance(k, str) for k in selected)
        and len(set(selected)) == len(selected) and set(selected) <= routes.keys()
        and isinstance(grids, dict) and set(grids) == set(selected) and
        (selected or pivots or isinstance(body.get("stop_reason"), str) and body["stop_reason"].strip()),
        "Cite the assessment and justify a next grid, pivot, or stop.")
    used, seen = set(selected), set(routes)
    for p in pivots:
        src, name = p.get("from_path"), p.get("id")
        original.checked(isinstance(src, str) and src in routes and src not in used and
            isinstance(name, str) and name.strip() and name not in seen and
            isinstance(p.get("prediction"), str) and p["prediction"].strip() and p["prediction"] != routes[src]["prediction"]
            and type(p.get("expected_reaches_threshold", True)) is bool and
            p.get("claim_scope", "interpretation") in ("finite_grid_threshold", "interpretation"),
            "A pivot requires an unused source, a new path ID and a new testable prediction.")
        original._validate_grid(p.get("grid"))
        original.checked(not conn.execute("SELECT 1 FROM science_settlements WHERE run_id=? AND path_id=?", (run, name)).fetchone(),
                         "The pivot route already exists.")
        used.add(src)
        seen.add(name)
    for name, grid in grids.items():
        original._validate_grid(grid)
        original.checked(grid["candidates"] != ArtifactService(conn).read_json(routes[name]["input_ref"])["candidates"],
                         "The next grid must test new candidates.")


def validate(conn, task, bundle):
    assets = ArtifactService(conn)
    inputs = task["input"]
    matches = [assets.describe(ref) for ref in task["submission_artifact_refs"]]
    matches = [row for row in matches if row["task_id"] == task["task_id"] and row["kind"] == inputs["output_kind"]]
    original.checked(len(matches) == 1, "Supply one product under the assigned task and kind.")
    body = assets.read_json(matches[0]["artifact_ref"])
    original.checked(isinstance(body, dict) and body.get("proposal_ref") == inputs["proposal_ref"], "Wrong proposal reference.")
    _judgment(conn, task, body, assets.read_json(inputs["proposal_ref"]))


def _effective_paths(ctx, proposal, evidence_ref, actions, paths, stop):
    routes = {r["path_id"]: r for r in ArtifactService(ctx.conn).read_json(evidence_ref)["routes"]}
    for action in actions:
        route = routes[action["path_id"]]
        if action["action"] not in ("continue", "pivot") or route["outcome_kind"] in ("infrastructure_failure", "measurement_failure"):
            continue
        try:
            science._current(ctx.conn, ctx.run_id, route, proposal["evaluation_item"], proposal.get("objective_item"))
        except GxError as exc:
            successor = action.get("successor", action["path_id"])
            paths = [p for p in paths if p["id"] != successor]
            action.update(action="prune", reason=str(exc))
            if not paths:
                stop = "no_current_evidence"
    return paths, stop


def decision(ctx):
    run, conn = ctx.run_id, ctx.conn
    latest = next(science_inputs.accepted_products(conn, run, "decision", ctx.round), None)
    if latest and not prior.decision_rule(conn, run, ctx.round)["complete"]:
        return NodeResult(interrupt=InterruptRequest(kind="stage", reason="Await a decision covering current sources.",
                         payload={"stage": ctx.node, "waiting_state": "WAITING_CLIENT"}))
    if latest is None:
        prior_assessment = ctx.outputs["assessment"]
        inputs = {**_experiment_inputs(ctx), "proposal_ref": prior_assessment["proposal_ref"],
                  "evidence_refs": [prior_assessment["evidence_ref"]]}
        scoped = products.for_decision(conn, run, inputs, prior_assessment["artifact_ref"])
        task_id = _commission(conn, run, "decision", scoped, previous=prior_assessment["task_id"], teams=ctx.teams)
        return _waiting("decision", task_id, "Choose the next investigation from assessed evidence.")
    inputs, body, ref = latest["input"], latest["body"], latest["artifact_ref"]
    proposal = ArtifactService(conn).read_json(inputs["proposal_ref"])
    owner = settlement.assessment_owner(conn, run, inputs["assessment_ref"], ctx.round)
    paths, actions = science.decision_routes(proposal, body)
    if paths and (ctx.round + 1 >= ctx.inputs["max_rounds"] or not ctx.rounds_left):
        paths, stop = [], "round_budget"
        actions = [{"action": "stop", "path_id": p["id"]} for p in proposal["paths"]]
    else:
        stop = None if paths else body["stop_reason"]
    with store.transaction(conn):
        paths, stop = _effective_paths(ctx, proposal, inputs["evidence_refs"][0], actions, paths, stop)
        science_inputs.save_decision(conn, run, ctx.round, latest["submission_id"], owner["submission_id"], ref,
                                     actions, body["reason"], stop)
    ctx.state.setdefault("history", []).append({"round": ctx.round, "proposal_ref": inputs["proposal_ref"],
        "evidence_ref": inputs["evidence_refs"][0], "assessment_ref": inputs["assessment_ref"],
        "assessment_mode": "independent_review" if owner["task_id"] else "deterministic",
        "artifact_ref": ref, "task_id": latest["task_id"], "submission_id": latest["submission_id"]})
    if not paths:
        return NodeResult(outputs={"stop_reason": stop})
    scopes = {p["id"]: p["claim_scope"] for p in proposal["paths"]}
    for p in paths:
        if p["id"] in scopes:
            p["claim_scope"] = scopes[p["id"]]
    ctx.state["next_proposal"] = {"threshold": proposal["threshold"], "paths": paths,
        "previous_decision_ref": ref, "requires_interpretation": proposal["requires_interpretation"]}
    return NodeResult(reenter="proposal", reason=body["reason"])
