"""P2A-15. Inject an unavailable status read while real detached CPU Jobs finish."""
import argparse
from contextlib import closing
import errno
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gx-h"))

from gx_core import budgets
from gx_core.capabilities import procs, storage
from gx_core.jobs import capacity, executor
from gx_core.jobs.grid_workflow import observe_grid, submit_grid
from gx_core.jobs.service import JobService
from gx_core.orchestration import seams
from gx_core.persistence import records, store
from gx_core.project_work import _reconcile_reason


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def snapshot(conn, job_id):
    row = conn.execute(
        "SELECT job_id,status,scheduler_ref,side_effect_status,observed_at,reservation_ref "
        "FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    reservation = row["reservation_ref"]
    return {**dict(row),
            "reservation_status": conn.execute("SELECT status FROM budget_reservations WHERE reservation_ref=?",
                                               (reservation,)).fetchone()[0],
            "settlements": conn.execute("SELECT count(*) FROM events WHERE kind='budget.settled' "
                                        "AND json_extract(payload_json,'$.reservation_ref')=?", (reservation,)).fetchone()[0],
            "terminal_outbox": conn.execute("SELECT count(*) FROM outbox WHERE idempotency_key=?",
                                            (f"job.terminal:{job_id}",)).fetchone()[0],
            "artifacts": [r[0] for r in conn.execute("SELECT artifact_ref FROM artifacts WHERE job_id=? ORDER BY rowid",
                                                     (job_id,))]}


def database_copy(conn, path, scratch):
    with closing(sqlite3.connect(scratch)) as target:
        conn.backup(target)
    with scratch.open("rb") as original, gzip.open(path, "wb") as output:
        shutil.copyfileobj(original, output)


def exercise(conn, directory, out):
    actor = "p2a-15"
    budget = budgets.create_root(conn, run_id=None, actor=actor)
    with store.transaction(conn) as tx:
        run = records.create_run(tx, objective="Reconcile two local CPU Jobs", project_ref=actor,
                                 actor=actor, budget_ref=budget)
        for state in ("AUDITING", "PLANNING", "ACTIVE"):
            store.transition(tx, machine="run", subject_id=run, to_state=state, actor=actor)
    requests = {}
    ids = {}
    for name, x in (("delayed_read", 2), ("failed_tool", "invalid number")):
        grid = {"synthetic": True, "method_id": "fixture-grid", "method_version": "1",
                "candidates": [{"candidate_id": name, "x": x}]}
        ref = storage.artifact_service(conn).publish_bytes(
            namespace=f"{run}/input", rel_path=f"{name}.json", kind="job_input", run_id=run,
            data=json.dumps(grid)).artifact_ref
        requests[name] = {"run_id": run, "operation_id": name, "input_ref": ref}
        ids[name] = submit_grid(conn, **requests[name])["job_id"]
    primary, peer = ids["delayed_read"], ids["failed_tool"]
    receipts = {job: storage.jobs_root(conn) / job / "outcome.json" for job in ids.values()}
    deadline = time.monotonic() + 30
    while not all(path.is_file() for path in receipts.values()):
        if time.monotonic() >= deadline:
            raise TimeoutError("local fixture Jobs did not produce their runner receipts")
        time.sleep(0.05)

    jobs = JobService(conn)
    before = {name: snapshot(conn, job) for name, job in ids.items()}
    assert all(row["reservation_status"] == "HELD" and row["scheduler_ref"] for row in before.values())
    backup = directory / "snapshot.sqlite"
    database_copy(conn, out / "before.sqlite.gz", backup)
    failed_reads = []
    backend = receipts[primary].parent.parent
    offline = directory / "backend-offline"

    def disconnect(path, *args, **kwargs):
        if Path(path) == receipts[primary] and backend.exists():
            backend.rename(offline)
        return open(path, *args, **kwargs)

    try:
        with patch.object(executor, "open", side_effect=disconnect, create=True):
            unavailable_backend = jobs.status(primary)
        assert unavailable_backend.get("reconciliation_deferred")
        assert seams.reconcile_jobs(conn, run_id=run, actor=actor) == []
        assert not backend.exists()
        assert all(snapshot(conn, job) == before[name] for name, job in ids.items())
    finally:
        if offline.exists():
            offline.rename(backend)

    def unavailable(path, *args, **kwargs):
        if Path(path) == receipts[primary]:
            failed_reads.append(time.time_ns())
            raise TimeoutError(errno.ETIMEDOUT, "scripted status read timeout")
        return open(path, *args, **kwargs)

    with patch.object(executor, "open", side_effect=unavailable, create=True):
        with patch.object(JobService, "submit", side_effect=AssertionError("reconciliation attempted submit")):
            status = jobs.status(primary)
            unchanged = conn.execute("SELECT * FROM jobs WHERE job_id=?", (primary,)).fetchone()
            assert _reconcile_reason(conn, unchanged, due=True) is None
            allocation = next(r for r in capacity.jobs_state(conn)["jobs"] if r["job_id"] == primary)
            assert allocation["holding_resources"] and allocation["holding_cpu_threads"]
            moved = seams.reconcile_jobs(conn, run_id=run, actor=actor)
            assert status.get("reconciliation_deferred") and "temporarily unavailable" in status["observation"]
            assert moved == [{"from": before["failed_tool"]["status"], "to": "FAILED", "job_id": peer}], moved
        replay = submit_grid(conn, **requests["delayed_read"])
        assert replay["job_id"] == primary and replay["replayed"]
    during = {name: snapshot(conn, job) for name, job in ids.items()}
    assert during["delayed_read"] == before["delayed_read"], during
    assert jobs.status(peer)["failure"]["observed"] == "nonzero_exit"
    assert during["failed_tool"]["settlements"] == during["failed_tool"]["terminal_outbox"] == 1
    database_copy(conn, out / "deferred.sqlite.gz", backup)

    def corrupt(path, *args, **kwargs):
        if Path(path) == receipts[primary]:
            return io.StringIO("{invalid JSON")
        return open(path, *args, **kwargs)

    with patch.object(executor, "open", side_effect=corrupt, create=True):
        unknown = jobs.status(primary)
    assert unknown["status"] == "OUTCOME_UNKNOWN" and not unknown.get("reconciliation_deferred")
    assert snapshot(conn, primary)["reservation_status"] == "HELD"
    recovered = seams.reconcile_jobs(conn, run_id=run, actor=actor)
    assert recovered == [{"from": "OUTCOME_UNKNOWN", "to": "SUCCEEDED", "job_id": primary}], recovered
    assert observe_grid(conn, primary)["observations"]["observations"][0]["score"] == 0
    after = {name: snapshot(conn, job) for name, job in ids.items()}
    assert seams.reconcile_jobs(conn, run_id=run, actor=actor) == []
    assert all(snapshot(conn, job) == after[name] for name, job in ids.items())
    for name, row in after.items():
        assert row["scheduler_ref"] == before[name]["scheduler_ref"]
        assert row["reservation_status"] == "SETTLED" and row["settlements"] == row["terminal_outbox"] == 1
        assert len(row["artifacts"]) == len(set(row["artifacts"]))
    database_copy(conn, out / "after.sqlite.gz", backup)
    refs = tuple(row["reservation_ref"] for row in after.values())
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE run_id=? OR json_extract(payload_json,'$.reservation_ref') IN (?,?) "
        "ORDER BY event_id", (run, *refs))]
    save(out / "events.json", events)

    # A transient /proc read must not establish the death of an identified live runner.
    empty = directory / "liveness-control"
    empty.mkdir()
    ticks = procs.process_start_ticks(os.getpid())
    assert ticks and executor.observe(empty)["state"] == executor.NOT_STARTED
    with patch.object(procs, "open", side_effect=OSError(errno.ESTALE, "scripted stale process handle"), create=True):
        live = executor.observe(empty, runner_pid=os.getpid(), runner_started=ticks)
    assert live.get("deferred") and live["runner_alive"] is None
    job_count = conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (run,)).fetchone()[0]
    assert job_count == len(ids)
    return {"run_id": run, "job_ids": ids, "branch_ids": [], "node_ids": [], "before": before, "during": during,
            "after": after, "same_pass_peer_changes": moved, "recovered": recovered,
            "backend_directory_outage": unavailable_backend["observation"], "duplicate_job_rows": job_count-len(ids),
            "failed_read_timestamps_ns": failed_reads, "status_during_outage": status["observation"],
            "corrupt_record_control": unknown["observation"], "unreadable_process_control": live["reason"]}


