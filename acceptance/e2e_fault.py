"""Kill only the official native app-server descended from the running test CLI."""

from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time


def observed_state(state):
    db, = state.rglob("gx.db")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        def rows(sql):
            return [dict(r) for r in conn.execute(sql)]
        return {"runs": rows("SELECT run_id,status FROM runs ORDER BY rowid"),
                "jobs": rows("SELECT job_id,status,scheduler_ref FROM jobs ORDER BY rowid"),
                "bindings": rows("SELECT r.task_id,r.attempt,r.thread_id,t.status AS task_status "
                                  "FROM p0_roots r LEFT JOIN tasks t USING(task_id) ORDER BY r.rowid"),
                "deliveries": rows("SELECT thread_id,turn_id,method,state FROM p0_root_deliveries ORDER BY rowid"),
                "submissions": rows("SELECT submission_id,task_id,result FROM submissions ORDER BY rowid")}


def active_server_pid(root_pid):
    # /proc's children list scopes the signal to this fixture's owned tree. Node
    # may wrap the pinned ELF, so match the native executable as well as argv.
    remaining = [root_pid]
    found = []
    while remaining:
        pid = remaining.pop()
        path = Path("/proc") / str(pid)
        try:
            remaining.extend(int(n) for n in (path/"task"/str(pid)/"children").read_text().split())
            if (path/"exe").resolve().name != "codex":
                continue
            if b"app-server" in (path/"cmdline").read_bytes().split(b"\0"):
                found.append(pid)
        except FileNotFoundError:
            continue
    if len(found) != 1:
        raise RuntimeError(f"expected one owned Codex app-server, found {len(found)}")
    return found[0]


def interrupt(args, *, command, state, server, env, package, out):
    server.fault_enabled = True
    with subprocess.Popen(command, env=env, cwd=package, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        try:
            deadline = time.monotonic() + args.timeout
            while not server.fault_ready.wait(0.1):
                if proc.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("CLI did not reach the first reviewer's submit request")
            before = observed_state(state)
            pid = active_server_pid(proc.pid)
            os.kill(pid, signal.SIGKILL)
            killed = time.monotonic()
            server.fault_release.set()
            stdout, stderr = proc.communicate(timeout=args.timeout + 20)
            after = observed_state(state)
        finally:
            server.fault_release.set()
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        (out/"faulted-run.stdout").write_text(stdout)
        (out/"faulted-run.stderr").write_text(stderr)
        fault = {"injection": "SIGKILL while first reviewer submit tool call was pending at the Responses endpoint",
                 "app_server_pid": pid, "cli_returncode": proc.returncode,
                 "seconds_from_injection_to_cli_exit": round(time.monotonic() - killed, 3),
                 "before": before, "after_cli_exit": after}
        (out/"fault.json").write_text(json.dumps(fault, indent=2) + "\n")
        return fault
