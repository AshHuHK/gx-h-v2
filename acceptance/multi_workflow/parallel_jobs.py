"""Two real CPU workers rendezvous, lose their coordinator, and settle in the original Goal."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PLUGIN = "org.gx.fixture-cpu-barrier"
METHOD = "fixture-cpu-barrier@1"


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def package(directory):
    directory.mkdir()
    worker = directory / "worker.py"
    shutil.copyfile(HERE / "barrier_worker.py", worker)
    # Mirror the minimal public plugin manifest; its implementation is only present in the private test installation.
    save(directory / "gx_plugin.json", {
        "id": PLUGIN, "version": "1.0.0", "kind": "bundle", "api_version": "1.0", "gx_compat": ">=3.0,<4.0",
        "entrypoint": "plugin:register", "dependencies": [],
        "extensions": [{"id": "barrier", "contract": "capability", "version": "1.0"},
                       {"id": "barrier-result", "contract": "goal_method", "version": "1.0"}]})
    (directory / "plugin.py").write_text("def register(ctx):\n"
        "    ctx.capabilities.add_manifest('method.json')\n"
        "    ctx.goal_methods.register('fixture-cpu-barrier@1', project=lambda value: value)\n")
    save(directory / "method.json", {"manifest": {
        "capability_id": "fixture-cpu-barrier", "version": "1", "kind": "UTILITY", "env_id": "control-plane-python",
        "entry": {"argv_template": ["{env.python}", "-I", "-B", "{entry_script}", "--sample", "{in.sample}", "--out", "{out.dir}"]},
        "weights": [], "required_data": [], "method_card": {"family": "cpu capacity control", "mechanism": "Rendezvous two workers",
        "objective": "Measure independent CPU capacity", "estimator": "Process CPU time and affinity", "training_data_lineage": "Synthetic"},
        "source": {"path": "{harness}/plugins/installed/" + PLUGIN + "/1.0.0/worker.py",
                   "sha256": hashlib.sha256(worker.read_bytes()).hexdigest()},
        "licence": "UNVERIFIED", "licence_basis": "Local acceptance workload.", "limitations": "Only tests local execution.",
        "resources": {"cpu_threads": 1, "wall_seconds": 30}, "outputs": [{"name": "sample.json", "kind": "file:cpu_sample",
        "required": True}]}, "entry_script": "worker.py"})


def cli(release, *argv):
    env = {**os.environ, "GX_CODEX_OFFLINE": "1"}
    done = subprocess.run([sys.executable, "-B", str(release / "bin/gx-p0"), *map(str, argv)],
                          capture_output=True, text=True, timeout=45, env=env)
    if done.returncode:
        raise RuntimeError(f"gx-p0 {argv[0]}: {done.stderr} {done.stdout}")
    return json.loads(done.stdout)


def coordinator(work, phase):
    release = work / "release"
    state = work / "project/state"
    sys.path.insert(0, str(release / "gx-h"))
    from gx_core.artifacts import ArtifactService
    from gx_core.jobs import capacity
    sys.path.insert(0, str(ROOT))
    from acceptance.v62a import interleavings_fixture as fx
    record = work / "interrupted.json"
    if phase == "dispatch":
        with fx.Scenario(work / "project", reopen=True) as s:
            run_id = s.run_id
            room = capacity.cpu_room(s.conn)
            if room["free"] < 2:
                raise RuntimeError(f"The controlled CPU experiment requires two available slots: {room}")
            actor = s.next_actor("run")
            branch = fx.branch("capacity")
            branch.update(budget_limits={"job_submissions": 2, "cpu_seconds": 60})
            nodes = [s.node(actor, side, ref, capability=METHOD, branch_id=branch["branch"])
                     for side, ref in zip(("left", "right"), s.materials, strict=True)]
            s.call(actor, "gx_decide_revision", {"decision": fx.draft(branch, *nodes)}, "parallel-cpu")
    else:
        previous = json.loads(record.read_bytes())
        run_id = previous["run_id"]
        (work / "coordinator-resumed").touch()

    command = "run" if phase == "dispatch" else "resume"
    argv = [sys.executable, "-B", str(release / "bin/gx-p0"), command, "--state", str(state),
            "--run-id", run_id, "--timeout", "35"]
    sql = "SELECT node_id,status,job_id,result_ref FROM v6_nodes WHERE run_id=? ORDER BY rowid"
    with (work / (command + ".log")).open("w") as log, subprocess.Popen(
            argv, env={**os.environ, "GX_CODEX_OFFLINE": "1"}, stdout=log, stderr=log) as driver:
        try:
            conn = sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    rows = [dict(r) for r in conn.execute(sql, (run_id,))]
                    if driver.poll() is not None or any(r["status"] in ("BLOCKED", "FAILED", "CANCELLED") for r in rows):
                        raise RuntimeError(f"{command} exited {driver.returncode}, observed nodes {rows}")
                    if phase == "dispatch" and len(rows) == 2 and rows[0]["status"] == "SUCCEEDED":
                        assert rows[1]["status"] == "RUNNING", rows
                        driver.kill()
                        assert driver.wait(timeout=5) == -signal.SIGKILL
                        save(record, {"run_id": run_id, "pid": driver.pid, "command": command,
                                      "exit_code": driver.returncode, "nodes": rows, "capacity": room})
                        return
                    if phase == "resume" and len(rows) == 2 and all(r["status"] == "SUCCEEDED" for r in rows):
                        break
                    time.sleep(.03)
                else:
                    raise TimeoutError(f"Coordinator {command} did not reach its checkpoint: {rows}")
            finally:
                conn.close()
        finally:
            if driver.poll() is None:
                driver.terminate()
                driver.wait(timeout=5)
    with fx.Scenario(work / "project", reopen=True) as s:
        assert run_id == s.run_id and previous["pid"] != driver.pid
        assert rows[0] == previous["nodes"][0]
        assert [r["job_id"] for r in rows] == [r["job_id"] for r in previous["nodes"]]
        jobs = s.rows("SELECT job_id,runner_pid,job_attempt,submit_intent_json FROM jobs WHERE run_id=? ORDER BY rowid",
                      run_id)
        for job in jobs:
            job["assigned_cpus"] = json.loads(job.pop("submit_intent_json")).get("cpu_affinity")
        assert len(jobs) == 2 and all(r["job_attempt"] == 1 for r in jobs)
        attempts = s.rows("SELECT node_id,operation_key FROM v6_job_attempts WHERE run_id=? ORDER BY rowid", run_id)
        assert len(attempts) == 2
        artifacts = ArtifactService(s.conn)
        samples = [artifacts.read_json(n["result_ref"]) for n in rows]
        model_calls, = s.conn.execute("SELECT COUNT(*) FROM p0_meter_native_turns WHERE run_id=?", (run_id,)).fetchone()
        assert model_calls == 0
        saved = {"run_id": run_id, "original": previous, "pid": driver.pid, "command": command, "jobs": jobs,
                 "nodes": rows, "attempts": attempts, "workers": samples, "real_model_calls": model_calls}
        save(work / "observed.json", saved)
        assert samples[0]["pid"] != samples[1]["pid"] and all(r["cpu_ns"] > 0 for r in samples)
        overlap = min(r["compute_end_ns"] for r in samples) - max(r["compute_start_ns"] for r in samples)
        assert overlap > 0, "CPU computations did not overlap"
        assert set(samples[0]["affinity"]).isdisjoint(samples[1]["affinity"]), "CPU workers pinned to the same CPU"
        assignments = {job["job_id"]: job["assigned_cpus"] for job in jobs}
        assert all(assignments[node["job_id"]] == sample["affinity"] for node, sample in zip(rows, samples, strict=True))
        elapsed = max(r["compute_end_ns"] for r in samples) - min(r["compute_start_ns"] for r in samples)
        concurrent_cpu = sum(r["cpu_ns"] for r in samples) - elapsed
        assert concurrent_cpu > 0, "The two workers used no more CPU time than a single CPU allowed"
        saved.update(compute_overlap_ns=overlap, concurrent_cpu_lower_bound_ns=concurrent_cpu,
                     successful_job_reused=True, extra_job_attempts=0)
        save(work / "observed.json", saved)


def experiment(work):
    release = work / "release"
    release.mkdir()
    shutil.copytree(ROOT / "gx-h", release / "gx-h", ignore=shutil.ignore_patterns(".venv", "var", "tests", "__pycache__"))
    shutil.copytree(ROOT / "bin", release / "bin")
    pkg = work / "plugin"
    package(pkg)
    pin = cli(release, "plugin", "digest", pkg)["sha256"]
    cli(release, "plugin", "install", pkg, "--sha256", pin)
    sources = []
    for side in ("left", "right"):
        path = work / (side + ".json")
        save(path, {"side": side, "barrier": str(work)})
        sources.append({"role": side, "path": str(path)})
    grant = {"project": "cpu-barrier", "plugins": [PLUGIN + "@1.0.0"], "capabilities": [METHOD], "input_files": sources,
             "initial_job_limit": 2, "turn_token_budget": 1000,
             "run_budget": {"job_submissions": 2, "cpu_seconds": 60, "worker_tasks": 8, "api_calls": 8,
                            "tokens": 16000, "gpu_jobs": 0, "gpu_minutes": 0, "wall_hours": 1}}
    save(work / "grant.json", grant)
    started = cli(release, "start", "--state", work / "project/state", "--run-key", "parallel-cpu",
                  "--goal", "Can two local CPU measurements finish across coordinator loss?", "--goal-config", work / "grant.json")
    for phase in ("dispatch", "resume"):
        child = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--work", str(work), "--phase", phase],
                               timeout=55, env={**os.environ, "GX_CODEX_OFFLINE": "1"})
        if child.returncode:
            raise RuntimeError(f"{phase} exited {child.returncode}")
    observed = json.loads((work / "observed.json").read_bytes())
    assert observed["run_id"] == started["run_id"]
    return observed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--phase", choices=["dispatch", "resume"])
    args = parser.parse_args()
    directory = args.work.resolve()
    if args.phase:
        coordinator(directory, args.phase)
    else:
        directory.mkdir(parents=True, exist_ok=False)
        print(json.dumps(experiment(directory), indent=2))
