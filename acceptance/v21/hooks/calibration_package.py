"""Example installed only in the acceptance deployment. Compare synthetic calibration readings."""
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import SchemaInvalid
from gx_core.jobs.grid_workflow import submit_grid, observe_grid
from gx_core.jobs.service import JobService
from gx_core.orchestration import build, expansion, NodeResult, InterruptRequest
from gx_core.orchestration.hooks import WorkerTools, WorkflowHooks, current_task_context, validate_standard_intent
from gx_core.p0 import intent, science
from gx_core.persistence import store


def validate_intent(conn, run_id, edits):
    validate_standard_intent(conn, run_id, edits)
    for edit in edits:
        if edit["item_id"] == "sampling" and edit["value"] is not None:
            value = edit["value"]
            if not (isinstance(value, dict) and set(value) == {"minimum"} and
                    type(value["minimum"]) is int and value["minimum"] > 0):
                raise SchemaInvalid("Sampling requires a positive minimum number of readings")


def measure(ctx):
    grid = {"synthetic": True, "method_id": "fixture-grid", "method_version": "1",
            "candidates": [{"candidate_id": f"sample-{n}", "x": n} for n in (0, 1, 2)]}
    assets = ArtifactService(ctx.conn)
    ref = assets.publish_bytes(run_id=ctx.run_id, namespace=f"{ctx.run_id}/calibration", rel_path="grid.json",
        kind="input", data=ids.canonical_json(grid), media_type="application/json").artifact_ref
    criterion = intent.read_item(ctx.conn, ctx.run_id, "evaluation")
    frozen = science.freeze_spec(ctx.conn, ctx.run_id, criterion["value"])
    job = submit_grid(ctx.conn, run_id=ctx.run_id, operation_id="calibration", input_ref=ref,
        binding={"workflow_id": ctx.workflow_id, "node_id": ctx.node, "evaluator": frozen})
    # Small bounded real CPU Job. The node runs outside a callback and outside SQLite's write transaction.
    JobService(ctx.conn).wait(job["job_id"], timeout_seconds=15)
    actual = observe_grid(ctx.conn, job["job_id"])
    actual["path_id"] = None
    science.experiment_result(ctx.conn, ctx.run_id, actual, {"evaluator": frozen,
        "evaluation_item": {"item_id": "evaluation", "version": criterion["version"]}})
    if not actual.get("evaluation_ref"):
        raise ValueError(f"Could not evaluate original measurements: {actual}")
    return NodeResult(outputs={name: actual[name] for name in ("job_id", "observations_ref", "evaluation_ref")})


def judgment(conn, run_id):
    row = conn.execute("SELECT s.submission_id,s.artifact_refs_json FROM submissions s JOIN tasks t USING(task_id) "
                       "WHERE t.run_id=? AND t.stage='review' AND s.result='ACCEPTED'", (run_id,)).fetchone()
    if row:
        return {"submission_id": row["submission_id"], "artifact_ref": store.loads(row, "artifact_refs_json")[0]}


def review(ctx):
    if result := judgment(ctx.conn, ctx.run_id):
        return NodeResult(outputs=result)
    request = {"stage": ctx.node, "kind": "research_round", "role": "evidence_reviewer",
        "bundle_kind": None, "input": {"output_kind": "calibration_review", **ctx.outputs["measure"],
            "instruction": "Inspect gx_task_context, count observations with gx_sample_count and submit the observed count."},
        "acceptance": {"validate_workflow_product": True}, "required_artifacts": ["calibration_review"]}
    key = expansion.round_key(ctx.run_id, ctx.node, ctx.round, "readings", "instrument")
    outcome = expansion.expand(ctx.gx, ctx.run_id, rule="calibration_review", key=key, trigger=ctx.outputs["measure"],
                              tasks=[request], stage=ctx.node, round_index=ctx.round, teams=ctx.teams)
    return NodeResult(interrupt=InterruptRequest(kind="stage", tasks=tuple(outcome["task_ids"]),
                            reason="Compare the actual observations", payload={"stage": ctx.node, "waiting_state":"WAITING_CLIENT"}))


