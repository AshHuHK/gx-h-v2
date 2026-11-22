"""Stop before an in-flight R1 read reaches GX. Resume the same official thread and re-read the artifact."""
from argparse import Namespace
from contextlib import closing
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time

import e2e_responses as peer
from e2e_grid import fixture_env
from v21_long_run import BASE,CODEX,Staging,check,make_install,project_spec,rows,save,utc


def preserved(db):
    return rows(db,"SELECT run_id,round_index,decision_ref,submission_id,reviewer_submission_id FROM science_decisions ORDER BY round_index")


def execute():
    root=BASE/"var/v21-long-run"
    root.mkdir(exist_ok=True,parents=True)
    work=Path(tempfile.mkdtemp(dir=root,prefix="pause-read-"))
    print(str(work),flush=True)
    (work/"tmp").mkdir()
    stage,sha=make_install(work,Namespace(rounds=3,lease=16))
    info={"result":"FAIL","started_utc":utc(),"stage_sha256":sha}
    initial,release=threading.Event(),threading.Event()
    failed=[]
    retries=[]
    query={}
    original=peer.fixture_action
    def provider(body,ident):
        # Interpret the current task's public thread history. A rejected read supplies no artifact value.
        brief=peer.brief_from(body)
        items=body["input"]
        errors=[]
        unrelated=[]
        for i,item in enumerate(items):
            if item.get("type")=="function_call_output":
                try:
                    output=peer.output_json(item["output"])
                except (ValueError, TypeError):
                    unrelated.append(i)
                    continue
                if isinstance(output,dict) and "SCOPE_DENIED" in str(output.get("error","")):
                    errors.append(i)
        if errors:
            last=errors[-1]
            failed.append({"request":ident,"task_id":brief["task_id"],"error":peer.output_json(items[last]["output"])["error"]})
            resumed=any(i.get("role")=="user" and any("GX task brief" in t for t in peer.message_texts(i)) for i in items[last+1:])
            if not resumed:
                return {"type":"message","id":"m-"+ident,"role":"assistant","content":[{"type":"output_text",
                    "text":"GX declined the artifact request. Retaining the task and evidence reference for its next delivery."}]}, {"task_id":brief["task_id"],"deferred":True}
        if errors or unrelated:
            body={**body,"input":[obj for i,obj in enumerate(items) if i not in errors and i not in unrelated]}
        item,detail=original(body,ident)
        if detail.get("round")==1 and detail.get("output_kind")=="science_assessment" and detail.get("tool")=="gx_read_artifact":
            if not query:
                query.update(task_id=brief["task_id"],ref=detail["ref"],request=ident)
                initial.set()
                if not release.wait(15):
                    raise TimeoutError("operator did not release fixture gate")
            elif errors and detail["ref"]==query["ref"]:
                retries.append({"request":ident,"ref":detail["ref"]})
        return item,detail
    peer.fixture_action=provider
    try:
        with peer.local_responses(work/"requests.jsonl") as server:
            env=fixture_env(work,server,str(CODEX))
            env.update(TMPDIR=str(work/"tmp"), GX_METRICS_FILE=str(work/"metrics.jsonl"))
            site=Staging(work,stage,server,env,time.monotonic()+130)
            info["commands"]=site.commands
            try:
                spec=project_spec("A",3)
                save(work/"A.json",spec)
                site.command("start","start","--run-key","A","--spec",work/"A.json")
                active,entry=site.command("drive","run","--timeout",90,background=True)
                check(initial.wait(40),"first R1 read gate")
                with closing(site.db()) as db:
                    info["decisions_before"]=preserved(db)
                    first=rows(db,"SELECT run_id,thread_id,lease_id,attempt FROM p0_roots WHERE task_id=?",(query["task_id"],))[0]
                check(len(info["decisions_before"])==1,"one existing round")
                info.update(before=first,attempted_read=query)
                site.command("stop","stop","--run-id",first["run_id"])
                release.set()
                site.reap(active,entry,0)
                check(bool(failed),"no official denied artifact response during pause")
                info["denied_before_resume"]=list(failed)
                fresh,entry=site.command("resume","resume","--run-id",first["run_id"],"--timeout",65,background=True)
                site.reap(fresh,entry,0)
                with closing(site.db()) as db:
                    tasks=rows(db,"SELECT r.thread_id,r.lease_id,r.attempt,t.status,t.attempts_consumed,s.submission_id,s.result FROM p0_roots r JOIN tasks t USING(task_id) JOIN submissions s USING(task_id) WHERE r.task_id=?",(query["task_id"],))
                    info["decisions_after"]=preserved(db)
                    info["official_turns"]=rows(db,"SELECT event_id,turn_id,budget_status,confidence,terminal_status FROM p0_meter_native_turns WHERE thread_id=? ORDER BY sequence",(first["thread_id"],))
                    read=db.execute("SELECT count(*) FROM p0_root_artifact_reads a JOIN p0_roots r USING(root_key) WHERE task_id=? AND a.artifact_ref=?",(query["task_id"],query["ref"])).fetchone()[0]
                check(len(tasks)==1 and all(tasks[0][k]==first[k] for k in ("thread_id","lease_id","attempt")) and tasks[0]["result"]=="ACCEPTED" and tasks[0]["attempts_consumed"]==0,"resume lost original authority")
                check(read==1 and retries and info["decisions_after"][:1]==info["decisions_before"] and len(info["decisions_after"])==3,"retry read, immutable prior decisions, completion")
                info.update(result="PASS",after=tasks[0],successful_reread=retries,provider_requests=server.request_count,run_status=site.snapshot()[first["run_id"]]["status"])
            finally:
                release.set()
                site.close()
    except Exception as exc:
        info["error"]=str(exc)
        raise
    finally:
        peer.fixture_action=original
        info.update(ended_utc=utc(),retries=retries,denied_mentions=len(failed))
        save(work/"summary.json",info)
    return work

if __name__=="__main__":
    execute()
