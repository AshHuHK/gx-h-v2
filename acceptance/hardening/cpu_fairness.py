"""Exercise two-slot CPU admission with real detached grid Jobs and a cold Engine."""

import argparse
from contextlib import closing
import gzip
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.closure.profile import test_environment
from gx_core import budgets, config
from gx_core.capabilities import registry as capabilities, shipped, storage
from gx_core.errors import DependencyUnmet, ResourceDenied
from gx_core.jobs import capacity
from gx_core.jobs.service import JobService, PURPOSE_WORKFLOW
from gx_core.orchestration import service
from gx_core.orchestration.engine import Engine, InterruptRequest, Node, NodeResult, Workflow
from gx_core.orchestration.registry import WorkflowRegistry
from gx_core.persistence import records, store

FLOW = "cpu_queue_fixture"
TWO = "fixture-grid-two@1"


def save(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def submit(conn, rid, manifest, threads, operation):
    return JobService(conn).submit(
        run_id=rid,
        input_manifest_ref=manifest,
        capability_version=TWO,
        purpose=PURPOSE_WORKFLOW,
        tighten={"cpu_threads": threads},
        parameters={"operation": operation},
        idempotency_key=operation,
    )


def admit(ctx):
    job = submit(ctx.conn, ctx.run_id, ctx.inputs["manifest"], ctx.inputs["threads"], "measurement")
    return NodeResult(outputs={"job_id": job["job_id"]})


def admitted(conn, rid, round_index):
    return {"complete": bool(conn.execute("SELECT 1 FROM jobs WHERE run_id=?", (rid,)).fetchone())}


def independent(ctx):
    return NodeResult(outputs={"ready": True})


def result(ctx):
    job = ctx.outputs["admit"]["job_id"]
    row = ctx.conn.execute("SELECT status FROM jobs WHERE job_id=?", (job,)).fetchone()
    if row["status"] != "SUCCEEDED":
        return NodeResult(
            interrupt=InterruptRequest(kind="external_job", reason="Original grid Job runs", job_refs=(job,))
        )
    return NodeResult(outputs={"job": job})


def closed(conn, rid):
    return True, []


def make_run(conn, label, *, workflow=True):
    budget = budgets.create_root(conn, run_id=None, actor="fairness")
    kw = {"workflow_id": FLOW, "workflow_version": "1"} if workflow else {}
    rid = records.create_run(
        conn, objective=label, project_ref="cpu-fairness", actor="fairness", budget_ref=budget, **kw
    )
    if not workflow:
        with store.transaction(conn, transaction_owner="Fixture", operation="activate_holder", run_id=rid):
            for state in ("AUDITING", "PLANNING", "ACTIVE"):
                store.transition(conn, machine="run", subject_id=rid, to_state=state, actor="fairness")
    assets = storage.artifact_service(conn)
    data = {
        "synthetic": True,
        "method_id": "fixture-grid",
        "method_version": "1",
        "candidates": [{"candidate_id": "A", "x": 5}],
    }
    raw = assets.publish_bytes(
        run_id=rid, namespace=rid, rel_path="grid.json", kind="job_input", data=json.dumps(data)
    )
    manifest = assets.make_input_manifest(
        run_id=rid, namespace=rid, entries=[{"role": "grid", "artifact_ref": raw.artifact_ref}]
    )
    return {"id": rid, "manifest": manifest.artifact_ref}


def start(engine, runs, label, threads):
    run = runs[label] = make_run(engine.conn, label)
    engine.start(
        run_id=run["id"],
        workflow_id=FLOW,
        workflow_version="1",
        inputs={"manifest": run["manifest"], "threads": threads},
    )
    assert "independent" in engine.state(run["id"])["completed_nodes"]
    return run


def observe(conn):
    cpu = capacity.snapshot(conn)["cpu"]
    return {
        "allowed": cpu["threads_allowed_for_jobs"],
        "free": cpu["threads_free"],
        "held": cpu["threads_held_by_our_jobs"],
        "waiters": cpu["admission_waiters"],
    }


def setup(conn, engine, work):
    ctx = JobService(conn).ctx
    shipped.ensure(ctx, shipped.GRID)
    bundle = capabilities.candidate(conn, shipped.GRID)["bundle"]
    bundle["manifest"]["capability_id"] = "fixture-grid-two"
    bundle["manifest"]["resources"]["cpu_threads"] = 2
    digest = shipped.portable_digest(bundle)
    pins = config.CONFIG_DIR / "shipped.json"
    doc = json.loads(pins.read_text())
    doc["capabilities"][TWO] = digest
    save(pins, doc)
    capabilities.register_candidate(ctx, bundle, shipped_digest=digest)
    runs = {"holders": make_run(conn, "two independently released Jobs", workflow=False)}
    holder = runs["holders"]
    # Simulate the pre-existing crash boundary between committed admission and launch.
    # Each original CREATED row reserves one genuine CPU slot and will be started by replay.
    with patch.object(JobService, "_start", lambda owner, row: owner.view(row)):
        for key in ("first", "second"):
            holder[key] = submit(conn, holder["id"], holder["manifest"], 1, key)["job_id"]
    assert observe(conn)["allowed"] == 2 and observe(conn)["free"] == 0
    for name in ("paused", "cancelled", "superseded", "large", "small1"):
        start(engine, runs, name, 1 if name == "small1" else 2)
    # A replan can replace a node wait without ever admitting the old operation.
    # Commit that replacement through the same checkpoint writer a recovered workflow uses.
    superseded = runs["superseded"]["id"]
    checkpoint = engine.checkpointer.latest(conn, superseded)
    state = checkpoint.state
    state["node_waits"].pop("admit")
    state.update(pending_interrupt=None, interrupt_ref=None)
    with store.transaction(conn, transaction_owner="Engine", operation="replace_wait", run_id=superseded):
        engine.checkpointer.save(
            conn, run_id=superseded, workflow_id=FLOW, workflow_version="1", node="admit", state=state
        )
    assert not conn.execute("SELECT 1 FROM job_cpu_waits WHERE run_id=?", (superseded,)).fetchone()
    for name, state in [("paused", "PAUSED"), ("cancelled", "CANCELLED")]:
        rid = runs[name]["id"]
        with store.transaction(
            conn, transaction_owner="Fixture", operation="operator_transition", run_id=rid
        ):
            store.transition(
                conn,
                machine="run",
                subject_id=rid,
                to_state=state,
                actor="operator",
                reason="fixture operator decision",
            )
    first = submit(conn, holder["id"], holder["manifest"], 1, "first")
    assert (
        first["job_id"] == holder["first"]
        and JobService(conn).wait(first["job_id"], 30)["status"] == "SUCCEEDED"
    )
    start(engine, runs, "small2", 1)
    queue = observe(conn)
    assert queue["free"] == 1 and [row["run_id"] for row in queue["waiters"]] == [
        runs[k]["id"] for k in ("large", "small1", "small2")
    ]
    assert not any(row["eligible"] for row in queue["waiters"])
    save(work / "handoff.json", {"setup_pid": os.getpid(), "runs": runs, "queue": queue})


def finish(conn, engine, work, out):
    data = json.loads((work / "handoff.json").read_text())
    runs = data["runs"]
    hold = runs["holders"]
    assert (
        data["setup_pid"] != os.getpid()
        and observe(conn)["waiters"][0]["sequence"] == data["queue"]["waiters"][0]["sequence"]
    )
    second = submit(conn, hold["id"], hold["manifest"], 1, "second")
    assert (
        second["job_id"] == hold["second"]
        and JobService(conn).wait(hold["second"], 30)["status"] == "SUCCEEDED"
    )
    queue = [data["queue"], observe(conn)]
    assert queue[-1]["free"] == 2 and [r["run_id"] for r in queue[-1]["waiters"] if r["eligible"]] == [
        runs["large"]["id"]
    ]
    for name in ("small1", "small2"):
        rid = runs[name]["id"]
        ref = engine.waits(rid)["admit"]["interrupt_ref"]
        try:
            engine.resume(run_id=rid, interrupt_ref=ref)
        except DependencyUnmet:
            pass
        else:
            raise AssertionError("younger Run overtook older Run")
    jobs = []
    for name in ("large", "small1", "small2"):
        rid = runs[name]["id"]
        ref = engine.waits(rid)["admit"]["interrupt_ref"]
        with store.transaction(conn, transaction_owner="Engine", operation="resume", run_id=rid):
            begun = engine.begin_resume(conn, run_id=rid, interrupt_ref=ref)
        if name == "large":
            # A separate writer arrives between Engine clearance and original Job reservation.
            with closing(store.open_store(work / "gx.db")) as competitor:
                other = runs["small2"]
                assert observe(competitor)["waiters"][0]["run_id"] == rid
                try:
                    submit(competitor, other["id"], other["manifest"], 1, "measurement")
                except ResourceDenied as exc:
                    assert exc.details["ahead"] == f"job:{rid}:measurement" and exc.details["free"] == 2
                else:
                    raise AssertionError("competing writer took reserved headroom")
        engine.advance_resumed(begun)
        [job] = conn.execute("SELECT job_id FROM jobs WHERE run_id=?", (rid,)).fetchone()
        assert JobService(conn).wait(job, 30)["status"] == "SUCCEEDED"
        if wait := engine.waits(rid).get("result"):
            engine.resume(run_id=rid, interrupt_ref=wait["interrupt_ref"])
        assert engine.state(rid)["status"] == "COMPLETED"
        replay = submit(conn, rid, runs[name]["manifest"], 2 if name == "large" else 1, "measurement")
        assert replay["job_id"] == job and replay["replayed"]
        jobs.append(job)
        queue.append(observe(conn))
    assert not queue[-1]["waiters"]
    events = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY event_id")]
    (out / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in events))
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT run_id,job_id,idempotency_key,status,reservation_ref,resources_json,runner_pid FROM jobs ORDER BY rowid"
        )
    ]
    assert [r["job_id"] for r in rows] == [hold["first"], hold["second"], *jobs]
    for row in rows:
        assert row["runner_pid"] and row["status"] == "SUCCEEDED"
        assert (
            sum(
                e["kind"] == "budget.settled"
                and json.loads(e["payload_json"]).get("reservation_ref") == row["reservation_ref"]
                for e in events
            )
            == 1
        )
        records = conn.execute(
            "SELECT artifact_ref FROM artifacts WHERE job_id=? AND kind='synthetic_grid'", (row["job_id"],)
        ).fetchall()
        assert len(records) == 1
        original = storage.artifact_service(conn).read_json(records[0][0])
        assert original["observations"] == [{"candidate_id": "A", "x": 5, "score": -9}]
    for name in ("large", "small1", "small2"):
        for node in ("admit", "independent", "result"):
            assert (
                sum(
                    e["kind"] == "workflow.node_completed"
                    and e["run_id"] == runs[name]["id"]
                    and json.loads(e["payload_json"])["node"] == node
                    for e in events
                )
                == 1
            )
    with closing(sqlite3.connect(work / "snapshot.db")) as copy:
        conn.backup(copy)
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with (work / "snapshot.db").open("rb") as src, gzip.open(out / "gx.db.gz", "wb") as dest:
        shutil.copyfileobj(src, dest)
    save(
        out / "receipt.json",
        {
            "status": "PASS",
            "evidence_class": "REAL_COMPUTE",
            "scenario_class": "SCRIPTED",
            "runs": runs,
            "queue": queue,
            "jobs": rows,
            "duplicates": {"jobs": 0, "node_completions": 0, "artifacts": 0, "settlements": 0},
            "cpu_budget_threads": 2,
            "checks": {
                "older_two_threads_progressed": True,
                "cold_process_reopened_queue": True,
                "returned_first_slot_preserved": True,
                "cross_connection_admission": True,
                "paused_and_cancelled_excluded": True,
                "noncompute_sibling_progressed": True,
                "superseded_checkpoint_released": True,
                "original_grid_scores_verified": True,
            },
            "pids": [data["setup_pid"], os.getpid()],
            "commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip(),
        },
    )


