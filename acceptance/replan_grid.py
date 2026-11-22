"""Replace an objective in a live GX project through the official Codex main thread."""
import argparse
from contextlib import closing, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch

import e2e_responses
from e2e_audit import require, rows
from e2e_grid import PACKAGE, fixture_env, save
from gx_core.errors import ScopeDenied
from gx_core.p0.roots import require_binding


OBJECTIVE = "Measure whether any of the new positive points x=8 and x=9 qualifies."
NEW_PATH = "fresh-positive-points"


def edit(key, kind, old, value):
    return {"kind":"change","items":[{"item_id":key,"item_type":kind,"expected_version":old,"value":value}]}


def snapshot(state):
    with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro",uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        db = {
            "runs": rows(conn,"SELECT run_id,status FROM runs"),
            "jobs": rows(conn,"SELECT job_id,status,scheduler_ref,idempotency_key,input_manifest_ref,measured_json FROM jobs ORDER BY rowid"),
            "tasks": rows(conn,"SELECT task_id,stage,role,status,attempt,lease_id,input_json FROM tasks ORDER BY rowid"),
            "leases": rows(conn,"SELECT lease_id,task_id,principal,attempt,released_at FROM leases ORDER BY rowid"),
            "checkpoints": rows(conn,"SELECT checkpoint_id,seq,node,json_extract(state_json,'$.round') AS round FROM checkpoints ORDER BY seq"),
            "submissions": rows(conn,"SELECT submission_id,task_id,lease_id,result,artifact_refs_json FROM submissions ORDER BY rowid"),
            "settlements": rows(conn,"SELECT round_index,path_id,job_id,eval_id,kind,direction,evidence_id FROM science_settlements ORDER BY rowid"),
            "roots": rows(conn,"SELECT root_key,thread_id,task_id,lease_id,attempt,role FROM p0_roots ORDER BY rowid"),
            "human_inputs": rows(conn,"SELECT message_id,receipt_turn_id,consumed_at FROM p0_human_inputs ORDER BY rowid"),
            "human_receipts": rows(conn,"SELECT client_id,thread_id,turn_id,state FROM p0_root_deliveries WHERE human=1 ORDER BY rowid"),
            "decisions": rows(conn,"SELECT round_index,submission_id,reviewer_submission_id,actions_json,decision_ref FROM science_decisions"),
            "events": rows(conn,"SELECT event_id,kind,payload_json FROM events WHERE kind LIKE '%replan%' OR kind='workflow.reentered' ORDER BY event_id"),
            "cost":rows(conn,"SELECT spent_json,reserved_json FROM budgets ORDER BY rowid"),
            "artifacts":{},
        }
        for a in rows(conn, "SELECT artifact_ref,namespace,rel_path,kind,sha256,bytes FROM artifacts"):
            data = (state/"artifacts"/a["namespace"]/a["rel_path"]).read_bytes()
            require(len(data)==a["bytes"] and hashlib.sha256(data).hexdigest()==a["sha256"], "artifact identity mismatch")
            entry = {"kind":a["kind"],"sha256":a["sha256"]}
            if a["kind"] in {"research_proposal","research_report","synthetic_grid"}:
                entry["body"] = json.loads(data)
            db["artifacts"][a["artifact_ref"]] = entry
        return db


