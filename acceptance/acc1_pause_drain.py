"""Stop the public CLI after a real planner submission while its last usage receipt is in flight."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch

import e2e_responses
from e2e_grid import fixture_env

PACKAGE = Path(__file__).resolve().parents[1]


def state_at(state, task):
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        def rows(sql, args=()):
            return [dict(r) for r in conn.execute(sql,args)]
        return {
            "run": rows("SELECT run_id,status,workflow_version,workflow_sha256 FROM runs")[0],
            "planner": rows("SELECT task_id,status,attempt FROM tasks WHERE task_id=?",(task,))[0],
            "submission": rows("SELECT submission_id,result,attempt,content_sha256 FROM submissions "
                               "WHERE task_id=?",(task,))[0],
            "turn": rows("SELECT r.thread_id,d.turn_id,b.reservation_ref,b.status,b.settled_at,u.total_tokens,"
                         "u.terminal_status FROM p0_roots r JOIN p0_root_deliveries d USING(thread_id) "
                         "JOIN budget_reservations b USING(reservation_ref) LEFT JOIN p0_root_turn_usage u "
                         "USING(thread_id,turn_id) WHERE d.method='turn/start' AND r.task_id=?",(task,))[0],
            "ledger": rows("SELECT spent_json,reserved_json FROM budgets ORDER BY rowid"),
            "paid_turn_count": conn.execute("SELECT count(*) FROM budget_reservations WHERE subject_kind='native_turn'").fetchone()[0],
            "job_count": conn.execute("SELECT count(*) FROM jobs").fetchone()[0],
            "submission_count": conn.execute("SELECT count(*) FROM submissions").fetchone()[0],
        }


def run(folder, report):
    state = folder / "project"
    waiting, release = threading.Event(), threading.Event()
    target = {}
    fixture = e2e_responses.fixture_action
    def delayed_planner_reply(body, ident):
        item, facts = fixture(body, ident)
        if facts.get("round") == 0 and facts.get("output_kind") == "science_decision":
            if facts.get("tool") == "gx_submit_product":
                # Leave a deterministic interval for the stop, before the next review could be due.
                args = json.loads(item["arguments"])
                for grid in args["product"]["next_grids"].values():
                    grid["delay_seconds"] = 5
                item = {**item, "arguments":json.dumps(args)}
            elif item.get("type") == "message":
                target["task_id"] = facts["task_id"]
                target["request_id"] = ident
                waiting.set()
                if not release.wait(25):
                    raise TimeoutError("the test operator did not release the final provider reply")
        return item, facts
    with e2e_responses.local_responses(folder/"requests.jsonl") as peer, patch.object(e2e_responses, "fixture_action", delayed_planner_reply):
        binary = shutil.which("codex")
        env = fixture_env(folder, peer, binary)
        report["codex"] = subprocess.check_output([binary,"--version"],env=env,text=True).strip()
        assert report["codex"] == "codex-cli 0.169.0"
        def command(action, flags=()):
            return ["bin/gx-p0",action,"--state",str(state),*flags]
        def invoke(action, flags=()):
            output = subprocess.run(command(action,flags),cwd=PACKAGE,env=env,text=True,capture_output=True,timeout=30)
            if output.returncode:
                raise RuntimeError(f"{action} failed: {output.stderr}")
            return json.loads(output.stdout)
        started = invoke("start",["--demo-grid"])
        with subprocess.Popen(command("run",["--timeout","50"]),cwd=PACKAGE,env=env,text=True,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE) as driver:
            try:
                if not waiting.wait(25):
                    raise AssertionError("planner did not submit before the deadline")
                task = target["task_id"]
                before = report["before_stop"] = state_at(state,task)
                assert before["planner"]["status"] == before["submission"]["result"] == "ACCEPTED"
                assert before["turn"]["status"] == "HELD" and before["turn"]["terminal_status"] is None
                assert started["run_id"] == before["run"]["run_id"]
                admitted = peer.request_count
                then = time.monotonic()
                paused = invoke("stop")
                report["stop_ack"] = {"status":paused["status"],"seconds":round(time.monotonic()-then,3),
                        "before_provider_release":not release.is_set(),"new_model_requests":peer.request_count-admitted}
                assert paused["status"] == "PAUSED" and not release.is_set() and peer.request_count == admitted
                # The old Driver closes the live official pipe on its next loop. A draining
                # Driver remains until the other process's outstanding paid reply completes.
                try:
                    driver.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                report["background_running_before_provider_release"] = driver.poll() is None
                report["while_stopped"] = state_at(state,task)
                report["new_requests_during_pause"] = peer.request_count-admitted
            finally:
                release.set()
            output, err = driver.communicate(timeout=25)
            if driver.returncode:
                raise RuntimeError(f"background run failed: {err}")
            acknowledged = json.loads(output)
        after = report["after_drain"] = state_at(state,task)
        report.update(final_request=target["request_id"],new_requests_while_draining=peer.request_count-admitted,
                driver_exit_status=acknowledged["status"],driver_exit_unsettled_turns=acknowledged["unsettled_turns"],
                planner_provider_requests=sum(r.get("task_id") == task for r in peer.rows),
                total_provider_requests=peer.request_count)
        assert report["background_running_before_provider_release"], "Driver exited before the admitted planner's final usage arrived"
        assert report["while_stopped"]["turn"]["status"] == "HELD"
        assert report["while_stopped"]["turn"]["turn_id"] == before["turn"]["turn_id"] == after["turn"]["turn_id"]
        assert after["turn"]["reservation_ref"] == before["turn"]["reservation_ref"]
        assert after["turn"]["terminal_status"] == "completed" and after["turn"]["status"] == "SETTLED"
        assert after["turn"]["total_tokens"] == report["planner_provider_requests"]*35
        assert after["planner"] == before["planner"] and after["submission"] == before["submission"]
        assert after["paid_turn_count"] == before["paid_turn_count"]
        assert acknowledged["status"] == "PAUSED" and not acknowledged["unsettled_turns"]
        assert sum(json.loads(r["spent_json"]).get("tokens",0) for r in after["ledger"]) == peer.request_count*35
        assert not report["new_requests_during_pause"] and not report["new_requests_while_draining"]
        count = peer.request_count
        cold = invoke("run",["--timeout","8"])
        report["cold_new_model_requests"] = peer.request_count-count
        report["cold_state_unchanged"] = state_at(state,task) == after
        assert cold["status"] == "PAUSED" and cold["unsettled_turns"] == []
        assert report["cold_new_model_requests"] == 0 and report["cold_state_unchanged"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",type=Path,required=True)
    args = parser.parse_args()
    driver = PACKAGE/"gx-h/gx_core/p0/driver.py"
    report = {"mode":"official Codex; scripted localhost Responses; real CPU grid Jobs",
              "entry":"bin/gx-p0 start, concurrent run and stop, cold run",
              "driver_sha256":hashlib.sha256(driver.read_bytes()).hexdigest(),
              "started":datetime.now(timezone.utc).isoformat(),"result":"FAIL"}
    try:
        with tempfile.TemporaryDirectory(prefix="gx-acc1-pause-drain-") as temporary:
            run(Path(temporary), report)
        report["result"]="PASS"
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["ended"]=datetime.now(timezone.utc).isoformat()
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(report,indent=2)+"\n")