def run_phase(phase, work, out):
    config.CONFIG_DIR = work / "config"
    flow = Workflow(
        FLOW,
        "1",
        [
            Node("admit", admit, work="stage"),
            Node("independent", independent, work="mechanical"),
            Node("result", result, ("admit",)),
        ],
        stage_rules={"admit": admitted},
        completion_evidence=closed,
    )
    reg = WorkflowRegistry([flow])
    service.set_registry(reg)
    with closing(store.open_store(work / "gx.db")) as conn:
        engine = Engine(conn, reg)
        if phase == "setup":
            setup(conn, engine, work)
        else:
            finish(conn, engine, work, out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=("setup", "resume"))
    parser.add_argument("--work", type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    if args.phase:
        run_phase(args.phase, args.work, out)
        return
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError("use a fresh evidence directory")
    with tempfile.TemporaryDirectory(prefix="gx-fairness-") as temp:
        work = Path(temp)
        shutil.copytree(config.CONFIG_DIR, work / "config")
        env = test_environment(work / "process", profile="deterministic", python=Path(sys.executable))
        for phase in ("setup", "resume"):
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--phase",
                phase,
                "--work",
                str(work),
                "--output",
                str(out),
            ]
            with (out / f"{phase}.log").open("w") as log:
                ran = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT)
            if ran.returncode:
                raise RuntimeError(f'{phase} exited {ran.returncode}, see {out / (phase+".log")}')
    print("PASS FAIRNESS")


if __name__ == "__main__":
    main()
