"""Run the static branch acceptance on an isolated private deployment and loopback Responses peer."""
import argparse
from collections import Counter
from unittest.mock import patch
from contextlib import closing, ExitStack
import gzip
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from types import SimpleNamespace


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(REPO / "gx-h"), str(REPO / "acceptance")]
from native_probe import Probe, find_binary
from native_probe_peer import Peer
from gx_core import metrics
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.native_release import native_state
from gx_core.orchestration import service
from gx_core.p0 import cli, driver as driver_module, operator, schema
from gx_core.p0.driver import Driver, context
from gx_core.p0.profiles import CODEX_VERSION, clash_env
from gx_core.persistence import store
from static_case import OPERATION, WORKFLOW, install, observe


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def get(path):
    return json.loads(path.read_text())


def until(predicate, label, *, process=None, timeout=35):
    start = time.monotonic()
    while time.monotonic() < start + timeout:
        result = predicate()
        if result:
            return result
        if process and process.poll() is not None:
            raise RuntimeError(f"{label}: GX exited {process.returncode}, see its log")
        time.sleep(.025)
    raise TimeoutError(label)


def trace_spawns(path):
    # All GX launches of AppServerClient use stdlib Popen. Log argv but never its environment.
    def audit(event, args):
        if event in ("subprocess.Popen", "os.exec", "os.posix_spawn", "os.system"):
            argv = args[1] if len(args) > 1 else args[0]
            with path.open("a") as stream:
                print(json.dumps({"ns": time.time_ns(), "pid": os.getpid(), "api": event,
                                  "argv": [os.fsdecode(a) for a in argv] if isinstance(argv, (tuple, list))
                                          else os.fsdecode(argv)}), file=stream)
    sys.addaudithook(audit)


def codex_processes(executable):
    """Inspect this fixture network namespace only. May run alongside unrelated native hosts."""
    namespace = os.readlink("/proc/self/ns/net")
    rows = []
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if os.readlink(process / "ns/net") != namespace:
                continue
            if os.readlink(process / "exe") != executable:
                continue
            stat = (process / "stat").read_text().rsplit(")", 1)[-1].split()
            rows.append({"pid": int(process.name), "ppid": int(stat[1]), "start_ticks": int(stat[19]),
                         "state": stat[0], "net_namespace": namespace})
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return rows


def cutoff(proc, out):
    until((out / "stop-host").exists, "parent has not requested the host cut")
    assert proc.poll() is None
    binary = os.readlink(f"/proc/{proc.pid}/exe")
    live = codex_processes(binary)
    if {p["pid"] for p in live} != {proc.pid}:
        raise RuntimeError(f"The shutdown fixture must own the only Codex process in its network namespace: {live}")
    proc.kill()
    exit_code = proc.wait(5)
    assert exit_code == -signal.SIGKILL
    assert not Path(f"/proc/{proc.pid}").exists()
    after = codex_processes(binary)
    assert not after, after
    os.environ["GX_CODEX_OFFLINE"] = "1"
    save(out / "host-stopped.json", {"cut_ns": time.time_ns(), "signal": "SIGKILL", "exit_code": exit_code,
                                     "codex_pid": proc.pid, "gx_pid": os.getpid(),
                                     "live_before": live, "live_after": after})


def first_driver(conn, state, out, case):
    spec = {"project": "persistent-grid-" + case, "objective": "Measure the synthetic grid and await a planner decision.",
            "max_rounds": 1, "turn_token_budget": 128}
    with ExitStack() as stack:
        def prepare(run_id):
            initial = cli.initial_action(spec, service.default_registry().get(WORKFLOW, "1").hooks.research_start(spec))
            applied = operator.apply_bootstrap(conn, run_id, initial, str(uuid.uuid4()))
            assert applied["status"] == "committed", applied
            driver = Driver(conn, state.directory, run_id)
            stack.callback(driver.close)
            host = driver.native
            turn = host.human_turn("Wait for my next input.", message_id=str(uuid.uuid4()))
            proc = host.client.transport.process
            save(out / "host-ready.json", {"run_id": run_id, "turn_id": turn, "codex_pid": proc.pid,
                                          "gx_pid": os.getpid(), "exe": os.readlink(f"/proc/{proc.pid}/exe"),
                                          "argv": list(host.client.argv), "ts_ns": time.time_ns()})
            if case == "restart":
                cutoff(proc, out)
            return proc

        child = []
        outcome = service.run_start(context(conn), {"objective": spec["objective"], "project_ref": spec["project"],
                "workflow": {"id": WORKFLOW, "version": "1"}, "budget_profile": "p0",
                "idempotency_key": "phase2a-start", "inputs": {"p0": True, "p0_spec": spec}},
                workspace_key=case, prepare_p0=lambda rid: child.append(prepare(rid)))
        run_id = outcome["run_id"]
        save(out / "mixed.json", observe(conn, run_id))
        if case == "steady":
            cutoff(child[0], out)
    return run_id


