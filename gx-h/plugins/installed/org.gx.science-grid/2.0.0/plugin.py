"""Research entry and scoped measurement reads for the original science_grid@2 graph."""


def research_start(spec):
    proposal = spec.get("proposal")
    if not isinstance(proposal, dict) or "threshold" not in proposal:
        raise ValueError("the grid proposal requires its prospective threshold")
    return {
        "inputs": {"proposal": proposal},
        "files": [],
        "items": [
            {"item_id": "evaluation", "item_type": "evaluation_spec", "expected_version": 0,
             "value": {"threshold": proposal["threshold"]}},
            {"item_id": "methods", "item_type": "method_ban", "expected_version": 0,
             "value": {"forbidden_capabilities": []}},
        ],
        "roles": {"objective": "context", "evaluation": "evaluation"},
    }


def build_task_context(ctx, task_id, attempt):
    context = ctx.tasks.read_context(task_id)
    inputs = ctx.tasks.inputs()
    if inputs.get("output_kind") in ("science_assessment", "science_decision"):
        assigned = {"proposal_ref": inputs["proposal_ref"], "evidence_refs": inputs["evidence_refs"]}
        if inputs.get("assessment_ref"):
            assigned["assessment_ref"] = inputs["assessment_ref"]
        context["grid_measurements"] = assigned
    return context


def grid_measurement(ctx, arguments):
    if set(arguments) != {"path_id"} or not isinstance(arguments["path_id"], str) or not arguments["path_id"]:
        raise ValueError("supply one path_id from the assigned grid proposal")
    path_id = arguments["path_id"]
    inputs = ctx.tasks.inputs()
    if inputs.get("output_kind") not in ("science_assessment", "science_decision"):
        raise ValueError("the current task has no assigned grid measurements")
    proposal_ref = inputs["proposal_ref"]
    proposal = ctx.artifacts.read(proposal_ref)
    path = next((candidate for candidate in proposal["paths"] if candidate["id"] == path_id), None)
    if path is None or proposal.get("method") != "fixture-grid@1":
        raise ValueError("the route is absent from the assigned fixture-grid proposal")

    matches = []
    for ref in dict.fromkeys(inputs["evidence_refs"]):
        evidence = ctx.artifacts.read(ref)
        if evidence.get("proposal_ref") == proposal_ref:
            matches.extend((ref, route) for route in evidence["routes"] if route["path_id"] == path_id)
    if len(matches) != 1:
        raise ValueError("the assignment must cite one measurement for this route and proposal")
    evidence_ref, route = matches[0]
    if path["input_ref"] != route["input_ref"]:
        raise ValueError("the route cites a different input from its frozen proposal")

    metrics = None
    evaluation_ref = route.get("evaluation_ref")
    if evaluation_ref and route.get("outcome_kind") in (None, "valid_scientific_outcome"):
        evaluation = ctx.artifacts.read(evaluation_ref)
        if evaluation["input_ref"] != route["observations_ref"] or evaluation["threshold"] != proposal["threshold"]:
            raise ValueError("the frozen evaluation does not match the route observations and cutoff")
        metrics = {key: evaluation[key] for key in ("observations", "max_score", "count_at_or_above")}
    return {
        "path_id": path_id, "prediction": path["prediction"],
        "expected_reaches_threshold": path.get("expected_reaches_threshold"), "threshold": proposal["threshold"],
        "proposal_ref": proposal_ref, "evidence_ref": evidence_ref, "input_ref": route["input_ref"],
        "job_id": route["job_id"], "status": route["status"], "outcome_kind": route.get("outcome_kind"),
        "observations_ref": route.get("observations_ref"), "evaluation_ref": evaluation_ref,
        "metrics": metrics, "failure": route.get("failure"), "evaluation_error": route.get("evaluation_error"),
    }


def register(ctx):
    ctx.workflows.register(workflow_id="science_grid", version="2",
                           hooks={"research_start": research_start, "build_task_context": build_task_context})
    ctx.tools.register(
        name="gx_grid_measurement",
        description="Read a route's frozen prediction, measured cutoff result and original evidence references.",
        schema={"type": "object", "properties": {"path_id": {"type": "string", "minLength": 1}},
                "required": ["path_id"], "additionalProperties": False},
        handler=grid_measurement,
    )
    ctx.capabilities.add_manifest("fixture-grid@1")
