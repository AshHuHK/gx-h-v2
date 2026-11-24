"""Start two real local GX jobs, then recover their raw evidence in a second process."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from gx_core import budgets
from gx_core.capabilities import storage
from gx_core.capabilities.shipped import ensure_grid as activate_grid
from gx_core.context import Ctx, Principal
from gx_core.jobs.service import JobService, PURPOSE_WORKFLOW
from gx_core.jobs.run_resources import observe
from gx_core.persistence import records, store


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "examples/p01_data"
PROJECT = "fixture-science-001"
ACTOR = "p01-scripted-driver"
CAPABILITY = "fixture-grid@1"


def project_run(conn):
    return conn.execute("SELECT * FROM runs WHERE project_ref=?", (PROJECT,)).fetchone()


def prepare(ctx):
    conn = ctx.conn
    project = json.loads((DATA / "project.json").read_text())
    existing = project_run(conn)
    if existing:
        return existing["run_id"]
    resources = observe()
    created = {}
    with store.transaction(conn, transaction_owner="P01Fixture", operation="prepare_run", created_run=created):
        existing = project_run(conn)
        if existing:
            created["run_id"] = existing["run_id"]
            return existing["run_id"]
        budget = budgets.create_root(conn, run_id=None, actor=ctx.actor)
        run = records.create_run(conn, objective=project["objective"]["description"],
                                 project_ref=PROJECT, actor=ctx.actor, budget_ref=budget,
                                 inputs=project, host={"kind": "offline_scripted"}, resource_profile=resources)
        created["run_id"] = run
        for state in ("AUDITING", "PLANNING", "ACTIVE"):
            store.transition(conn, machine="run", subject_id=run, to_state=state, actor=ctx.actor)
        artifacts = storage.artifact_service(conn)
        hypotheses = {h["id"]: h for h in project["hypotheses"]}
        for route in project["paths"]:
            route_id = route["id"]
            grid = (DATA / Path(route["input"]).name).read_bytes()
            namespace = f"{run}/inputs/{route_id}"
            raw = artifacts.publish_bytes(namespace=namespace, rel_path="grid.json", kind="job_input",
                                          run_id=run, data=grid)
            manifest = artifacts.make_input_manifest(namespace=namespace, rel_path="inputs.json",
                run_id=run, entries=[{"role": "grid", "artifact_ref": raw.artifact_ref}])
            spec = {"route_id": route_id, "hypothesis": hypotheses[route["hypothesis_id"]]["prediction"],
                    "method_family": route["method_id"], "tool_and_skill_versions": [CAPABILITY],
                    "input_manifest_ref": manifest.artifact_ref, "operation": f"{run}/{route_id}"}
            path_id = records.create_path(conn, run_id=run, actor=ctx.actor, spec=spec)
            task = records.create_task(conn, run_id=run, path_id=path_id, actor=ctx.actor, role="executor",
                                       kind="execute_path", task_input=spec)
            records.record_worker_observation(conn, task_id=task, host="offline_scripted",
                host_session_ref=run, agent_ref="scripted-" + route_id, state="reported_running",
                evidence_level="CLIENT_REPORTED", payload={"fixture": True})
        return run


def tasks(conn, run):
    return conn.execute("SELECT t.task_id,t.path_id,t.input_json,o.agent_ref,p.fingerprint FROM tasks t "
                        "JOIN worker_observations o ON o.task_id=t.task_id JOIN paths p ON p.path_id=t.path_id "
                        "WHERE t.run_id=? ORDER BY t.rowid", (run,)).fetchall()


def submit_saved(conn, run, interrupt_after_submit=False):
    jobs = JobService(conn, actor=ACTOR)
    for task in tasks(conn, run):
        spec = json.loads(task["input_json"])
        jobs.submit(run_id=run, purpose=PURPOSE_WORKFLOW, capability_version=CAPABILITY,
                    input_manifest_ref=spec["input_manifest_ref"], idempotency_key=spec["operation"],
                    parameters={"operation": spec["operation"]})
        if interrupt_after_submit:
            # The GX owner has a durable receipt. Drop the caller before it reports one.
            raise SystemExit(75)


def collect(conn, run):
    jobs = JobService(conn, actor=ACTOR)
    artifacts = storage.artifact_service(conn)
    evidence = []
    for task in tasks(conn, run):
        spec = json.loads(task["input_json"])
        job = conn.execute("SELECT job_id FROM jobs WHERE run_id=? AND idempotency_key=?",
                           (run, f"job:{run}:{spec['operation']}")).fetchone()
        if not job:
            raise ValueError("launch the saved fixture with start before collecting")
        result = jobs.wait(job["job_id"], timeout_seconds=15)
        if result["status"] != "SUCCEEDED":
            raise RuntimeError(f"Job {job['job_id']}: {result['status']} {result['failure']}")
        ref = next(item["artifact_ref"] for item in result["outputs"]
                   if item["output_name"] == "observations.json")
        document = artifacts.read_json(ref)
        manifest = artifacts.read_input_manifest(spec["input_manifest_ref"], verify_entries=True)
        if (document["operation_id"] != spec["operation"]
                or document["input_sha256"] != manifest["entries"][0]["sha256"]):
            raise ValueError("grid output does not match the saved operation and input")
        scores = [item["score"] for item in document["observations"]]
        evidence.append({"task_id": task["task_id"], "thread_id": task["agent_ref"], "path_id": task["path_id"],
                         "spec_fingerprint": task["fingerprint"], "operation_id": spec["operation"],
                         "route": spec["route_id"], "job_id": job["job_id"], "artifact_ref": ref,
                         "best_score": max(scores), "threshold_met": max(scores) >= -0.25})
    decision = {"kind": "scripted_next_round_input", "threshold": -0.25, "evidence": evidence,
                "continue": [r["route"] for r in evidence if r["threshold_met"]],
                "reconsider": [r["route"] for r in evidence if not r["threshold_met"]]}
    artifacts.publish_bytes(namespace=f"{run}/decisions", rel_path="round-1.json", kind="scripted_decision",
                            run_id=run, data=json.dumps(decision, sort_keys=True))


def status(conn):
    row = project_run(conn)
    if row is None:
        raise ValueError("no fixture project in this database")
    run = row["run_id"]
    decision = conn.execute("SELECT artifact_ref FROM artifacts WHERE run_id=? AND kind='scripted_decision'",
                            (run,)).fetchone()
    result = {"run_id": run, "backend": "gx_local_subprocess", "runtime": "offline_scripted",
              "scientific_claims": False, "capability": CAPABILITY,
              "tasks": [{"task_id": t["task_id"], "thread_id": t["agent_ref"], "fingerprint": t["fingerprint"]}
                        for t in tasks(conn, run)],
              "jobs": [dict(r) for r in conn.execute("SELECT job_id,status,scheduler_ref,idempotency_key FROM jobs "
                                                      "WHERE run_id=? ORDER BY rowid", (run,))],
              "decision_ref": decision[0] if decision else None}
    if decision:
        result["decision"] = storage.artifact_service(conn).read_json(decision[0])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "status", "resume"])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--interrupt-after-submit", action="store_true",
                        help="exit 75 after the first Job is accepted to simulate loss of the caller")
    args = parser.parse_args()
    if args.interrupt_after_submit and args.action != "start":
        parser.error("--interrupt-after-submit applies to start")
    db = args.state_dir.resolve() / "p01.sqlite"
    if args.action != "start" and not db.is_file():
        parser.error("start a fixture in this directory first")
    if args.action == "status":
        connection = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    else:
        args.state_dir.mkdir(parents=True, exist_ok=True)
        connection = store.open_store(db)
    with closing(connection) as conn:
        if args.action == "start":
            ctx = Ctx(conn=conn, principal=Principal(subject=ACTOR, scope="operator"), tool="fixture")
            activate_grid(ctx)
            run = prepare(ctx)
            submit_saved(conn, run, args.interrupt_after_submit)
        if args.action == "resume":
            row = project_run(conn)
            if row is None:
                parser.error("the fixture has no jobs yet")
            submit_saved(conn, row["run_id"])
            collect(conn, row["run_id"])
        print(json.dumps(status(conn), sort_keys=True))


if __name__ == "__main__":
    main()