def drive(conn, state, run_id, seconds, *, measure_sleeps=False):
    wakeups = []
    def timed_sleep(delay):
        started = time.monotonic_ns()
        time.sleep(delay)
        wakeups.append({"requested_seconds": delay, "elapsed_seconds": (time.monotonic_ns() - started) / 1e9,
                        "woke_at_ns": time.time_ns()})
    r0 = resource.getrusage(resource.RUSAGE_SELF)
    start = time.monotonic_ns()
    driver = Driver(conn, state.directory, run_id)
    try:
        with ExitStack() as instruments:
            if measure_sleeps:
                clock = SimpleNamespace(**{**vars(time), "sleep": timed_sleep})
                instruments.enter_context(patch.object(driver_module, "time", clock))
            instruments.enter_context(metrics.identity(run_id=run_id))
            result = driver.drive(seconds)
    finally:
        driver.close()
    r1 = resource.getrusage(resource.RUSAGE_SELF)
    return {"wall_seconds": (time.monotonic_ns() - start) / 1e9,
            "process_cpu_seconds": (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime),
            "voluntary_context_switches": r1.ru_nvcsw - r0.ru_nvcsw,
            "involuntary_context_switches": r1.ru_nivcsw - r0.ru_nivcsw, "pid": os.getpid(),
            "sleep_wakeups": wakeups, "driver": result.get("driver"), "status": result["status"]}


def worker(args):
    out, phase = args.output, args.worker
    audit = out / ("exec-" + phase + ".jsonl")
    audit.touch(exist_ok=False)
    trace_spawns(audit)
    install(args.private)
    with native_state(args.private / "state") as state, closing(store.open_store(state.db_path)) as conn:
        schema.install(conn)
        if phase == "first":
            run_id = first_driver(conn, state, out, args.case)
        else:
            run_id = get(out / "host-ready.json")["run_id"]
        if phase == "recovery":
            save(out / "restored.json", observe(conn, run_id))
        durations = {"idle": 5, "recovery": 2, "first": 3 if args.case == "steady" else 30}
        save(out / (phase + "-enter.json"), {"run_id": run_id, "pid": os.getpid(), "ns": time.time_ns(),
                "db_path": str(state.db_path), "db_inode": state.db_path.stat().st_ino,
                "db_device": state.db_path.stat().st_dev})
        cpu = drive(conn, state, run_id, durations[phase], measure_sleeps=phase == "idle")
        final = observe(conn, run_id)
        hosts = codex_processes(get(out / "host-ready.json")["exe"])
        assert not hosts, hosts
        save(out / (phase + "-end.json"), {"state": final, "cpu": cpu, "official_host_pids": hosts})
        if phase != "idle":
            assets = ArtifactService(conn)
            raw = final["outputs"]["B1"]["raw_ref"]
            (out / "observations.raw.json").write_bytes(assets.read_bytes(raw))
            summary = final["outputs"]["B2"]["ref"]
            (out / "summary.raw.json").write_bytes(assets.read_bytes(summary))


def snapshot(db, out, name):
    """SQLite's backup API takes a coherent image, including committed WAL frames."""
    temp = out / (name + ".db")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(temp)) as copy:
        source.backup(copy)
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    with temp.open("rb") as raw, gzip.open(temp.with_suffix(".db.gz"), "wb") as dest:
        shutil.copyfileobj(raw, dest)
    temp.unlink()


def read_events(db, out):
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        with (out / "events.jsonl").open("w") as stream:
            for row in conn.execute("SELECT * FROM events ORDER BY event_id"):
                print(json.dumps(dict(row)), file=stream)


def spawn_worker(stack, out, private, case, phase, env, commands):
    argv = [sys.executable, str(Path(__file__).resolve()), "--output", str(out), "--private", str(private),
            "--case", case, "--worker", phase]
    child_env = {**env, "GX_METRICS_FILE": str(out / ("metrics-" + phase + ".jsonl"))}
    if phase != "first":
        child_env["GX_CODEX_OFFLINE"] = "1"
    log = stack.enter_context((out / (phase + ".log")).open("w"))
    proc = subprocess.Popen(argv, env=child_env, stdout=log, stderr=subprocess.STDOUT)
    def cleanup():
        if proc.poll() is None:
            proc.kill()
        proc.wait(5)
    stack.callback(cleanup)
    commands.append({"command": argv, "gx_pid": proc.pid, "phase": phase, "start_ns": time.time_ns()})
    return proc


