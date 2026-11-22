"""GX stages for a prospective comparison of atom selections on deposited TL1A."""
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids, machines
from gx_core.errors import GxError
from gx_core.orchestration import expansion, service
from gx_core.orchestration.engine import InterruptRequest, NodeResult, pause, stop
from gx_core.p0 import intent, manifest, science, science_inputs
from gx_core.persistence import store
from gx_core.teams.service import backend_ctx

from . import domain, review_tools

KINDS = {"proposal": "geometry_plan", "assessment": "geometry_assessment", "decision": "geometry_decision"}
ROLES = {"proposal": ("planner", "author"), "assessment": ("evidence_reviewer", "review"), "decision": ("planner", "decision")}
BRIEFS = {
    "proposal": "Read the coordinate provenance and quality summary; inspect the original mmCIF via gx_read_artifact "
                "with offset as needed. Propose an independently authored prospective test confined to this model. "
                "Choose two or more funded, distinct protocols, explain their interpretability and missing data. "
                "Submit {source_ref,protocols,rationale,hypotheses:[{id,prediction,expected}],criteria}. Supply "
                "discriminable claims including expected='stable' and expected='sensitive'. An underpowered "
                "or poorly reproduced test may be insufficient for either. Define all of coverage, comparison "
                "and reproduction using the exact keys and units in criteria_template. The supplied numeric "
                "values are defaults unless formal_criteria is present; then use every user value exactly, even if it "
                "leads to insufficient coverage. Justify the actual frozen tolerances and residue coverage in rationale. "
                "Choose initial protocols and coverage that can distinguish the two predictions on this source. "
                "Any reusable_protocols are earlier observations; clearly distinguish retrospective numerical reuse from a new test. "
                "All subsequent rounds use the same pairs, mapping, denominator and prospective evaluation.",
    "assessment": "Independently review the original deposited coordinates, the plan, each Job observation and "
                  "the cross-protocol evaluation. Run gx_recalculate_geometry for every route with an observations_ref "
                  "and cite the returned check_ref. Evaluate pairwise RMSD and angle deltas and effective residue "
                  "coverage. Separate a negative prediction from insufficient data. Submit "
                  "{proposal_ref,evidence_ref,status,checks,findings,limitations}. status is the frozen "
                  "comparison outcome stable/sensitive/insufficient, or insufficient when an independent calculation fails; checks is a protocol -> check_ref object. "
                  "findings is an object keyed by each hypothesis ID, each value {supported,reason}, with "
                  "supported true, false or null (insufficient). If a reviewed supplement undermines interpretation, "
                  "you may withhold a conclusion with status=insufficient and a nonempty qualification explaining why. "
                  "Explain uncertainty and limit claims to this crystal.",
    "decision": "Read the accepted independent assessment, evidence and proposal. Decide whether enough has "
                "been learned or whether a specific new atom selection warrants a new Job. Submit "
                "{proposal_ref,evidence_ref,assessment_ref,reason,counterevidence,uncertainty,next_protocols,stop_reason}. "
                "Cite the actual assessment in assessment_ref. Select a subset of available_protocols within the "
                "remaining budget or [] to stop. Explain what the findings change and which question a new "
                "protocol would answer. A stop requires a nonempty stop_reason. Preserve adverse and insufficient outcomes."
}
require = domain.require


def state(conn, run):
    return store.loads(store.latest_checkpoint(conn, run), "state_json")


def quality_rule(conn, run, round_index):
    rows = conn.execute("SELECT status FROM jobs WHERE run_id=? AND capability_id='tl1a-coordinate-quality'", (run,))
    return {"complete": any(r["status"] in machines.terminal_states("job") for r in rows)}


def waiting_jobs(ctx, jobs):
    return NodeResult(interrupt=InterruptRequest(kind="stage", reason="Await durable coordinate Job receipts.",
        job_refs=tuple(jobs), payload={"stage": ctx.node, "waiting_state": "WAITING_EXTERNAL_JOB"}))