def main(out):
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True).splitlines()
    commands = []

    def audit(event, args):
        if event == "subprocess.Popen":
            argv = [str(arg) for arg in args[1]]
            commands.append({"executable": Path(argv[0]).name,
                             "detached_job": Path(argv[-1]).name if "--detach" in argv else None})

    sys.addaudithook(audit)
    with tempfile.TemporaryDirectory(prefix="gx-p2a15-") as tmp:
        work = Path(tmp)
        trace = work / "metrics.jsonl"
        with patch.dict(os.environ, GX_METRICS_FILE=str(trace)), closing(store.open_store(work / "gx.db")) as conn:
            result = exercise(conn, work, out)
        metrics = [r for line in trace.read_text().splitlines() if (r := json.loads(line))["kind"] == "job_reconcile"]
        assert any(r["open_jobs"] == 2 and r["changed_jobs"] == r["deferred_jobs"] == 1 for r in metrics)
        save(out / "reconcile-metrics.json", metrics)
    launches = [row["detached_job"] for row in commands if row["detached_job"]]
    codex = sum("codex" in row["executable"] for row in commands)
    assert sorted(launches) == sorted(result["job_ids"].values()) and codex == 0, commands
    relevant = ["gx-h/gx_core/jobs/" + p for p in ("executor.py", "service.py", "tools.py")]
    relevant += ["gx-h/gx_core/capabilities/procs.py", "gx-h/gx_core/project_work.py",
                 "acceptance/phase2a/job_unavailable.py"]
    result.update(test="P2A-15", evidence_class="SCRIPTED", scope="JobService and the Driver's reconciliation seam",
                  command=sys.orig_argv, cwd=str(Path.cwd()), commit=revision, dirty_state=dirty, exit_code=0,
                  source_sha256={p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in relevant},
                  child_processes=commands, codex_spawn_count=codex, duplicate_launch_count=len(launches)-len(set(launches)),
                  evidence_files=["before.sqlite.gz", "deferred.sqlite.gz", "after.sqlite.gz", "events.json",
                                  "reconcile-metrics.json"])
    save(out / "result.json", result)
    print(f"P2A-15 passed for Run {result['run_id']} with {len(launches)} original Jobs and {codex} Codex launches")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "jobs")
    main(parser.parse_args().output)
