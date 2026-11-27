"""Versioned geometry product and input contracts. Saved v1 runs keep their original definition and validators."""
from gx_core.artifacts import ArtifactService
from gx_core.orchestration import build
from gx_core.orchestration.engine import NodeResult, module_sources
from gx_core.persistence import store
from gx_core.p0 import science_inputs
from workflows.tl1a_geometry import domain, nodes as original
from . import briefs


def normalize_review(plan, body):
    limitations = body.get("limitations")
    if isinstance(limitations, list):
        domain.require(limitations and all(domain.text(item) for item in limitations),
                       "limitations must be one nonblank text or a nonempty list of nonblank texts")
        # The original submission preserves each item. This view feeds the v1 scientific predicate.
        limitations = "\n".join(limitations)
    domain.require(domain.text(limitations), "limitations must be one nonblank text or a nonempty list of nonblank texts")
    findings = body.get("findings")
    domain.require(isinstance(findings, dict), "findings must be keyed by the assigned hypothesis IDs")
    aliases = {}
    for h in plan["hypotheses"]:
        for key in (h["id"], h["hypothesis_id"]):
            aliases[key] = h["id"] if key not in aliases or aliases[key] == h["id"] else None
    normalized = {}
    for key, finding in findings.items():
        short = aliases.get(key)
        domain.require(short and short not in normalized, "findings contain an unknown, ambiguous, or duplicate hypothesis ID")
        normalized[short] = finding
    domain.require(set(normalized) == {h["id"] for h in plan["hypotheses"]}, "cover each assigned hypothesis once")
    return {**body, "findings": normalized, "limitations": limitations}


def validate(conn, task, bundle):
    inputs = task["input"]
    if inputs["output_kind"] != "geometry_assessment":
        return original.validate(conn, task, bundle)
    assets = ArtifactService(conn)
    refs = [ref for ref in task["submission_artifact_refs"] if
            (d := assets.describe(ref))["kind"] == "geometry_assessment" and d["task_id"] == task["task_id"]]
    domain.require(len(refs) == 1, "submit one assessment under the assigned task")
    author = conn.execute("SELECT principal FROM leases WHERE lease_id=?", (task["lease_id"],)).fetchone()
    main = conn.execute("SELECT thread_id FROM p0_intent_main WHERE run_id=?", (task["run_id"],)).fetchone()
    plan = assets.read_json(inputs["proposal_ref"])
    domain.require(main and author and author["principal"] and author["principal"] not in (main[0], plan["author_principal"]),
                   "commission a reviewer independent of the operator and proposal author")
    domain.require(domain.matches_intent(conn, task["run_id"], inputs), "current scientific requirements changed")
    submitted = assets.read_json(refs[0])
    domain.require(isinstance(submitted, dict) and submitted.get("proposal_ref") == inputs["proposal_ref"]
                   and submitted.get("evidence_ref") == inputs["evidence_ref"], "cite this round's immutable proposal and evidence")
    view = normalize_review(plan, submitted)
    evidence = assets.read_json(inputs["evidence_ref"])
    original._validate_review(conn, inputs, author["principal"], view, plan, evidence)
    cited = [inputs[k] for k in ("structure_ref", "quality_summary_ref", "proposal_ref", "evidence_ref", "criteria_ref")]
    cited.extend(r[k] for r in evidence["routes"] for k in ("observations_ref", "rescored_ref", "check_ref") if r.get(k))
    science_inputs.validate_product(conn, task, {**submitted, "findings": list(view["findings"].values())},
                                   [*cited, *view["checks"].values()])


def quality(ctx):
    result = original.quality(ctx)
    if not result.outputs:
        return result
    outputs = dict(result.outputs)
    summary = ArtifactService(ctx.conn).read_json(outputs["quality_summary_ref"])
    summary["inventory_summary_ref"] = outputs["quality_summary_ref"]
    summary["source_guide"] = briefs.source_guide(ctx.conn, outputs["structure_ref"])
    outputs["quality_summary_ref"] = domain.publish(ctx.conn, ctx.run_id, "geometry_quality_summary", summary)
    return NodeResult(outputs=outputs)


def proposal(ctx):
    with store.transaction(ctx.conn):
        return briefs.add_to_new_tasks(ctx, original.proposal(ctx))


def settle_review(ctx, result):
    inputs = result["input"]
    assets = ArtifactService(ctx.conn)
    original_ref = result["artifact_ref"]
    raw = result["body"]
    view = normalize_review(assets.read_json(inputs["proposal_ref"]), raw)
    if view["findings"] == raw["findings"]:
        domain.settle(ctx.conn, ctx.run_id, ctx.round, result, inputs)
        return
    # The evidence owner consumes short route names. Preserve the author's submitted artifact,
    # its reference on the reviewed finding, and a link to the exact normalization used in settlement.
    normalized_ref = domain.publish(ctx.conn, ctx.run_id, "geometry_review_mapping",
        {**raw, "findings": view["findings"], "original_review_ref": original_ref,
         "reviewer_submission_id": result["submission_id"]})
    domain.settle(ctx.conn, ctx.run_id, ctx.round, {**result, "artifact_ref": normalized_ref}, inputs)
    ctx.conn.execute("UPDATE evidence SET source_locator_json=json_set(source_locator_json,'$.review_ref',?,"
                     "'$.normalized_findings_ref',?) WHERE json_extract(source_locator_json,'$.review_ref')=? "
                     "AND evidence_id IN (SELECT evidence_id FROM science_settlements WHERE run_id=? AND submission_id=?)",
                     (original_ref, normalized_ref, normalized_ref, ctx.run_id, result["submission_id"]))


def assessment(ctx):
    with store.transaction(ctx.conn):
        inputs = original.research_inputs(ctx)
        result = original.accepted(ctx.conn, ctx.run_id, "assessment", ctx.round, evidence=inputs["evidence_ref"])
        if result:
            settle_review(ctx, result)
            return NodeResult(outputs={k: result[k] for k in ("artifact_ref", "task_id", "submission_id")})
        task = original.assign(ctx.conn, ctx.run_id, "assessment", ctx.round, inputs)
        return briefs.add_to_new_tasks(ctx, original.waiting_task(ctx, task))


def decision(ctx):
    with store.transaction(ctx.conn):
        # A supplement can commission a newer reviewer inside this decision stage. Settle that
        # submission using its scientific IDs before the shared decision node consumes the ledger.
        inputs = original.research_inputs(ctx)
        review = original.accepted(ctx.conn, ctx.run_id, "assessment", ctx.round, evidence=inputs["evidence_ref"], current=True)
        if review and not ctx.conn.execute("SELECT 1 FROM science_settlements WHERE submission_id=?",
                                           (review["submission_id"],)).fetchone():
            settle_review(ctx, review)
        return briefs.add_to_new_tasks(ctx, original.decision(ctx))


def register_all(registry):
    baseline = registry.get("tl1a_geometry", "1")
    graph = {**baseline.document, "version": "2"}
    bindings = {stage: node.fn for stage, node in baseline.nodes.items()}
    bindings.update(quality=quality, proposal=proposal, assessment=assessment, decision=decision)
    flow = build(graph, bindings, stage_rules=baseline.stage_rules, completion_evidence=baseline.completion_evidence,
                 task_product_validator=validate)
    flow.sources.update(baseline.sources)
    flow.sources.update(module_sources([briefs.schema_for, science_inputs.validate_product]))
    return [registry.register(flow)]