def quality(ctx):
    from gx_core.jobs.geometry_workflow import submit_quality, observe_quality
    structure_ref = domain.source_artifact(ctx.conn, ctx.run_id)
    job = submit_quality(ctx.conn, run_id=ctx.run_id, operation_id="structure-inventory", structure_ref=structure_ref,
                         binding={"workflow_id": ctx.workflow_id, "node_id": ctx.node})
    result = observe_quality(ctx.conn, job["job_id"])
    if result["status"] not in machines.terminal_states("job"):
        return waiting_jobs(ctx, [job["job_id"]])
    if result["status"] != "SUCCEEDED":
        return stop(f"Coordinate quality Job {job['job_id']} ended {result['status']}. No fit was evaluated.",
                    rule="missing_coordinate_inventory")
    project = ctx.conn.execute("SELECT project_ref FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()[0]
    child = service.run_start(backend_ctx(ctx.conn), {"objective": "Determine usable atom selections and their observed residue coverage.",
        "workflow": {"id": "tl1a_coordinate_quality", "version": "1"}, "project_ref": project, "budget_profile": "cpu_only",
        "inputs": {"parent_run_id": ctx.run_id, "structure_ref": structure_ref, "quality_job_id": job["job_id"],
                   "inventory_ref": result["observations_ref"]}, "idempotency_key": "coordinate-quality:" + ctx.run_id})
    child_id = child["run_id"]
    child_row = ctx.conn.execute("SELECT * FROM runs WHERE run_id=?", (child_id,)).fetchone()
    if child_row["status"] == "ACTIVE":
        from gx_core.orchestration import reactor
        reactor.drive_science(ctx.gx, child_id)
        child_row = ctx.conn.execute("SELECT * FROM runs WHERE run_id=?", (child_id,)).fetchone()
    require(child_row["status"] == "COMPLETED", "the versioned coordinate-quality workflow has no completed result")
    item = state(ctx.conn, child_id)["outputs"]["inventory"]
    summary = {**item["summary"], "provenance": domain.PROVENANCE}
    ref = domain.publish(ctx.conn, ctx.run_id, "geometry_quality_summary", summary)
    return NodeResult(outputs={"structure_ref": structure_ref, "quality_summary_ref": ref, "mask_ref": item["mask_ref"],
                               "quality_run_id": child_id, "quality_job_id": job["job_id"]})


def accepted(conn, run, stage, round_index, *, evidence=None, review=None, current=False):
    role = ROLES[stage][1]
    for product in science_inputs.accepted_products(conn, run, role, round_index):
        inputs, body = product["input"], product["body"]
        if inputs.get("output_kind") != KINDS[stage]:
            continue
        if stage == "proposal" and not domain.matches_intent(conn, run, inputs):
            continue
        if evidence and inputs.get("evidence_ref") != evidence or review and inputs.get("assessment_ref") != review:
            continue
        if current and (stage == "assessment" and not science_inputs.review_covers(conn, run, round_index, body)
                        or stage == "decision" and not science_inputs.review_covers(
                            conn, run, round_index, {"source_reviews": body.get("source_decisions", [])})):
            continue
        return product
    return None


def rule_for(conn, run, round_index, stage):
    # A successor round selected directly by the decision uses that accepted decision as its prospective plan.
    if stage == "proposal" and state(conn, run)["scratch"].get("next_plan"):
        return {"complete": True}
    return {"complete": bool(accepted(conn, run, stage, round_index, current=stage == "decision"))}


def proposal_rule(conn, run, round_index):
    return rule_for(conn, run, round_index, "proposal")


def assessment_rule(conn, run, round_index):
    return rule_for(conn, run, round_index, "assessment")


def decision_rule(conn, run, round_index):
    return rule_for(conn, run, round_index, "decision")


def assign(conn, run, stage, round_index, inputs, *, phase=None, depends=()):
    role, science_role = ROLES[stage]
    inputs = science_inputs.with_context(conn, run, {**inputs, "round": round_index, "output_kind": KINDS[stage],
                        "science_domain": "tl1a_geometry", "science_role": science_role, "instruction": BRIEFS[stage]})
    phase = phase or stage
    # Include exact sources and prior review for a later judgment in the same round.
    key = expansion.round_key(run, phase, round_index, KINDS[stage], ids.sha256_of_obj(
        [inputs.get(k) for k in ("objective_item", "evaluation_item", "structure_ref", "evidence_ref", "assessment_ref", "supplement_sources")]))
    existing = expansion.replay(conn, key)
    if existing:
        return existing["task_ids"][0]
    spec = {"stage": phase, "kind": "research_round", "role": role, "bundle_kind": None, "input": inputs,
            "required_artifacts": [KINDS[stage]], "acceptance": {"validate_workflow_product": True},
            "depends_on": list(depends)}
    with store.transaction(conn):
        expanded = expansion.expand(backend_ctx(conn), run, rule=KINDS[stage], key=key,
                 trigger={"source": inputs["structure_ref"]}, stage=phase, round_index=round_index, tasks=[spec])
        task_id = expanded["task_ids"][0]
        bindings = {"objective": "context", "coordinates": "context", "job-limit": "context"}
        if evaluation := inputs.get("evaluation_item"):
            bindings[evaluation["item_id"]] = "evaluation"
        intent.bind_dependencies(conn, run, "task", task_id, bindings)
        science_inputs.bind_task(conn, run, task_id)
    return task_id


def waiting_task(ctx, task_id):
    return NodeResult(interrupt=InterruptRequest(kind="stage", tasks=(task_id,), reason=BRIEFS[ctx.node],
                payload={"stage": ctx.node, "waiting_state": "WAITING_CLIENT"}))


def proposal(ctx):
    if plan := ctx.state.get("next_plan"):
        ref = domain.publish(ctx.conn, ctx.run_id, "geometry_proposal", plan)
        return NodeResult(outputs={"proposal_ref": ref})
    quality = ctx.outputs["quality"]
    summary = ArtifactService(ctx.conn).read_json(quality["quality_summary_ref"])
    reused = list(observed_jobs(ctx.conn, ctx.run_id, quality))
    prior_ref = ctx.state.get("prior_objective_proposal")
    inputs = {**quality, "protocol_coverage": summary["protocol_coverage"], "reusable_protocols": reused,
              "prior_objective_proposal_ref": prior_ref, **domain.active_inputs(ctx.conn, ctx.run_id),
              "available_jobs": domain.budget_left(ctx.conn, ctx.run_id), "max_rounds": ctx.inputs["max_rounds"],
              "protocol_descriptions": domain.TEMPLATE["protocols"], "mapping": domain.TEMPLATE["mapping"],
              "criteria_template": {key: domain.TEMPLATE[key] for key in domain.SECTIONS},
              "interpretation": domain.TEMPLATE["interpretation"]}
    if inputs["formal_criteria"] is not None:
        inputs["criteria_template"] = inputs["formal_criteria"]
    result = accepted(ctx.conn, ctx.run_id, ctx.node, ctx.round)
    if not result:
        if inputs["available_jobs"] + len(reused) < 2:
            return pause([{"kind": "budget", "key": "job_submissions"}],
                         reason="Two distinct coordinate selections require two remaining Job submissions.")
        return waiting_task(ctx, assign(ctx.conn, ctx.run_id, ctx.node, ctx.round, inputs))
    if not domain.matches_intent(ctx.conn, ctx.run_id, result["input"]):
        return pause([{"kind": "intent", "item_id": "objective"}], reason="A changed objective needs a new prospective plan.")
    plan = domain.freeze(ctx.conn, ctx.run_id, result, result["body"], result["input"])
    return NodeResult(outputs={"proposal_ref": domain.publish(ctx.conn, ctx.run_id, "geometry_proposal", plan)})


def operation(plan, protocol):
    # The same observed structure, mask and scientific criteria always reuse their original Job.
    return ids.sha256_of_obj([plan["structure_ref"], plan["mask_ref"], plan["evaluator"], protocol])


def observed_jobs(conn, run, plan):
    """Physical coordinates and atom mask define a reusable geometry measurement even after a criterion edit."""
    rows = conn.execute("SELECT j.job_id,m.admission_json FROM jobs j JOIN p0_execution_manifests m USING(job_id)"
                        " WHERE j.run_id=? AND j.status='SUCCEEDED' AND j.capability_id='tl1a-geometry'"
                        " AND j.capability_version='1' ORDER BY j.rowid", (run,))
    selected = {}
    for row in rows:
        admission = store.loads(row, "admission_json")
        files = {i["role"]: i["artifact_ref"] for i in admission["inputs"]}
        if files["structure"] == plan["structure_ref"] and files["selection"] == plan["mask_ref"]:
            selected.setdefault(admission["parameters"]["protocol"], row["job_id"])
    return selected


def experiment_rule(conn, run, round_index):
    plan_ref = state(conn, run)["outputs"]["proposal"]["proposal_ref"]
    plan = ArtifactService(conn).read_json(plan_ref)
    reused = observed_jobs(conn, run, plan)
    keys = {f"job:{run}:tl1a-geometry:{operation(plan, p)}" for p in plan["protocols"] if p not in reused}
    finished = {r["idempotency_key"] for r in conn.execute("SELECT status,idempotency_key FROM jobs WHERE run_id=?", (run,))
                if r["status"] in machines.terminal_states("job")}
    return {"complete": keys <= finished}


def experiment(ctx):
    from gx_core.jobs.geometry_workflow import submit_geometry, observe_geometry
    assets = ArtifactService(ctx.conn)
    ref = ctx.outputs["proposal"]["proposal_ref"]
    plan = assets.read_json(ref)
    if not domain.matches_intent(ctx.conn, ctx.run_id, plan):
        return pause([{"kind": "intent", "item_id": "objective"}], reason="Confirmed scientific requirements changed; replan before the next Job.")
    rows, pending = [], []
    reused = observed_jobs(ctx.conn, ctx.run_id, plan)
    for protocol in plan["protocols"]:
        job_id = reused.get(protocol)
        if not job_id:
            job = submit_geometry(ctx.conn, run_id=ctx.run_id, operation_id=operation(plan, protocol),
                                  structure_ref=plan["structure_ref"], protocol=protocol, criteria_ref=plan["criteria_ref"],
                                  quality_ref=plan["mask_ref"], binding={"workflow_id": ctx.workflow_id,
                                  "node_id": ctx.node, "evaluator": plan["evaluator"], "path_id": protocol})
            job_id = job["job_id"]
        job = observe_geometry(ctx.conn, job_id)
        row = {"protocol": protocol, "job_id": job["job_id"], "job_status": job["status"],
               "observations_ref": job.get("observations_ref"), "failure": job.get("failure")}
        if job["status"] not in machines.terminal_states("job"):
            pending.append(job["job_id"])
        rows.append(row)
    if pending:
        return waiting_jobs(ctx, pending)
    for row in rows:
        if row["job_status"] == "SUCCEEDED":
            try:
                row.update(domain.inspect_measurement(ctx.conn, ctx.run_id, plan, row))
            except (ValueError, OSError, GxError) as exc:
                row["measurement_error"] = str(exc)
    ref = domain.evaluate(ctx.conn, ctx.run_id, ctx.round, ref, rows)
    return NodeResult(outputs={"evidence_ref": ref})


def research_inputs(ctx):
    plan_ref = ctx.outputs["proposal"]["proposal_ref"]
    plan = ArtifactService(ctx.conn).read_json(plan_ref)
    return {"proposal_ref": plan_ref, "evidence_ref": ctx.outputs["experiment"]["evidence_ref"],
            **{k: plan[k] for k in ("objective_item", "source_item", "evaluation_item", "formal_criteria", "structure_ref", "criteria_ref", "quality_summary_ref")}}


def assessment(ctx):
    inputs = research_inputs(ctx)
    result = accepted(ctx.conn, ctx.run_id, ctx.node, ctx.round, evidence=inputs["evidence_ref"])
    if not result:
        return waiting_task(ctx, assign(ctx.conn, ctx.run_id, ctx.node, ctx.round, inputs))
    domain.settle(ctx.conn, ctx.run_id, ctx.round, result, result["input"])
    return NodeResult(outputs={k: result[k] for k in ("artifact_ref", "task_id", "submission_id")})


def decision(ctx):
    inputs = research_inputs(ctx)
    assets = ArtifactService(ctx.conn)
    plan = assets.read_json(inputs["proposal_ref"])
    prior = ctx.outputs["assessment"]
    remaining = domain.budget_left(ctx.conn, ctx.run_id)
    can_continue = ctx.round + 1 < ctx.inputs["max_rounds"] and ctx.rounds_left > 0 and remaining > 0
    choices = [p for p in domain.PROTOCOLS if p not in plan["protocols"]] if can_continue else []
    inputs.update(assessment_ref=prior["artifact_ref"], available_protocols=choices, available_jobs=remaining,
                  max_rounds=ctx.inputs["max_rounds"], protocols_tested=plan["protocols"])
    result = accepted(ctx.conn, ctx.run_id, ctx.node, ctx.round, evidence=inputs["evidence_ref"], current=True)
    if not result:
        return waiting_task(ctx, assign(ctx.conn, ctx.run_id, ctx.node, ctx.round, inputs, depends=[prior["task_id"]]))
    body = result["body"]
    review = accepted(ctx.conn, ctx.run_id, "assessment", ctx.round, evidence=inputs["evidence_ref"], current=True)
    require(review and review["artifact_ref"] == body["assessment_ref"], "the decision needs an up-to-date independent review")
    domain.settle(ctx.conn, ctx.run_id, ctx.round, review, review["input"])
    next_protocols = body["next_protocols"]
    stopped = body.get("stop_reason") if not next_protocols else None
    if next_protocols and (not set(next_protocols) <= set(choices) or len(next_protocols) > remaining):
        stopped, next_protocols = "The remaining research budget changed after hand-in.", []
    actions = [{"action": "investigate", "protocol": protocol} for protocol in next_protocols] or [{"action": "stop"}]
    with store.transaction(ctx.conn):
        science_inputs.save_decision(ctx.conn, ctx.run_id, ctx.round, result["submission_id"], review["submission_id"],
                                     result["artifact_ref"], actions, body["reason"], stopped)
    summary = {"round": ctx.round, "proposal_ref": inputs["proposal_ref"], "evidence_ref": inputs["evidence_ref"],
               "assessment_ref": review["artifact_ref"], "artifact_ref": result["artifact_ref"],
               "quality_run_id": ctx.outputs["quality"]["quality_run_id"]}
    ctx.state.setdefault("history", []).append(summary)
    if not next_protocols:
        return NodeResult(outputs={"stop_reason": stopped})
    author = science._reviewed_submission(ctx.conn, result["task_id"])["principal"]
    protocols = [*plan["protocols"], *next_protocols]
    with store.transaction(ctx.conn):
        hypotheses = domain.register_claims(ctx.conn, ctx.run_id, plan["hypotheses"], plan["criteria_ref"],
                            plan["structure_ref"], plan["objective_item"], protocols, author)
    ctx.state["next_plan"] = {**plan, "previous_proposal_ref": inputs["proposal_ref"], "hypotheses": hypotheses,
                              "previous_assessment_ref": review["artifact_ref"], "previous_decision_ref": result["artifact_ref"],
                              "author_principal": author, "protocols": protocols,
                              "rationale": body["reason"]}
    return NodeResult(reenter="proposal", reason=body["reason"])


def validate(conn, task, bundle):
    inputs = task["input"]
    assets = ArtifactService(conn)
    products = [ref for ref in task["submission_artifact_refs"] if
                (desc := assets.describe(ref))["kind"] == inputs["output_kind"] and desc["task_id"] == task["task_id"]]
    require(len(products) == 1, "submit one scientific product under the assigned GX task")
    worker = conn.execute("SELECT principal FROM leases WHERE lease_id=?", (task["lease_id"],)).fetchone()
    main = conn.execute("SELECT thread_id FROM p0_intent_main WHERE run_id=?", (task["run_id"],)).fetchone()
    require(main and worker and worker["principal"] and worker["principal"] != main[0],
            "commission an independent scientific worker")
    require(domain.matches_intent(conn, task["run_id"], inputs),
            "the project objective or source was replaced; a fresh proposal is required")
    body = assets.read_json(products[0])
    require(isinstance(body, dict), "deliver an authored JSON object")
    cited = [inputs["structure_ref"], inputs["quality_summary_ref"]]
    if inputs["output_kind"] == "geometry_plan":
        domain.validate_plan(inputs, body)
    else:
        require(body.get("proposal_ref") == inputs["proposal_ref"] and body.get("evidence_ref") == inputs["evidence_ref"],
                "cite this round's exact proposal and evidence")
        plan = assets.read_json(inputs["proposal_ref"])
        evidence = assets.read_json(inputs["evidence_ref"])
        cited.extend([inputs["evidence_ref"], inputs["proposal_ref"], inputs["criteria_ref"]])
        cited.extend(r[k] for r in evidence["routes"] for k in ("observations_ref", "check_ref") if r.get(k))
        if inputs["output_kind"] == "geometry_assessment":
            require(worker["principal"] != plan["author_principal"], "evidence requires its own independent reviewer")
            _validate_review(conn, inputs, worker["principal"], body, plan, evidence)
        else:
            review = accepted(conn, task["run_id"], "assessment", inputs["round"], evidence=inputs["evidence_ref"], current=True)
            require(review and review["artifact_ref"] == inputs["assessment_ref"] == body.get("assessment_ref"),
                    "cite the accepted independent assessment for this round and source set")
            reviewer = science._reviewed_submission(conn, review["task_id"])
            require(worker["principal"] != reviewer["principal"], "the decision and evidence review require separate workers")
            selected = body.get("next_protocols")
            require(isinstance(selected, list) and all(isinstance(p, str) for p in selected) and
                    len(selected) == len(set(selected)) <= inputs["available_jobs"]
                    and set(selected) <= set(inputs["available_protocols"]),
                    "select only distinct new and funded protocols, or stop")
            require(all(domain.text(body.get(k)) for k in ("reason", "counterevidence", "uncertainty")) and
                    (selected or domain.text(body.get("stop_reason"))), "explain the decision, counterevidence, uncertainty and stop")
            cited.append(inputs["assessment_ref"])
    if inputs["output_kind"] == "geometry_assessment":
        cited.extend(body["checks"].values())
    science_inputs.validate_product(conn, task, body, cited)


def _validate_review(conn, inputs, principal, body, plan, evidence):
    expected = {r["protocol"] for r in evidence["routes"] if r.get("observations_ref")}
    checks = body.get("checks")
    require(isinstance(checks, dict) and set(checks) == expected and all(isinstance(ref, str) for ref in checks.values()),
            "run and cite an independent recalculation for every protocol with original measurements")
    comparisons = review_tools.reviewed(conn, principal, inputs, checks)
    measured = (evidence["comparison"]["status"] if all(c["status"] == "matches" for c in comparisons)
                else "insufficient")
    status = body.get("status")
    withheld = (status == "insufficient" and domain.text(body.get("qualification"))
                and any(s.get("disposition") in ("accepted", "pending") for s in body.get("source_reviews", [])
                        if isinstance(s, dict)))
    require((status == measured or withheld) and domain.text(body.get("limitations")),
            "interpret the frozen comparison; explain any evidentiary qualification and limits")
    findings = body.get("findings")
    require(isinstance(findings, dict) and set(findings) == {h["id"] for h in plan["hypotheses"]},
            "assess each declared hypothesis")
    for hyp in plan["hypotheses"]:
        f = findings[hyp["id"]]
        expected = None if status == "insufficient" else status == hyp["expected"]
        require(isinstance(f, dict) and "supported" in f and f["supported"] is expected and domain.text(f.get("reason")),
                "each reviewed finding must give the correct direction or insufficiency with a reason")


def prepare_dispatch(conn, run, task):
    inputs = store.loads(task, "input_json")
    updated = science_inputs.with_context(conn, run, inputs)
    pins = domain.active_inputs(conn, run)
    if not domain.matches_intent(conn, run, inputs):
        if inputs["output_kind"] == "geometry_plan":
            updated.update(pins)
            if pins["formal_criteria"]:
                updated["criteria_template"] = pins["formal_criteria"]
        else:
            if conn.execute("SELECT status FROM runs WHERE run_id=?", (run,)).fetchone()[0] != "PAUSED":
                store.transition(conn, machine="run", subject_id=run, to_state="PAUSED", actor="backend",
                                 reason="The confirmed change requires a new geometric plan for the current objective.")
            return False
    if inputs["output_kind"] == "geometry_decision":
        review = accepted(conn, run, "assessment", inputs["round"], evidence=inputs["evidence_ref"], current=True)
        if not review:
            # The earlier review remains valid for its original source set. Use its evidence and commission a
            # new review in the existing decision stage; no scientific Job is launched for a supplement.
            prior = accepted(conn, run, "assessment", inputs["round"], evidence=inputs["evidence_ref"])
            task_id = assign(conn, run, "assessment", inputs["round"], prior["input"], phase=task["stage"])
            conn.execute("INSERT OR IGNORE INTO task_deps(task_id,depends_on) VALUES(?,?)", (task["task_id"], task_id))
            return False
        updated["assessment_ref"] = review["artifact_ref"]
    if updated != inputs:
        conn.execute("UPDATE tasks SET input_json=?,brief_ref=NULL,brief_sha256=NULL WHERE task_id=? AND status='READY'",
                     (ids.canonical_json(updated), task["task_id"]))
    return True


def report(ctx):
    history = ctx.state["history"]
    latest = history[-1]
    assets = ArtifactService(ctx.conn)
    decision = assets.read_json(latest["artifact_ref"])
    body = {"run_id": ctx.run_id, "objective": assets.read_json(latest["proposal_ref"])["objective"],
            "source": domain.PROVENANCE, "coordinate_quality": ctx.outputs["quality"], "rounds": history,
            "conclusion": decision["reason"], "counterevidence": decision["counterevidence"],
            "uncertainty": decision["uncertainty"], "stop_reason": ctx.outputs["decision"]["stop_reason"],
            "scope": domain.LIMITS}
    return NodeResult(outputs={"artifact_ref": domain.publish(ctx.conn, ctx.run_id, "geometry_report", body)})


def completed(conn, run):
    assets = ArtifactService(conn)
    doc = assets.read_json(state(conn, run["run_id"])["outputs"]["report"]["artifact_ref"])
    require(doc["run_id"] == run["run_id"] and doc["rounds"] and doc["stop_reason"], "a final scientific report is required")
    for item in doc["rounds"]:
        official = conn.execute("SELECT decision_ref FROM science_decisions WHERE run_id=? AND round_index=?",
                                (run["run_id"], item["round"])).fetchone()
        require(official and official[0] == item["artifact_ref"], "report needs its recorded decision")
        for key in ("proposal_ref", "evidence_ref", "assessment_ref", "artifact_ref"):
            assets.read_json(item[key])
    return True, []