def finish(proc, command, expected):
    code = proc.wait(35)
    command.update(exit=code, end_ns=time.time_ns())
    if code != expected:
        raise AssertionError(f"{command['phase']} GX exited {code}, expected {expected}; inspect log")


def case_run(out, private, case, env, peer):
    out.mkdir()
    private.mkdir()
    commands = []
    with ExitStack() as stack:
        first = spawn_worker(stack, out, private, case, "first", env, commands)
        host = until(lambda: get(out / "host-ready.json") if (out / "host-ready.json").exists() else None,
                     "actual native turn acknowledgement", process=first)
        until(lambda: peer.rows, "loopback request")
        db = private / "state" / "gx.db"
        if case == "steady":
            until((out / "mixed.json").exists, "A and B parked", process=first)
            mixed = get(out / "mixed.json")
            outcome = private / "state" / "jobs" / mixed["jobs"][0]["job_id"] / "outcome.json"
            until(outcome.exists, "detached job finished while Driver has yet to reconcile", process=first)
            shutil.copyfile(outcome, out / "runner.outcome.json")
        snapshot(db, out, "before-host-stop")
        (out / "stop-host").touch()
        until((out / "first-enter.json").exists, "first Driver entered", process=first)
        mixed = get(out / "mixed.json")
        snapshot(db, out, "mixed")
        if case == "restart":
            def has_cycle():
                path = out / "metrics-first.jsonl"
                return path.exists() and '"kind":"p0_driver_cycle"' in path.read_text()
            until(has_cycle, "first real offline Driver cycle", process=first)
            job = mixed["jobs"][0]
            outcome = private / "state" / "jobs" / job["job_id"] / "outcome.json"
            assert not outcome.exists(), "runner ended before the GX fault"
            first.kill()
            finish(first, commands[-1], -signal.SIGKILL)
            snapshot(db, out, "after-gx-kill")
            assert Path(f"/proc/{job['runner_pid']}").exists()
            until(outcome.exists, "recorded detached runner completes with no GX process")
            shutil.copyfile(outcome, out / "runner.outcome.json")
            proc = spawn_worker(stack, out, private, case, "recovery", env, commands)
            finish(proc, commands[-1], 0)
            final = get(out / "recovery-end.json")["state"]
            snapshot(db, out, "recovered")
            proc = spawn_worker(stack, out, private, case, "idle", env, commands)
            finish(proc, commands[-1], 0)
        else:
            finish(first, commands[-1], 0)
            final = get(out / "first-end.json")["state"]
        save(out / "commands.json", commands)
        snapshot(db, out, "after")
        read_events(db, out)
        return check(out, case, mixed, final, host, commands)