def run(out, codex, work_dir=None):
    out.mkdir(exist_ok=False,parents=True)
    summary = {"result":"FAIL","mode":"official Codex app-server with local scripted Responses and detached CPU Jobs",
               "codex":subprocess.check_output([codex,"--version"],text=True).strip(),
               "started":datetime.now(timezone.utc).isoformat(),"commands":[]}
    require(summary["codex"] == "codex-cli 0.169.0", "unexpected Codex version")
    try:
        with ExitStack() as stack:
            if work_dir is None:
                work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="gx-p0-replan-")))
            else:
                work = work_dir.resolve()
                work.mkdir(exist_ok=False,parents=True)
            state = work / "state"
            server = stack.enter_context(e2e_responses.local_responses(out/"requests.jsonl"))
            env = fixture_env(work,server,codex)
            def command(label, verb, *args, expected=0):
                argv = ["bin/gx-p0",verb,"--state",str(state),*args]
                job = subprocess.run(argv,env=env,cwd=PACKAGE,capture_output=True,text=True,timeout=130)
                if job.stderr:
                    (out/(label+".stderr")).write_text(job.stderr.replace(str(work),"$WORK"))
                summary["commands"].append({"label":label,"verb":verb,"exit":job.returncode})
                require(job.returncode == expected,f"{label}: exit {job.returncode}, expected {expected}: {job.stderr[:400]}")
                return json.loads(job.stdout) if job.stdout else {}
            def message(mid, action):
                path = work/(mid+".json")
                save(path,action)
                reply = command(mid,"message","--action",str(path),"--message-id",mid)
                return reply["result"]

            original = command("01-start","start","--demo-grid")
            run_id = original["run_id"]
            decision_ready, release = threading.Event(),threading.Event()
            original_action = e2e_responses.fixture_action
            def hold_decision(body, ident):
                item,metadata = original_action(body,ident)
                if metadata.get("output_kind")=="science_decision" and metadata.get("round")==0:
                    decision_ready.set()
                    release.wait(timeout=40)
                return item,metadata
            with patch.object(e2e_responses,"fixture_action",hold_decision):
                argv = ["bin/gx-p0","run","--state",str(state),"--timeout","90"]
                with subprocess.Popen(argv,env=env,cwd=PACKAGE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) as proc:
                    try:
                        until = time.monotonic()+65
                        while not decision_ready.wait(0.1):
                            require(proc.poll() is None and time.monotonic()<until,"original independent decision was never requested")
                        before = snapshot(state)
                        save(out/"02-before.json",before)
                        old_decision, = [r for r in before["roots"] if r["role"]=="planner"]
                        old_task = old_decision["task_id"]
                        require(len(before["jobs"])==2 and all(j["status"]=="SUCCEEDED" for j in before["jobs"]),"original Jobs did not finish")
                        require(len(before["settlements"])==2 and all(r["kind"]=="valid_scientific_outcome" for r in before["settlements"]),
                                "original assessment did not settle both hypotheses")
                        paused = command("03-pause","stop")
                        require(paused["status"]=="PAUSED","project did not pause")
                    finally:
                        release.set()
                    _,stderr = proc.communicate(timeout=45)
                    summary["commands"].append({"label":"02-original-run","verb":"run","exit":proc.returncode})
                    if stderr:
                        (out/"02-original-run.stderr").write_text(stderr.replace(str(work),"$WORK"))
                    require(proc.returncode==0,"original run failed after pause: "+stderr[-400:])
            preview = message("04-change-objective",edit("objective","objective",1,OBJECTIVE))
            require(preview["status"]=="awaiting_confirmation","objective replacement skipped review")
            confirmation = {"kind":"confirm","change_id":preview["change_id"],"preview_sha256":preview["preview_sha256"]}
            require(message("05-confirm-objective",confirmation)["status"]=="committed","objective change did not commit")
            command("06-missing-proposal","resume","--timeout","5",expected=1)
            require(command("06-still-paused","status")["status"]=="PAUSED","missing plan resumed obsolete work")
            value = {"kind":"replacement_proposal","objective":{"item_id":"objective","version":2},
                "evaluation":{"item_id":"evaluation","version":1},"proposal":{"threshold":-0.25,"paths":[
                    {"id":NEW_PATH,"prediction":"At least one score at x=8 or x=9 reaches -0.25.",
                     "grid":{"synthetic":True,"method_id":"fixture-grid","method_version":"1","candidates":[
                         {"candidate_id":"positive-eight","x":8},{"candidate_id":"positive-nine","x":9}]}}]}}
            require(message("07-proposal",edit("positive-grid","data_source",0,value))["status"]=="committed",
                    "main-thread replacement plan not committed")
            final = command("08-resume","resume","--timeout","100")
            require(final["status"]=="COMPLETED" and final["run_id"]==run_id,"same run did not finish")
            after = snapshot(state)
            save(out/"08-after.json",after)
            require([r["run_id"] for r in after["runs"]]==[run_id],"another project was created")
            require(all(r in after["jobs"] for r in before["jobs"]) and len(after["jobs"])==3,
                    "the two original jobs or their costs changed, or extra work was submitted")
            require(all(after["artifacts"][k]==v for k,v in before["artifacts"].items()),"historical artifacts changed")
            require(all(r in after["settlements"] for r in before["settlements"]),"historical findings changed")
            require(all(r in after["submissions"] for r in before["submissions"]),"old research products changed")
            new, = [s for s in after["settlements"] if s["path_id"]==NEW_PATH]
            require(new["direction"]=="refutes" and new["round_index"]==1,"new objective has no independent qualifying measurement")
            plans = [a["body"] for a in after["artifacts"].values() if a["kind"]=="research_proposal"]
            require(any(p.get("objective")==OBJECTIVE and [route["id"] for route in p["paths"]]==[NEW_PATH] for p in plans),
                    "wrong objective/path used for replacement computation")
            require(len(after["decisions"])==1 and after["decisions"][0]["round_index"]==1,"old decision affected the new route")
            event, = [json.loads(r["payload_json"]) for r in after["events"] if r["kind"]=="p0.objective_replanned"]
            require(event["previous_checkpoint"] in {ck["checkpoint_id"] for ck in after["checkpoints"]}
                    and event["checkpoint"] in {ck["checkpoint_id"] for ck in after["checkpoints"]}
                    and event["previous_proposal_ref"] in before["artifacts"]
                    and event["objective"]==value["objective"] and event["evaluation"]==value["evaluation"]
                    and event["proposal_source"]["item_id"]=="positive-grid",
                    "new research lacks a verified link to its approved replacement and old checkpoint")
            report = after["artifacts"][final["report_ref"]]["body"]
            require([r["round"] for r in report["rounds"]]==[1],"current report contains results under the old objective")
            require(sum(r["task_id"]==old_task and r["status"]=="CANCELLED" for r in after["tasks"])==1,
                    "superseded planner task is still open")
            decision = after["decisions"][0]
            accepted = {s["submission_id"]:s for s in after["submissions"] if s["result"]=="ACCEPTED"}
            subjects = {r["lease_id"]:r["principal"] for r in after["leases"]}
            reviewer = subjects[accepted[decision["reviewer_submission_id"]]["lease_id"]]
            planner = subjects[accepted[decision["submission_id"]]["lease_id"]]
            require(len({reviewer,planner,old_decision["thread_id"]})==3,"decision and original/replacement review identities overlap")
            require(not any(s["task_id"]==old_task and s["result"]=="ACCEPTED" for s in after["submissions"]),
                    "superseded decision produced a formal submission")
            with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro",uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                try:
                    require_binding(conn,run_id,old_decision["thread_id"])
                except ScopeDenied:
                    pass
                else:
                    raise AssertionError("old root still has write access")
            old_lease, = [r for r in after["leases"] if r["lease_id"]==old_decision["lease_id"]]
            require(old_lease["released_at"],"old attempt lease has not been fenced")
            ids = {r["client_id"]:r for r in after["human_receipts"]}
            clients = ["04-change-objective","05-confirm-objective","07-proposal"]
            require(len({ids[k]["thread_id"] for k in clients})==1 and len({ids[k]["turn_id"] for k in clients})==3
                    and all(ids[k]["state"]=="acknowledged" for k in clients),"main-turn provenance missing")
            requests = server.request_count
            replay = command("09-replay","run","--timeout","25")
            require(replay["status"]=="COMPLETED" and requests==server.request_count and snapshot(state)==after,
                    "reopening a finished replan changed work")
            summary.update(result="PASS",run_id=run_id,initial_jobs=2,replacement_jobs=1,
                           measurement_direction="refutes",replanned_round=new["round_index"],
                           old_task_id=old_task,old_lease=old_lease["lease_id"],new_reviewer=reviewer,new_planner=planner,
                           objective_change_id=preview["change_id"],main_receipts={k:ids[k] for k in clients},
                           job_cpu_costs=[json.loads(j["measured_json"]) for j in after["jobs"]],
                           request_count=requests,external_requests=0,gpu_seconds=0,external_cost_usd=0,
                           reported_fixture_tokens=35*requests,warm_replay_additional_calls=0)
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        summary["ended"] = datetime.now(timezone.utc).isoformat()
        save(out/"summary.json",summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--work",type=Path)
    parser.add_argument("--codex",default=os.environ.get("GX_P0_CODEX","codex"))
    args = parser.parse_args()
    run(args.output.resolve(),shutil.which(args.codex),args.work)
