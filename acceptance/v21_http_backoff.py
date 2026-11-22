"""Real HTTP 429 from a local Responses endpoint, then normal numerical judgments on the same public GX run."""
from argparse import ArgumentParser, Namespace
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time

import e2e_responses as peer
from e2e_grid import fixture_env
from v21_long_run import BASE,CODEX,Staging,check,make_install,project_spec,rows,save,utc


def execute(trace_holds=False):
    directory=BASE/"var/v21-long-run"
    directory.mkdir(parents=True,exist_ok=True)
    work=Path(tempfile.mkdtemp(dir=directory,prefix="http-429-"))
    print(str(work),flush=True)
    stage,source_digest=make_install(work,Namespace(lease=12,rounds=3))
    cfg=stage/"gx-h/config/limits.json"
    limit=json.loads(cfg.read_text())
    limit["native_provider"]["retry_seconds"]=3
    save(cfg,limit)
    if trace_holds:
        # Diagnostic-only, isolated install. Show the first three admission refusals of each kind.
        driver=stage/"gx-h/gx_core/p0/driver.py"
        target=driver.read_text()
        old="                    except HoldActive:"
        check(old in target,"diagnostic driver shape changed")
        extra="""                    except HoldActive as diagnostic:
                        import sys
                        label = str(diagnostic) + " " + str(diagnostic.details)
                        notices = getattr(self, "_staging_hold_notices", {})
                        if notices.get(label, 0) < 3:
                            print("staging admission hold", label, file=sys.stderr, flush=True)
                        notices[label] = notices.get(label, 0) + 1
                        self._staging_hold_notices = notices"""
        driver.write_text(target.replace(old,extra))
    (work/"tmp").mkdir()
    started=time.monotonic()
    seen=[]
    original=peer.Peer.do_POST
    def first_refusal(handler):
        if handler.path=="/v1/responses" and not seen:
            body=json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
            first=peer.brief_from(body)
            seen.append({"http":429,"at":utc(),"task_id":first["task_id"]})
            packet=json.dumps({"error":{"type":"rate_limit_error","code":"rate_limit_exceeded",
                                        "message":"Staging rate quota reached. Retry after this window."}}).encode()
            handler.send_response(429)
            handler.send_header("Content-Type","application/json")
            handler.send_header("Content-Length",str(len(packet)))
            handler.send_header("Retry-After","3")
            handler.end_headers()
            handler.wfile.write(packet)
            return
        return original(handler)
    peer.Peer.do_POST=first_refusal
    info={"result":"FAIL","at":utc(),"base_stage_sha256":source_digest,"staged_provider_retry_seconds":3,"injected":seen}
    try:
        with peer.local_responses(work/"requests.jsonl") as server:
            env=fixture_env(work,server,str(CODEX))
            env.update(TMPDIR=str(work/"tmp"), GX_METRICS_FILE=str(work/"metrics.jsonl"))
            site=Staging(work,stage,server,env,time.monotonic()+130)
            info["commands"]=site.commands
            try:
                spec=project_spec("A",3)
                spec["metering"]={"grace_seconds":2}
                info["staged_metering_grace_seconds"]=2
                save(work/"A.json",spec)
                site.command("start","start","--run-key","A","--spec",work/"A.json")
                proc,entry=site.command("run","run","--timeout",100,background=True)
                site.reap(proc,entry,0)
                with closing(site.db()) as db:
                    tid=seen[0]["task_id"]
                    info["quota_receipts"]=rows(db,"SELECT provider,bucket,source,observed_at,blocked_until FROM p0_provider_limits")
                    info["waits"]=rows(db,"SELECT reason,started_at,ended_at,turn_id FROM p0_attempt_waits WHERE reason='provider_backoff'")
                    info["attempts"]=rows(db,"SELECT t.task_id,t.attempt,t.attempts_consumed,t.status,r.thread_id,r.lease_id FROM tasks t JOIN p0_roots r USING(task_id) WHERE t.task_id=?",(tid,))
                    info["turns"]=rows(db,"SELECT n.event_id,n.thread_id,n.turn_id,n.terminal_status,n.budget_status,n.confidence,n.settled_tokens,b.created_at,b.settled_at FROM p0_meter_native_turns n JOIN budget_reservations b USING(reservation_ref) WHERE n.thread_id=(SELECT thread_id FROM p0_roots WHERE task_id=?) ORDER BY n.sequence",(tid,))
                    info["adjustments"]=rows(db,"SELECT root_id,reported_total,delta FROM p0_meter_adjustments")
                    info["decisions"]=db.execute("SELECT count(*) FROM science_decisions").fetchone()[0]
                    info["jobs"]=rows(db,"SELECT job_id,job_attempt,status,scheduler_ref FROM jobs")
                    info["run_status"]=db.execute("SELECT status FROM runs").fetchone()[0]
                check(info["run_status"]=="COMPLETED" and info["decisions"]==3,"all scientific work completed")
                check(len(info["attempts"])==1 and info["attempts"][0]["attempts_consumed"]==0,"rate refusal consumed a failure attempt")
                check(info["turns"][0]["terminal_status"]=="failed" and info["turns"][-1]["terminal_status"]=="completed" and info["waits"] and info["quota_receipts"],"GX did not observe the official terminal 429 and its provider wait")
                reset=next(r["blocked_until"] for r in info["quota_receipts"] if r["source"]=="turn/completed")
                resume_at=datetime.fromisoformat(info["turns"][-1]["created_at"].replace("Z","+00:00")).timestamp()
                info["seconds_after_reset_to_readmission"]=round(resume_at-reset,3)
                check(resume_at>=reset and len(info["turns"])==2,"resumed before quota reset or duplicated paid turn")
                check(len(info["jobs"])==4 and all(r["job_attempt"]==1 and r["status"]=="SUCCEEDED" for r in info["jobs"]),"duplicate or failed jobs")
                info.update(result="PASS",accepted_http_200=server.request_count)
            finally:
                site.close()
    except Exception as exc:
        info["error"]=str(exc)
        raise
    finally:
        peer.Peer.do_POST=original
        info.update(finished_utc=utc(),wall_seconds=round(time.monotonic()-started,3))
        save(work/"summary.json",info)
    return work

if __name__=="__main__":
    parser=ArgumentParser(description=__doc__)
    parser.add_argument("--trace-holds",action="store_true",help="Log admission refusals in the isolated installation")
    execute(parser.parse_args().trace_holds)