def check(out, case, mixed, final, host, commands):
    stopped = get(out / "host-stopped.json")
    cut = stopped["cut_ns"]
    assert stopped["gx_pid"] == host["gx_pid"] == commands[0]["gx_pid"]
    assert stopped["codex_pid"] == host["codex_pid"] and stopped["exit_code"] == -9
    before, after = mixed["engine"], final["engine"]
    pre = before["node_states"]
    assert pre["A1"] == "WAITING_DECISION" and pre["B1"] == "WAITING_JOB" and pre["C1"] == "COMPLETED", pre
    c1_seen = mixed["outputs"]["C1"]["states_at_C1"]
    assert (c1_seen["A1"], c1_seen["B1"]) == ("WAITING_DECISION", "WAITING_JOB"), c1_seen
    for node, expected in {"A1": "WAITING_DECISION", "A2": "BLOCKED", "B1": "COMPLETED", "B2": "COMPLETED",
                           "B3": "COMPLETED", "C1": "COMPLETED"}.items():
        assert after["node_states"][node] == expected, after
    assert before["run_id"] == host["run_id"] == after["run_id"] and after["status"].startswith("WAITING")
    assert mixed["tasks"] == final["tasks"] and mixed["tasks"][0]["status"] == "READY"
    assert before["node_waits"]["A1"] == after["node_waits"]["A1"]
    assert before["node_waits"]["A1"]["reason"] and before["node_waits"]["A1"]["owner"] and before["node_waits"]["A1"]["since"]
    assert mixed["tasks"][0]["task_id"] in before["node_waits"]["A1"]["references"]
    assert len(mixed["usage"]) == len(final["usage"]) == 1 and mixed["usage"] == final["usage"]
    assert final["usage"][0]["status"] == "HELD" and final["usage"][0]["turn_id"] == host["turn_id"]
    assert len(mixed["jobs"]) == len(final["jobs"]) == 1
    job0, job = mixed["jobs"][0], final["jobs"][0]
    for key in ("job_id", "scheduler_ref", "runner_pid", "runner_started", "reservation_ref"):
        assert job0[key] == job[key] and job[key] is not None
    assert job["status"] == "SUCCEEDED" and job["exit_code"] == 0
    assert job0["job_id"] in before["node_waits"]["B1"]["references"]
    job_settlements = [r for r in final["settlements"] if r["ref"] == job["reservation_ref"]]
    assert len(job_settlements) == 1
    native_waits = [json.loads(r["payload_json"]) for r in final["outbox"]
                    if r["topic"] == "p0.native.wait" and r["consumed_at"] is None]
    for owner, reference in (("usage", final["usage"][0]["thread_id"]), ("task", final["tasks"][0]["task_id"])):
        [wait] = [w for w in native_waits if (w["owner"], w["reference"]) == (owner, reference)]
        assert wait["reason"] and wait["since"] and "next_retry_at" in wait
    terminal = [r for r in final["outbox"] if r["topic"] == "job.terminal"]
    assert len(terminal) == 1 and terminal[0]["consumed_at"]
    expected_nodes = {"admit", "B1", "B2", "B3", "C1"}
    counts = Counter(row["node"] for row in final["completed"])
    assert counts == dict.fromkeys(expected_nodes, 1)
    refs = {row["artifact_ref"]: row for row in final["artifacts"]}
    raw = refs[final["outputs"]["B1"]["raw_ref"]]
    assert raw["job_id"] == job["job_id"] and ids.sha256_of_file(out / "observations.raw.json") == raw["sha256"]
    runner = get(out / "runner.outcome.json")
    assert (runner["job_id"], runner["runner_pid"], runner["runner_started"], runner["exit_code"]) == (
        job["job_id"], job["runner_pid"], job["runner_started"], 0)
    [produced] = [f for f in runner["files"] if f["path"] == "observations.json"]
    assert produced["sha256"] == raw["sha256"]
    assert ids.sha256_of_file(out / "summary.raw.json") == final["outputs"]["B2"]["sha256"]
    assert get(out / "observations.raw.json")["operation_id"] == OPERATION
    epochs = [json.loads(x) for x in (out / "events.jsonl").read_text().splitlines()]
    published = Counter(row["subject_id"] for row in epochs if row["kind"] == "artifact.published")
    assert published[raw["artifact_ref"]] == published[final["outputs"]["B2"]["ref"]] == 1
    duplicates = {"node": sum(n - 1 for n in counts.values()), "artifact": sum(n - 1 for n in published.values()),
                  "settlement": sum(n - 1 for n in Counter(r["ref"] for r in final["settlements"]).values())}
    assert not any(duplicates.values())
    execs = [json.loads(line) for p in out.glob("exec-*.jsonl") for line in p.read_text().splitlines()]
    launches = [e for e in execs if "app-server" in e["argv"] or Path(e["argv"][0]).name == "codex"]
    assert len(launches) == 1 and launches[0]["ns"] < cut, launches
    assert stopped["live_before"][0]["pid"] == host["codex_pid"] and stopped["live_after"] == []
    cut_execs = [e for e in execs if e["ns"] >= cut]
    setup_execs = [e for e in execs if e["ns"] < cut]
    audit = {"before_cut": setup_execs, "after_cut": cut_execs, "host": stopped,
             "loopback_request_count": 1}
    save(out / "processes.json", audit)
    from datetime import datetime
    off_completed = [json.loads(e["payload_json"])["node"] for e in epochs if e["kind"] == "workflow.node_completed"
                     and datetime.fromisoformat(e["ts"]).timestamp() >= (cut // 1_000_000) / 1000]
    assert {"B2", "B3"}.issubset(off_completed)
    idle = None
    if case == "restart":
        restored = get(out / "restored.json")
        assert len(set(c["gx_pid"] for c in commands)) == 3
        identities = [get(out / (p + "-enter.json")) for p in ("first", "recovery", "idle")]
        assert len({(r["db_path"], r["db_inode"], r["db_device"]) for r in identities}) == 1
        assert restored["engine"]["node_waits"] == before["node_waits"]
        assert restored["jobs"][0]["job_id"] == job0["job_id"] and restored["usage"] == mixed["usage"]
        assert restored["completed"] == mixed["completed"] and restored["tasks"] == mixed["tasks"]
        idle_result = get(out / "idle-end.json")
        for key in ("completed", "artifacts", "settlements", "jobs", "usage", "tasks"):
            assert idle_result["state"][key] == final[key], key
        idle = idle_result["cpu"]
        measurements = [json.loads(s) for s in (out / "metrics-idle.jsonl").read_text().splitlines()]
        idle["driver_cycles"] = sum(m["kind"] == "p0_driver_cycle" for m in measurements)
        assert 1 <= len(idle["sleep_wakeups"]) <= idle["driver_cycles"] + 1, idle
        assert 1 <= idle["driver_cycles"] <= 24 and idle["process_cpu_seconds"] < idle["wall_seconds"] * .5, idle
        assert "C1" in off_completed
    return {"run_id": after["run_id"], "branch_nodes": {"A": ["A1", "A2"], "B": ["B1", "B2", "B3"], "C": ["C1"]},
            "job_id": job["job_id"], "task_id": final["tasks"][0]["task_id"], "wait": after["node_waits"]["A1"],
            "held_usage": final["usage"], "job_settlement": job_settlements, "terminal_outbox": terminal,
            "raw_artifact": raw, "commands": commands, "cut_ns": cut, "codex_spawns_after_stop": 0,
            "scoped_native_waits": native_waits, "audited_execs": len(execs), "duplicates": duplicates, "offline_deterministic_completions": off_completed,
            "idle": idle}


def main(out):
    if out.exists() and any(out.iterdir()):
        raise ValueError("Use a fresh output directory")
    out.mkdir(parents=True, exist_ok=True)
    git = lambda *argv: subprocess.check_output(["git", "-C", str(REPO), *argv], text=True).strip()
    source = {"commit": git("rev-parse", "HEAD"), "dirty": git("status", "--short"), "command": [sys.executable, *sys.argv]}
    save(out / "source.json", source)
    env = clash_env()
    binary = Path(os.environ.get("GX_P0_CODEX") or find_binary()).resolve()
    version = subprocess.check_output([binary, "--version"], text=True).strip()
    assert version == "codex-cli " + CODEX_VERSION, version
    cases = {}
    for name in ("steady", "restart"):
        with tempfile.TemporaryDirectory(prefix="gx2a-") as temp, Peer() as peer:
            work = Path(temp)
            probe = Probe(work / "host", binary, peer)
            peer.gate.clear()
            listener = threading.Thread(target=peer.serve_forever, daemon=True)
            listener.start()
            try:
                run_env = {**probe.env, **{k: v for k, v in env.items() if k.lower().endswith("_proxy")},
                           "GX_P0_TEST": "1", "GX_P0_TEST_PROVIDER": probe.provider,
                           "GX_P0_CODEX": str(binary), "TMPDIR": str(work), "PYTHONDONTWRITEBYTECODE": "1"}
                cases[name] = case_run(out / name, work / "gx", name, run_env, peer)
                assert len(peer.rows) == 1, peer.rows
            finally:
                peer.gate.set()
                peer.shutdown()
                listener.join(5)
    result = {"source": source, "cases": cases, "codex_binary": str(binary), "codex_version": version,
              "codex_sha256": ids.sha256_of_file(binary), "idle": cases["restart"]["idle"],
              "codex_spawns_after_stop": sum(c["codex_spawns_after_stop"] for c in cases.values()),
              "duplicates": {k: sum(c["duplicates"][k] for c in cases.values()) for k in ("node", "artifact", "settlement")},
              "offline_deterministic_completions": {k: c["offline_deterministic_completions"] for k,c in cases.items()},
              "exit_code": 0, "evidence_class": "SCRIPTED; real OS processes, official host on loopback, real local grid Job"}
    checks = {"01": "steady", "02": "restart", "03": "steady", "04": "restart", "05": "restart",
              "06": "restart", "07": "restart", "14": "restart"}
    result["matrix"] = {"P2A-" + n: {"case": case, "run_id": cases[case]["run_id"], "state": "PASS",
                                      "evidence": case + "/events.jsonl"} for n, case in checks.items()}
    save(out / "observations.json", result)
    print(json.dumps({"matrix": list(result["matrix"]), "duplicates": result["duplicates"], "idle": result["idle"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--private", type=Path)
    parser.add_argument("--case", choices=("steady", "restart"))
    parser.add_argument("--worker", choices=("first", "recovery", "idle"))
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.worker:
        worker(args)
    else:
        try:
            main(args.output)
        except Exception:
            # A caller supplying an existing evidence directory must never overwrite its result.
            if (args.output / "failure.json").exists() or (args.output / "observations.json").exists():
                raise
            save(args.output / "failure.json", {"exit_code": 1, "error": traceback.format_exc()})
            raise
