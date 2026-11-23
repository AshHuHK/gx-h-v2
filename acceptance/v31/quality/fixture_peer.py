"""Scripted positive control. Inputs are restricted to task briefs and native tool replies."""
import json
import re
from math import prod

import e2e_responses as responses


def estimates(points, x):
    points = sorted((p["x"], p["score"]) for p in points)
    assert len(points) == 3 and len({p[0] for p in points}) == 3
    quadratic = sum(y0 * prod((x - x1) / (x0 - x1) for x1, _ in points if x0 != x1)
                    for x0, y0 in points)
    for (a, ya), (b, yb) in zip(points, points[1:]):
        if a <= x <= b:
            return {"quadratic": quadratic, "linear": ya + (x - a) * (yb - ya) / (b - a)}
    raise ValueError("Follow-up candidate outside interpolation support")


def judgments(task, artifacts, version):
    routes = artifacts.read_json(task["evidence_refs"][0])["routes"]
    proposal = artifacts.read_json(task["proposal_ref"])
    points = {}
    for r in routes:
        measured = artifacts.read_json(r["observations_ref"])
        evaluated = artifacts.read_json(r["evaluation_ref"])
        assert r["outcome_kind"] == "valid_scientific_outcome" and evaluated["input_ref"] == r["observations_ref"]
        points[r["path_id"]] = measured["observations"]
    findings = {}
    for k, ps in points.items():
        best = max(p["score"] for p in ps)
        crossing = best >= proposal["threshold"]
        comparison = "crosses" if crossing else "falls below"
        findings[k] = {"reason": f"Measured maximum {best:g} {comparison} the cutoff {proposal['threshold']:g} on {k}.",
                       "risk": "Limited to the specified samples and score scale."}
    described, by_id, catalog = [], {}, []
    for src in task.get("supplement_sources", []):
        doc = artifacts.read_json(src["artifact_ref"])
        if doc["text"].startswith("Controller catalog"):
            catalog = json.loads(re.search(r'\[[^\]]+\]', doc["text"]).group())
            reason = "Use the permitted follow-up coordinates. The catalog does not alter the original measurements."
            disposition, compared = "accepted", list(points)
        else:
            x, prior = map(float, re.search(r"x=(\d+) .*?measured exactly (\d+)", doc["text"]).groups())
            path, value = next((k, p["score"]) for k, ps in points.items() for p in ps if p["x"] == x)
            assert value != prior
            reason = f"The original observation at x={x:g} is {value:g}, contradicting the archived {prior:g}."
            disposition, compared = "rejected", [path]
        described.append({"source_id": src["source_id"], "artifact_ref": src["artifact_ref"],
                          "disposition": disposition, "reason": reason,
                          "evidence_refs": [r["observations_ref"] for r in routes if r["path_id"] in compared]})
        by_id[src["source_id"]] = {"disposition": disposition, "reason": reason,
                                   "compared_paths": compared, "qualifies_paths": []}
    if task["output_kind"] == "science_assessment":
        if version == "3":
            return {"findings_by_path": findings, **({"source_reviews_by_id": by_id} if by_id else {})}
        return {"proposal_ref": task["proposal_ref"], "evidence_refs": task["evidence_refs"],
                "findings": [{"path_id": r["path_id"], "reason": findings[r["path_id"]]["reason"],
                              "evaluation_ref": r["evaluation_ref"],
                              "supported": max(p["score"] for p in points[r["path_id"]]) >= proposal["threshold"]}
                             for r in routes], "source_reviews": described}
    reviewed = artifacts.read_json(task["assessment_ref"])
    assert {s["source_id"]: s["disposition"] for s in reviewed.get("source_reviews", [])} == {
        s["source_id"]: s["disposition"] for s in described}
    result = {"proposal_ref": task["proposal_ref"], "assessment_ref": task["assessment_ref"],
              "continue_paths": [], "next_grids": {}, "source_decisions": described}
    if task["round"] == 0:
        qualifying, = [k for k, ps in points.items() if max(p["score"] for p in ps) >= proposal["threshold"]]
        predictions = {x: estimates(points[qualifying], x) for x in catalog}
        chosen = {m: max(catalog, key=lambda x: predictions[x][m]) for m in ("quadratic", "linear")}
        next_grid = {"method_id": "fixture-grid", "method_version": "1", "synthetic": True,
                     "candidates": [{"candidate_id": m, "x": x} for m, x in chosen.items()]}
        result.update(continue_paths=[qualifying], next_grids={qualifying: next_grid}, stop_reason=None,
                      reason=f"Only H_{qualifying} survives the finite test. The catalog restricts acquisition. "
                             f"Quadratic interpolation selects {chosen['quadratic']:g}; piecewise-linear selects "
                             f"{chosen['linear']:g}. Measure their head-to-head on the same score scale.")
    else:
        values = {p["candidate_id"]: p for ps in points.values() for p in ps}
        winner = max(values.values(), key=lambda p: p["score"])
        result.update(stop_reason="completed_head_to_head",
                      reason="; ".join(f"{m} choice x={p['x']:g} measured {p['score']:g}" for m, p in values.items())
                             + f". {winner['candidate_id']} selected the better candidate in this finite catalog. "
                               "This single noiseless comparison does not establish performance on other response surfaces.")
    return result


class StudyPeer:
    def __init__(self, version):
        self.version = version

    def __call__(self, body, ident):
        brief = responses.brief_from(body)
        task = brief["input"]
        meta = {"task_id": brief["task_id"], "round": task["round"], "output_kind": task["output_kind"]}
        artifacts, submitted = responses.tool_history(body["input"])
        for ref, entry in brief.get("preloaded_artifacts", {}).items():
            artifacts.replies.setdefault(ref, entry["content"])
        if submitted:
            return {"type": "message", "id": "m-" + ident, "role": "assistant",
                    "content": [{"type": "output_text", "text": "Recorded."}]}, meta
        try:
            product = judgments(task, artifacts, self.version)
            name, args = "gx_submit_product", {"product": product}
        except responses.ReadArtifact as ex:
            name, args = "gx_read_artifact", {"ref": ex.args[0]}
        event = {"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": "call-" + ident}
        if name == "gx_submit_product":
            groups = [g for g in body.get("tools", []) if g.get("type") == "namespace" and
                      any(t.get("name") == name for t in g.get("tools", []))]
            if groups:
                event["namespace"] = groups[0]["name"]
        return event, {**meta, "tool": name, **({"namespace": event["namespace"]} if "namespace" in event else {})}
