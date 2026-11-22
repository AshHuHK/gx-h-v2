"""Version one of a bounded grid investigation. Workers own assessment and route choice."""
import math

from gx_core.artifacts import ArtifactService
from gx_core.domain import ids, machines
from gx_core.errors import SchemaInvalid
from gx_core.jobs.grid_workflow import observe_grid, submit_grid
from gx_core.orchestration import InterruptRequest, NodeResult, expansion
from gx_core.persistence import store

TERMINAL = machines.terminal_states("job")
BRIEFS = {
    "assessment": "Evaluate the frozen prediction for every route using its original observations and threshold. "
                  "Return science_assessment JSON with proposal_ref, evidence_refs and findings "
                  "[{path_id,supported,reason}]. Cite evaluation_ref when present. Use null supported for a failed measurement.",
    "decision": "Read the independent assessment and original evidence. Return science_decision JSON "
                "with proposal_ref, assessment_ref, continue_paths, next_grids, reason and stop_reason. "
                "To continue, give each selected route a fresh grid of candidates for fixture-grid@1. "
                "To stop, supply an empty selection and explain the reason.",
}


def checked(ok, message):
    if not ok:
        raise SchemaInvalid(message)


def publish(ctx, kind, body):
    return ArtifactService(ctx.conn).publish_bytes(
        namespace=f"{ctx.run_id}/science/r{ctx.round}", rel_path=f"{kind}-{ids.sha256_of_obj(body)}.json",
        kind=kind, data=ids.canonical_json(body), media_type="application/json", run_id=ctx.run_id,
        actor=ctx.actor).artifact_ref


def _validate_grid(grid):
    checked(isinstance(grid, dict) and grid.get("synthetic") is True
            and (grid.get("method_id"), grid.get("method_version")) == ("fixture-grid", "1")
            and isinstance(grid.get("candidates"), list) and grid["candidates"], "expected fixture-grid@1 candidates")


def proposal(ctx):
    body = ctx.state.get("next_proposal") or ctx.inputs["proposal"]
    routes = body["paths"]
    checked(isinstance(routes, list) and routes and all(isinstance(p.get("id"), str) and p["id"]
            and p.get("prediction") for p in routes) and len({p["id"] for p in routes}) == len(routes),
            "each path needs a unique identifier and a testable prediction")
    checked(isinstance(body.get("threshold"), (float, int)) and math.isfinite(body["threshold"]),
            "the proposal needs a finite prospective threshold")
    checked(type(ctx.inputs.get("max_rounds")) is int and ctx.inputs["max_rounds"] >= 1, "set a positive max_rounds")
    frozen = []
    for path in routes:
        _validate_grid(path["grid"])
        frozen.append({"id": path["id"], "prediction": path["prediction"],
                       "input_ref": publish(ctx, "grid_input", path["grid"])})
    ref = publish(ctx, "research_proposal", {"run_id": ctx.run_id, "round": ctx.round, "paths": frozen,
        "objective": ctx.inputs["objective"], "method": "fixture-grid@1", "threshold": body["threshold"],
        "previous_decision_ref": body.get("previous_decision_ref")})
    return NodeResult(outputs={"proposal_ref": ref})


def _operation(round_index, path):
    return f"science:r{round_index}:{ids.sha256_of_obj(path)}"


def _state(conn, run):
    return store.loads(store.latest_checkpoint(conn, run), "state_json", {})


def experiment_rule(conn, run, round_index):
    proposal_ref = _state(conn, run)["outputs"]["proposal"]["proposal_ref"]
    paths = ArtifactService(conn).read_json(proposal_ref)["paths"]
    keys = {f"job:{run}:grid:{_operation(round_index, p)}" for p in paths}
    finished = {row["idempotency_key"] for row in conn.execute(
        "SELECT status,idempotency_key FROM jobs WHERE run_id=?", (run,)) if row["status"] in TERMINAL}
    return {"complete": keys.issubset(finished)}


def experiment(ctx):
    ref = ctx.outputs["proposal"]["proposal_ref"]
    proposal_body = ArtifactService(ctx.conn).read_json(ref)
    rows, pending = [], []
    for path in proposal_body["paths"]:
        job = submit_grid(ctx.conn, run_id=ctx.run_id, operation_id=_operation(ctx.round, path),
                          input_ref=path["input_ref"])
        job = observe_grid(ctx.conn, job["job_id"])
        row = {"path_id": path["id"], "input_ref": path["input_ref"], "job_id": job["job_id"],
               "status": job["status"], "observations_ref": job.get("observations_ref"), "failure": job.get("failure")}
        if job["status"] not in TERMINAL:
            pending.append(job["job_id"])
        rows.append(row)
    if pending:
        return NodeResult(interrupt=InterruptRequest(kind="stage", reason="Await project Job receipts.",
            job_refs=tuple(pending), payload={"stage": ctx.node, "waiting_state": "WAITING_EXTERNAL_JOB"}))
    if plugin := ctx.inputs.get("evaluator"):
        from gx_core.plugins.evaluator import evaluate_scores
        for row in rows:
            if row["observations_ref"]:
                result = evaluate_scores(ctx.conn, run_id=ctx.run_id, attempt_id=row["job_id"], plugin_ref=plugin,
                    observations_ref=row["observations_ref"], threshold=proposal_body["threshold"])
                row["evaluation_ref"] = result["artifact_ref"]
    ref = publish(ctx, "research_evidence", {"proposal_ref": ref, "routes": rows})
    return NodeResult(outputs={"evidence_ref": ref})


