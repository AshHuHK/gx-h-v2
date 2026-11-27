"""Scoped reads of measurements from the frozen grid@3 task. GX runs the graph and evaluator."""


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
    ctx.workflows.register(workflow_id="science_grid", version="3")
    ctx.tools.register(
        name="gx_grid_measurement",
        description="Read a route's frozen prediction, measured cutoff result and original evidence references.",
        schema={"type": "object", "properties": {"path_id": {"type": "string", "minLength": 1}},
                "required": ["path_id"], "additionalProperties": False},
        handler=grid_measurement,
    )
    ctx.capabilities.add_manifest("fixture-grid@1")
