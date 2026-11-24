"""A pinned static test graph using the shipped detached grid and the GX task and artifact owners."""
import json
import shutil

from gx_core import config
from gx_core.artifacts import ArtifactService
from gx_core.jobs.grid_workflow import submit_grid
from gx_core.orchestration import service
from gx_core.orchestration.engine import Engine, InterruptRequest, Node, NodeResult, Workflow
from gx_core.orchestration.hooks import WorkflowHooks
from gx_core.orchestration.registry import WorkflowRegistry
from gx_core.persistence import store


WORKFLOW = "phase2a_static"
OPERATION = "phase2a-original-grid"


def submit(ctx):
    assets = ArtifactService(ctx.conn)
    source = {"synthetic": True, "method_id": "fixture-grid", "method_version": "1",
              "delay_seconds": 5, "candidates": [{"candidate_id": "left", "x": 5}]}
    published = assets.publish_bytes(run_id=ctx.run_id, namespace=ctx.run_id, rel_path="input.json",
                                     kind="grid_input", data=json.dumps(source))
    job = submit_grid(ctx.conn, run_id=ctx.run_id, operation_id=OPERATION, input_ref=published.artifact_ref,
                      binding={"workflow_id": WORKFLOW, "node_id": "B1"})
    return NodeResult(outputs={"job_id": job["job_id"], "input_sha256": published.sha256})


def submitted(conn, run_id, round_index):
    return {"complete": bool(conn.execute("SELECT 1 FROM jobs WHERE run_id=?", (run_id,)).fetchone())}


def decision(ctx):
    def create():
        task = ctx.teams.create_task(run_id=ctx.run_id, kind="propose_paths", role="planner",
                                     bundle_kind="path_bundle", acceptance={},
                                     input={"question": "Which grid point should be examined next?"})
        return task["task_id"]
    task = ctx.once("planner", create)
    return NodeResult(interrupt=InterruptRequest("client_work", "A1 requires the planner decision",
                                                 tasks=(task,), payload={"wait_for": "decision"}))


def decision_descendant(ctx):
    raise AssertionError("A2 cannot execute before its planner decision")


def wait_job(ctx):
    job_id = ctx.outputs["admit"]["job_id"]
    job = ctx.conn.execute("SELECT status,outcome_json FROM jobs WHERE job_id=? AND run_id=?",
                           (job_id, ctx.run_id)).fetchone()
    if job["status"] != "SUCCEEDED":
        return NodeResult(interrupt=InterruptRequest("external_job", "B1 needs its submitted grid result",
                                                     job_refs=(job_id,)))
    raw = next(o for o in json.loads(job["outcome_json"])["outputs"] if o["output_name"] == "observations.json")
    return NodeResult(outputs={"job_id": job_id, "raw_ref": raw["artifact_ref"]})


def independent(ctx):
    # Capture what the Engine had persisted before C1 actually ran.
    state = Engine(ctx.conn, service.default_registry()).state(ctx.run_id)
    return NodeResult(outputs={"states_at_C1": state["node_states"]})


def summarize(ctx):
    assets = ArtifactService(ctx.conn)
    result = assets.read_json(ctx.outputs["B1"]["raw_ref"])
    assert result["operation_id"] == OPERATION and result["input_sha256"] == ctx.outputs["admit"]["input_sha256"]
    summary = {"raw_ref": ctx.outputs["B1"]["raw_ref"], "job_id": ctx.outputs["B1"]["job_id"],
               "max_score": max(item["score"] for item in result["observations"])}
    receipt = assets.publish_bytes(run_id=ctx.run_id, namespace=ctx.run_id, rel_path="summary.json",
                                   kind="phase2a_summary", data=json.dumps(summary))
    return NodeResult(outputs={"ref": receipt.artifact_ref, "sha256": receipt.sha256})


def verify_summary(ctx):
    summary = ArtifactService(ctx.conn).read_json(ctx.outputs["B2"]["ref"])
    assert summary["max_score"] == -9 and summary["raw_ref"] == ctx.outputs["B1"]["raw_ref"]
    return NodeResult(outputs=summary)


def install(directory):
    flow = Workflow(WORKFLOW, "1", [
        Node("admit", submit, work="stage"),
        Node("A1", decision, ("admit",), work="intelligent"),
        Node("A2", decision_descendant, ("A1",), work="mechanical"),
        Node("B1", wait_job, ("admit",)),
        Node("B2", summarize, ("B1",), work="mechanical"),
        Node("B3", verify_summary, ("B2",), work="mechanical"),
        Node("C1", independent, ("admit",), work="mechanical"),
    ], stage_rules={"admit": submitted})
    registry = WorkflowRegistry()
    pinned = registry.register(flow, hooks=WorkflowHooks(auto_advance=True))
    service.set_registry(registry)
    # Reuse normal workflow admission without modifying the release's shipped pins.
    private = directory / "config"
    if not private.exists():
        shutil.copytree(config.CONFIG_DIR, private)
        pins = private / "shipped.json"
        doc = json.loads(pins.read_text())
        doc["workflows"][WORKFLOW + "@1"] = pinned.digest()
        pins.write_text(json.dumps(doc))
    config.CONFIG_DIR = private
    return registry


def observe(conn, run_id):
    engine = Engine(conn, service.default_registry()).state(run_id)
    checkpoint = store.latest_checkpoint(conn, run_id)
    def rows(sql):
        return [dict(r) for r in conn.execute(sql, (run_id,))]
    return {"engine": engine, "outputs": store.loads(checkpoint, "state_json", {}).get("outputs", {}),
            "jobs": rows("SELECT job_id,status,scheduler_ref,runner_pid,runner_started,reservation_ref,exit_code,"
                         "staging_ref FROM jobs WHERE run_id=?"),
            "artifacts": rows("SELECT artifact_ref,sha256,job_id,kind,rel_path FROM artifacts WHERE run_id=? ORDER BY rowid"),
            "tasks": rows("SELECT task_id,role,status,attempt,lease_id FROM tasks WHERE run_id=? ORDER BY rowid"),
            "usage": rows("SELECT d.event_id,d.thread_id,d.turn_id,d.state,d.reservation_ref,r.amounts_json,"
                          "r.status FROM p0_root_deliveries d JOIN budget_reservations r USING(reservation_ref) "
                          "WHERE d.run_id=? ORDER BY d.rowid"),
            "outbox": rows("SELECT outbox_id,topic,consumed_at,attempts,payload_json FROM outbox WHERE run_id=? ORDER BY outbox_id"),
            "completed": rows("SELECT event_id,json_extract(payload_json,'$.node') AS node FROM events "
                              "WHERE run_id=? AND kind='workflow.node_completed' ORDER BY event_id"),
            "settlements": rows("SELECT event_id,json_extract(payload_json,'$.reservation_ref') AS ref FROM events "
                                "WHERE run_id=? AND kind='budget.settled' ORDER BY event_id")}