def measured(conn, run_id, round_index):
    return {"complete": bool(conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND status='SUCCEEDED'", (run_id,)).fetchone())}


def complete(conn, run_id, round_index):
    return {"complete": bool(judgment(conn, run_id))}


def finished(conn, run):
    return bool(judgment(conn, run["run_id"])), []


def validate(conn, task, bundle):
    refs = task["submission_artifact_refs"]
    if len(refs) != 1:
        raise SchemaInvalid("Submit one calibration result")
    body = ArtifactService(conn).read_json(refs[0])
    ref = task["input"]["observations_ref"]
    count = len(ArtifactService(conn).read_json(ref)["observations"])
    if (body.get("count"), body.get("observations_ref")) != (count, ref):
        raise SchemaInvalid("The submitted reading count must match its observed source")


def prepare_job(conn, run_id, request):
    if (request.get("capability_version"), request.get("parameters", {}).get("operation")) != ("fixture-grid@1", "calibration"):
        raise SchemaInvalid("Only this calibration measurement was requested")


def prepare_dispatch(conn, run_id, task):
    item = intent.read_item(conn, run_id, "sampling")
    conn.execute("UPDATE tasks SET input_json=json_set(input_json,'$.minimum',?) WHERE task_id=? AND status='READY'",
                 (item["value"]["minimum"], task["task_id"]))
    return True


def task_context(conn, run_id, task_id, attempt):
    return {**current_task_context(conn, run_id, task_id, attempt), "instrument_units": "readings", "attempt": attempt}


def worker_tools(conn, run_id):
    tool = {"type": "function", "name": "gx_sample_count", "description": "Count this task's observed samples.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}

    def call(row, tool, args, envelope):
        task = conn.execute("SELECT input_json FROM tasks WHERE task_id=? AND run_id=?", (row["task_id"], run_id)).fetchone()
        ref = store.loads(task, "input_json")["observations_ref"]
        return {"count": len(ArtifactService(conn).read_json(ref)["observations"]), "observations_ref": ref}

    return WorkerTools((tool,), call=call)


def replan(conn, run_id):
    run = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run["status"] == "PAUSED":
        store.append_event(conn, run_id=run_id, kind="calibration.resume", actor="backend", subject_kind="run",
                           subject_id=run_id, payload={"sampling": intent.read_item(conn, run_id, "sampling")["value"]})
    return False


def report(ctx):
    return NodeResult(outputs=ctx.outputs["review"])


def register_all(registry):
    child = register_default_example(registry)
    graph = {"workflow_id":"calibration", "version":"1", "description":"Count finite synthetic measurements",
             "entry_transitions":["AUDITING","PLANNING","ACTIVE"], "nodes":[
                 {"name":"measure","work":"stage"}, {"name":"review","work":"stage","artifact_task_kind":"research_round","deps":["measure"]},
                 {"name":"report","work":"mechanical","deps":["review"]}]}
    graph["calibration_note"] = {"id": child.workflow_id, "version": child.version, "sha256": child.digest()}
    flow = build(graph, {"measure":measure, "review":review, "report":report}, stage_rules={"measure":measured, "review":complete},
                 completion_evidence=finished, task_product_validator=validate)
    hooks = WorkflowHooks(validate_intent=validate_intent, build_worker_tools=worker_tools,
                          prepare_dispatch=prepare_dispatch, build_task_context=task_context, prepare_job=prepare_job,
                          replan=replan, reevaluate=science.reevaluate_intent, auto_advance=True)
    return [registry.register(flow, hooks=hooks)]


def record_note(ctx):
    return NodeResult(outputs={"objective": ctx.inputs["objective"]})


def note_recorded(conn, run):
    state = store.loads(store.latest_checkpoint(conn, run["run_id"]), "state_json")
    actual = state["outputs"]["record"]["objective"]
    return actual == intent.read_item(conn, run["run_id"], "objective")["value"], []


def register_default_example(registry):
    definition = {"workflow_id":"calibration_note", "version":"1",
        "nodes":[{"name":"record","work":"mechanical"}], "entry_transitions":["AUDITING","PLANNING","ACTIVE"]}
    return registry.register(build(definition, {"record":record_note}, completion_evidence=note_recorded))