def _accepted(conn, run, stage, round_index):
    return conn.execute("SELECT t.task_id,s.submission_id,s.artifact_refs_json FROM tasks t JOIN submissions s "
        "ON s.task_id=t.task_id AND s.result='ACCEPTED' AND s.attempt=t.attempt "
        "WHERE t.run_id=? AND t.stage=? AND t.status='ACCEPTED' AND json_extract(t.input_json,'$.round')=?",
        (run, stage, round_index)).fetchone()


def assessment_rule(conn, run, round_index):
    return {"complete": bool(_accepted(conn, run, "assessment", round_index))}


def decision_rule(conn, run, round_index):
    return {"complete": bool(_accepted(conn, run, "decision", round_index))}


def _judgment(ctx, role, prior=None):
    kind = "science_" + ctx.node
    saved = _accepted(ctx.conn, ctx.run_id, ctx.node, ctx.round)
    if saved:
        refs = store.loads(saved, "artifact_refs_json")
        ref = next(ref for ref in refs if ArtifactService(ctx.conn).describe(ref)["kind"] == kind)
        return {"artifact_ref": ref, "task_id": saved["task_id"], "submission_id": saved["submission_id"]}
    evidence_ref = ctx.outputs["experiment"]["evidence_ref"]
    inputs = {"round": ctx.round, "max_rounds": ctx.inputs["max_rounds"], "output_kind": kind,
              "instruction": BRIEFS[ctx.node], "proposal_ref": ctx.outputs["proposal"]["proposal_ref"],
              "evidence_refs": [evidence_ref]}
    if prior:
        inputs["assessment_ref"] = prior["artifact_ref"]
    spec = {"stage": ctx.node, "kind": "research_round", "role": role, "bundle_kind": None,
            "input": inputs, "acceptance": {"validate_workflow_product": True}, "required_artifacts": [kind],
            "depends_on": [prior["task_id"]] if prior else []}
    key = expansion.round_key(ctx.run_id, ctx.node, ctx.round, kind, inputs["proposal_ref"])
    tasks = expansion.expand(ctx.gx, ctx.run_id, rule=kind, key=key, trigger={"evidence_ref":evidence_ref},
                             tasks=[spec], stage=ctx.node, round_index=ctx.round, teams=ctx.teams)
    return NodeResult(interrupt=InterruptRequest(kind="stage", tasks=tuple(tasks["task_ids"]),
        reason=BRIEFS[ctx.node], payload={"stage":ctx.node,"waiting_state":"WAITING_CLIENT"}))


def assessment(ctx):
    result = _judgment(ctx, "evidence_reviewer")
    return result if isinstance(result, NodeResult) else NodeResult(outputs=result)


def decision(ctx):
    result = _judgment(ctx, "planner", ctx.outputs["assessment"])
    if isinstance(result, NodeResult):
        return result
    read = ArtifactService(ctx.conn).read_json
    decision_body = read(result["artifact_ref"])
    summary = {**result, "round": ctx.round, "assessment_ref": ctx.outputs["assessment"]["artifact_ref"],
               **ctx.outputs["proposal"], **ctx.outputs["experiment"]}
    ctx.state.setdefault("history", []).append(summary)
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
    checked(lease and root and lease["principal"] != root["owner"], "a scientific judgment needs its assigned worker")
    if inputs["output_kind"] == "science_decision":
        assessment = _accepted(conn, task["run_id"], "assessment", inputs["round"])
        prior = conn.execute("SELECT l.principal FROM submissions s JOIN leases l ON l.lease_id=s.lease_id "
            "WHERE s.submission_id=?", (assessment["submission_id"],)).fetchone()
        checked(prior and prior["principal"] != lease["principal"], "delegate decision and evidence review separately")
    body = assets.read_json(matches[0]["artifact_ref"])
    checked(isinstance(body, dict) and body.get("proposal_ref")==inputs["proposal_ref"], "wrong proposal reference")
    proposal_body = assets.read_json(inputs["proposal_ref"])
    routes = {p["id"]:p for p in proposal_body["paths"]}
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
            checked(type(f["supported"]) is bool if outcomes[f["path_id"]]["status"] == "SUCCEEDED" else f["supported"] is None,
                    "distinguish negative evidence from a failed measurement")
    else:
        selected, grids = body.get("continue_paths"), body.get("next_grids", {})
        checked(body.get("assessment_ref") == inputs["assessment_ref"] and isinstance(body.get("reason"),str)
                and body["reason"].strip() and isinstance(selected,list) and all(isinstance(v,str) for v in selected)
                and len(set(selected))==len(selected) and set(selected)<=routes.keys()
                and isinstance(grids,dict) and set(grids)==set(selected)
                and (selected or isinstance(body.get("stop_reason"),str) and body["stop_reason"].strip()),
                "cite the assessment and justify a next grid or a stop")
        for name, grid in grids.items():
            _validate_grid(grid)
            checked(grid["candidates"] != assets.read_json(routes[name]["input_ref"])["candidates"],
                    "the next grid must test new candidates")


def report(ctx):
    return NodeResult(outputs={"artifact_ref":publish(ctx, "research_report",
        {"run_id":ctx.run_id,"rounds":ctx.state["history"],"stop_reason":ctx.outputs["decision"]["stop_reason"]})})


def completion_evidence(conn, run):
    assets = ArtifactService(conn)
    body = assets.read_json(_state(conn,run["run_id"])["outputs"]["report"]["artifact_ref"])
    if body["run_id"] != run["run_id"] or not body["rounds"] or not body["stop_reason"]:
        return False, ["missing final research decision"]
    for item in body["rounds"]:
        for kind in ("proposal_ref","evidence_ref","assessment_ref","artifact_ref"):
            assets.read_json(item[kind])
    return True, []
