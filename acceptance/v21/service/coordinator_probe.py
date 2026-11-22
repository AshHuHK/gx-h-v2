"""Exercise a paused run's inbox while another run uses the official App Server."""
from collections import defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT / "acceptance"), str(ROOT / "gx-h")]
from e2e_grid import fixture_env
from e2e_responses import local_responses
from v21_long_audit import percentile
from v21_long_run import CODEX, Staging, make_install, project_spec, save


def peers(pid):
    children = [pid]
    servers = set()
    while children:
        child = children.pop()
        proc = Path("/proc") / str(child)
        try:
            children.extend(map(int, (proc / "task" / str(child) / "children").read_text().split()))
            if (proc / "exe").resolve() == CODEX and b"app-server" in (proc / "cmdline").read_bytes().split(b"\0"):
                servers.add(child)
        except FileNotFoundError:
            pass
    return servers


def wait_for(site, operation, test):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with closing(site.db()) as conn:
            value = test(conn)
        if value:
            return value
        time.sleep(0.04)
    raise AssertionError(operation)


def run(work):
    stage, stage_hash = make_install(work, SimpleNamespace(lease=3600, rounds=3))
    (work / "tmp").mkdir()
    metrics = work / "metrics.jsonl"
    with local_responses(work / "requests.jsonl") as server:
        env = fixture_env(work, server, str(CODEX))
        env.update(PATH=str(ROOT / ".venv/bin") + ":" + env["PATH"], TMPDIR=str(work / "tmp"),
                   GX_METRICS_FILE=str(metrics))
        version = subprocess.check_output([CODEX, "--version"], text=True, env=env).strip()
        assert version == "codex-cli 0.169.0"
        site = Staging(work, stage, server, env, time.monotonic() + 90)
        try:
            for key in ("A", "B"):
                save(work / f"{key}.json", project_spec(key, 1))
                site.command("start-" + key, "start", "--spec", work / f"{key}.json", "--run-key", key)
            with closing(site.db()) as conn:
                rid = dict(conn.execute("SELECT run_key,run_id FROM p0_workspace_runs"))
            site.command("pause-A", "stop", "--run-id", rid["A"])
            server.fault_enabled = True
            driver, entry = site.command("workspace", "run", "--timeout", 70, background=True)
            assert server.fault_ready.wait(30), "B did not reach its official submission callback"
            original_peers = peers(driver.pid)
            assert len(original_peers) == 1, original_peers
            for action in ("status", "resume"):
                mid = "skipped-run-" + action
                save(work / f"{action}.json", {"kind": action})
                site.command("queue-" + action, "message", "--run-id", rid["A"], "--message-id", mid,
                             "--action", work / f"{action}.json", "--queue-only")
                def acknowledged(conn):
                    row = conn.execute("SELECT result_json,rejected FROM p0_operator_messages WHERE run_id=? "
                                       "AND message_id=?", (rid["A"], mid)).fetchone()
                    assert row and not row["rejected"], row
                    return row["result_json"]
                wait_for(site, f"A did not consume queued {action} while B had an open response", acknowledged)
            server.fault_release.set()
            site.reap(driver, entry, 0)
            assert not any((Path("/proc") / str(pid)).exists() for pid in original_peers)
            first = site.snapshot()
            assert len(first) == 2 and all(v["status"] == "COMPLETED" for v in first.values()), first
            count = server.request_count
            site.command("cold-completed", "run", "--timeout", 12)
            assert site.snapshot() == first and server.request_count == count
            with closing(site.db()) as conn:
                submissions = []
                for item in conn.execute("SELECT s.*,t.run_id FROM submissions s JOIN tasks t USING(task_id) ORDER BY s.rowid"):
                    task, submission = item["task_id"], item["submission_id"]
                    events = dict(conn.execute("SELECT new_state,event_id FROM events WHERE kind='task.transition' "
                                               "AND subject_id=? ORDER BY event_id", (task,)))
                    states = [events[s] for s in ("RUNNING", "SUBMITTED", "VALIDATING", "ACCEPTED")]
                    assert sorted(states) == states and len(set(states)) == 4
                    outbox, = conn.execute("SELECT payload_json FROM outbox WHERE topic='submission.accepted' AND "
                                          "json_extract(payload_json,'$.subject')=?", (submission,)).fetchall()
                    assert json.loads(outbox[0])["event_id"] == states[-1]
                    receipt, = conn.execute("SELECT c.call_id,r.run_id FROM p0_root_calls c JOIN p0_roots r "
                        "USING(thread_id) WHERE r.task_id=? AND json_extract(c.result_json,'$.submission_id')=?",
                        (task, submission)).fetchall()
                    assert receipt["run_id"] == item["run_id"]
                    submissions.append({"run_id": item["run_id"], "task_id": task, "submission_id": submission,
                                        "callback_id": receipt["call_id"], "transition_event_ids": states})
                assert len(submissions) == 4 and not list(conn.execute("PRAGMA foreign_key_check"))
            samples = defaultdict(list)
            for line in metrics.read_text().splitlines():
                sample = json.loads(line)
                samples[sample["kind"]].append(sample)
            duration = {}
            for kind in ("p0_callback", "db_transaction", "p0_dispatch"):
                assert samples[kind] and all(row["duration_ms"] >= 0 for row in samples[kind])
                duration[kind] = {"count": len(samples[kind]), "duration_ms": percentile(samples[kind], "duration_ms")}
            assert {row["run_id"] for row in samples["p0_callback"]} == set(rid.values())
            assert not any("error" in row for row in server.rows)
            return {"status": "PASS", "codex": version, "install_sha256": stage_hash,
                    "coordinator_py_sha256": hashlib.sha256((stage / "gx-h/gx_core/p0/coordinator.py").read_bytes()).hexdigest(),
                    "cli_commands": site.commands, "provider": "scripted loopback Responses",
                    "provider_requests": count, "external_model_requests": 0,
                    "replay_new_requests": server.request_count - count, "same_durable_snapshot": True,
                    "queued_status_and_resume_consumed_while_peer_open": True,
                    "observed_native_pids_closed": sorted(original_peers), "submissions": submissions,
                    "metrics": duration, "lock_acquire_ms": percentile(samples["db_transaction"], "lock_acquire_ms")}
        finally:
            server.fault_release.set()
            site.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="gx-v21-coordinator-") as directory:
        result = run(Path(directory))
    save(HERE / "coordinator.json", result)
    print(json.dumps({"status": result["status"], "metrics": result["metrics"]}))
