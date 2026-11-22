"""One private, offline comparison worker. Imports GX solely from its supplied Git export."""
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

source, workspace, spec_path, arm, cpu = sys.argv[1:]
source, workspace = Path(source), Path(workspace)
sys.path.insert(0, str(source / "gx-h"))

from gx_core.artifacts import ArtifactService
from gx_core.errors import HoldActive
from gx_core.jobs.service import JobService
from gx_core.native_release import native_state
from gx_core.orchestration import reactor, service
from gx_core.p0 import cli, roots, team_admission
from gx_core.persistence import records, store
from gx_core.teams.service import backend_ctx


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def make_roots(conn, rid, spec):
    tools = service.hooks_for_run(conn, rid).build_worker_tools(conn, rid)
    kwargs = {"worker_prepare": tools.prepare} if arm == "p1" else {}
    return roots.Roots(conn, run_id=rid, cwd=workspace, worker_tools=tools.specs,
                       worker_call=tools.call, token_budget=spec["turn_token_budget"], **kwargs)


def counts(conn):
    return {table: conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in ("tasks", "jobs", "leases", "p0_roots", "p0_root_deliveries", "p0_root_turn_usage")}


def scientific_state(conn, rid, target):
    assets = ArtifactService(conn)
    inputs = store.loads(target, "input_json")
    observations, operations = [], {}
    for ref in inputs["evidence_refs"]:
        for route in assets.read_json(ref)["routes"]:
            measured = assets.read_json(route["observations_ref"])
            operations[route["path_id"]] = measured["operation_id"]
            observations.append({"path_id": route["path_id"], "outcome": route["outcome_kind"],
                                 "operation": measured["operation_id"].rsplit(":", 1)[0],
                                 **{k: measured[k] for k in ("method_id", "method_version",
                                                            "input_sha256", "observations")}})
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (rid,)).fetchone()
    binding = store.loads(run, "inputs_json").get("plugin_binding")
    return {"scientific_projection": observations, "scientific_sha256": digest(observations),
            "workflow": [run["workflow_id"], run["workflow_version"], run["workflow_sha256"]],
            "operation_ids": operations, "binding": binding, "role": target["role"], "output": inputs["output_kind"],
            "initial_status": target["status"], "initial_attempt": target["attempt"], "counts": counts(conn),
            "jobs": [dict(r) for r in conn.execute("SELECT status,job_attempt FROM jobs ORDER BY rowid")]}


def run(stack):
    # Public keyed start selects, pins and queues the two finite CPU Jobs. It never starts a native turn.
    with open(spec_path) as f:
        spec = json.load(f)
    if arm == "p1":
        spec["plugins"] = ["org.gx.science-grid@2.0.0"]
    state = stack.enter_context(native_state(workspace))
    conn = stack.enter_context(closing(store.open_store(state.db_path)))
    rid = cli.start(conn, workspace, spec, run_key="claim-comparison")["run_id"]
    jobs = JobService(conn)
    for (job_id,) in conn.execute("SELECT job_id FROM jobs").fetchall():
        assert jobs.wait(job_id, timeout_seconds=30)["terminal"]
    gx = backend_ctx(conn)
    reactor.lazy(gx, rid)
    reactor.drive_science(gx, rid)
    target = conn.execute("SELECT * FROM tasks WHERE run_id=? AND status='READY' AND role='evidence_reviewer'",
                          (rid,)).fetchone()
    assert target and store.loads(target, "input_json")["output_kind"] == "science_assessment"
    task_id = target["task_id"]
    scientific = scientific_state(conn, rid, target)
    assert len(scientific["jobs"]) == 2 and all(j["job_attempt"] == 1 for j in scientific["jobs"])
    assert all(s["outcome"] == "valid_scientific_outcome" for s in scientific["scientific_projection"])
    assert (scientific["binding"] is not None) == (arm == "p1")

    with store.transaction(conn):
        blocker = records.create_task(conn, run_id=rid, kind="research_round", role="planner", actor="backend",
                                      task_input={"instruction": "Hold the team's single claim slot."})
        team = team_admission.selected_team(conn, rid, "planner")
        assert team["max_parallel_roots"] == 1
        team_admission.attach(conn, blocker, team, False)
        store.transition(conn, machine="task", subject_id=blocker, to_state="READY", actor="backend",
                         reason="comparison capacity fixture")

    templates = {}
    def capture(case):
        templates[case] = stack.enter_context(closing(sqlite3.connect(":memory:")))
        conn.backup(templates[case])
    root = make_roots(conn, rid, spec)
    capture("eligible")
    assert root._claim(blocker)["attempt"] == 1
    capture("denied")
    templates["eligible"].backup(conn)
    original = root._claim(task_id)
    assert original["attempt"] == 1 and original["thread_id"] is None
    capture("resumed")

    # Each lane holds a persistent production connection. The new-claim lane gets
    # the identical pre-claim image back between samples. The other two are read-only.
    lanes = {}
    os.sched_setaffinity(0, {int(cpu)})
    for case, template in templates.items():
        db = stack.enter_context(closing(store.connect(workspace / (case + ".db"))))
        template.backup(db)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        lanes[case] = (db, make_roots(db, rid, spec), counts(db))
    emit({"run_id": rid, "task_id": task_id, "held_lease_id": original["lease_id"],
          "held_root_key": original["root_key"], "database_state": {case: lane[2] for case, lane in lanes.items()},
          "seed_database_sha256": {case: hashlib.sha256(template.serialize()).hexdigest()
                                   for case, template in templates.items()},
          "state": scientific, "gx_source": str(Path(roots.__file__).resolve()), "pid": os.getpid(),
          "affinity": sorted(os.sched_getaffinity(0)), "sqlite": sqlite3.sqlite_version})

    for line in sys.stdin:
        request = json.loads(line)
        rows = []
        for case in request:
            db, root, before = lanes[case]
            if case == "eligible":
                templates[case].backup(db)
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            cpu_start = time.process_time_ns()
            start = time.perf_counter_ns()
            try:
                receipt = root._claim(task_id)
            except HoldActive as exc:
                end, cpu_end = time.perf_counter_ns(), time.process_time_ns()
                assert case == "denied" and exc.details["reason"] == "team_capacity" and exc.details["scope"] == "team"
                assert (exc.details["occupied"], exc.details["limit"]) == (1,1)
                classification = "team_capacity"
            else:
                end, cpu_end = time.perf_counter_ns(), time.process_time_ns()
                assert receipt and receipt["attempt"] == 1 and receipt["task_id"] == task_id
                assert receipt["thread_id"] is None
                if case == "resumed":
                    assert (receipt["root_key"], receipt["lease_id"]) == (original["root_key"], original["lease_id"])
                    classification = "same_live_lease"
                else:
                    assert case == "eligible"
                    classification = "new_lease"
            status = db.execute("SELECT status,attempt,attempts_consumed FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            assert tuple(status) == (("READY", 0, 0) if case == "denied" else ("CLAIMED", 1, 0))
            after = counts(db)
            assert after == {k: v + int(case == "eligible" and k in ("leases", "p0_roots")) for k, v in before.items()}
            assert after["p0_root_deliveries"] == after["p0_root_turn_usage"] == 0
            rows.append({"case": case, "outcome": classification, "wall_ns": end - start,
                         "process_cpu_ns": cpu_end - cpu_start})
        emit(rows)


if __name__ == "__main__":
    with ExitStack() as stack:
        run(stack)
