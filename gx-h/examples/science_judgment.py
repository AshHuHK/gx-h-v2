"""Deterministic judgment for the synthetic grid fixture's two distinct worker roles."""


def judge(artifacts, task_input):
    proposal_ref = task_input["proposal_ref"]
    proposal = artifacts.read_json(proposal_ref)
    refs = task_input["evidence_refs"]
    routes = [r for ref in refs for r in artifacts.read_json(ref)["routes"]]
    threshold = proposal["threshold"]
    observations = {
        route["path_id"]: artifacts.read_json(route["observations_ref"])["observations"]
        for route in routes if route["status"] == "SUCCEEDED"
        and route.get("outcome_kind", "valid_scientific_outcome") == "valid_scientific_outcome"
    }
    if task_input["output_kind"] == "science_assessment":
        findings = []
        for route in routes:
            path = route["path_id"]
            finding = {"path_id": path, "evaluation_ref": route.get("evaluation_ref")}
            if path in observations:
                if ref := finding["evaluation_ref"]:
                    summary = artifacts.read_json(ref)
                    if summary["input_ref"] != route["observations_ref"] or summary["threshold"] != threshold:
                        raise ValueError("the evaluation must reference this route's observations and cutoff")
                    best = summary["max_score"]
                    meets = summary["count_at_or_above"] > 0
                else:
                    best = max(point["score"] for point in observations[path])
                    meets = best >= threshold
                comparison = "reached" if meets else "fell below"
                expectation = next(p for p in proposal["paths"] if p["id"] == path).get("expected_reaches_threshold", True)
                finding.update(supported=meets == expectation,
                               reason=f"Best measured score {best:g} {comparison} the threshold {threshold:g}.")
            else:
                finding.update(supported=None, reason=f"Job {route['job_id']} ended {route['status']} "
                               "without a usable measurement.")
            findings.append(finding)
        return {"proposal_ref": proposal_ref, "evidence_refs": refs, "findings": findings}

    if task_input["output_kind"] != "science_decision":
        raise ValueError(f"Unexpected grid judgment kind {task_input['output_kind']!r}")
    assessment_ref = task_input["assessment_ref"]
    findings = artifacts.read_json(assessment_ref)["findings"]
    supported = [f["path_id"] for f in findings if f["supported"] is True]
    result = {"proposal_ref": proposal_ref, "assessment_ref": assessment_ref,
              "continue_paths": [], "next_grids": {}, "stop_reason": None}
    if task_input["round"] + 1 >= task_input["max_rounds"]:
        result.update(stop_reason="round_budget", reason="The prospective round budget has been reached.")
    elif not supported:
        measured = any(f["supported"] is False for f in findings)
        result.update(stop_reason="no_supported_routes" if measured else "no_measurements",
                      reason="No measured route met the threshold." if measured
                      else "Measurement failed on every route. The predictions remain untested.")
    else:
        path = max(supported, key=lambda name: max(p["score"] for p in observations[name]))
        points = observations[path]
        best = max(points, key=lambda p: p["score"])
        distances = [abs(p["x"] - best["x"]) for p in points if p["x"] != best["x"]]
        step = min(distances) / 2 if distances else 0.25
        grid = {"synthetic": True, "method_id": "fixture-grid", "method_version": "1",
                "candidates": [{"candidate_id": f"{path}-r{task_input['round'] + 1}-{label}",
                                "x": best["x"] + offset}
                               for label, offset in (("left", -step), ("right", step))]}
        result.update(continue_paths=[path], next_grids={path: grid},
                      reason=f"Refine the grid around x={best['x']:g} on {path}, "
                             f"whose best observed score {best['score']:g} supports its prediction.")
    return result
