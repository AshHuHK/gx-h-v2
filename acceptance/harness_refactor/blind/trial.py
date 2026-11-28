"""Run one original arm and inject a process loss after its first successful E1 read."""
import argparse
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

from common import ARMS, BASE, BINARY, EFFORT, HERE, LIMIT, MODEL, PACKET, ROOT, environment, oauth_host, lock, read, save, sha

def sql(folder,statement,args=()):
    path=folder/"state/gx.db"
    if not path.is_file(): return []
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True,timeout=2)) as db:
        db.row_factory=sqlite3.Row
        try: return [dict(r) for r in db.execute(statement,args)]
        except sqlite3.OperationalError as err:
            if "no such table" in str(err): return []
            raise

def gx_checkpoint(folder):
    return sql(folder,"""SELECT c.thread_id,c.turn_id,c.call_id,
                  json_extract(n.request_json,'$.arguments.ref') AS artifact_ref
                  FROM p0_root_calls c JOIN p0_v6_native_calls n USING(thread_id,turn_id,call_id)
                  WHERE json_extract(n.request_json,'$.tool')='gx_read_artifact'
                    AND json_extract(c.result_json,'$.record')='E1' ORDER BY c.rowid LIMIT 1""")

def official_read(folder, checkpoint):
    """Require the original App Server to have durably completed the same scientific read."""
    archive=read(PACKET/"initial.json")
    thread=checkpoint["thread_id"]
    for path in (folder/"sessions").rglob("*"+thread+".jsonl"):
        with path.open() as rows:
            for index,line in enumerate(rows,1):
                if 'gx_read_artifact' not in line or 'DynamicToolCall' not in line: continue
                try: row=json.loads(line)
                except json.JSONDecodeError: continue
                msg=row.get("payload",{})
                item=msg.get("item",{})
                if (row.get("type")!="event_msg" or msg.get("type")!="item_completed"
                        or msg.get("turn_id")!=checkpoint["turn_id"] or msg.get("thread_id")!=thread
                        or item.get("type")!="DynamicToolCall" or item.get("tool")!="gx_read_artifact"
                        or item.get("id")!=checkpoint["call_id"]
                        or item.get("status")!="completed" or item.get("success") is not True): continue
                for content in item.get("content_items",[]):
                    try: body=json.loads(content.get("text", ""))
                    except (ValueError,TypeError):continue
                    if body==archive:
                        return dict(checkpoint, official_time=row["timestamp"], receipt_line=index,
                                session=str(path.relative_to(folder)), archive_sha256=sha(PACKET/"initial.json"))
    return None

def gx_facts(folder, *, required=True):
    goals=sql(folder,"SELECT run_id,status FROM v6_goals")
    if not goals and not required:return {"goal":None,"jobs":[],"turns":[],"last_event":None}
    if len(goals)!=1: raise ValueError(f"Expected one original Run, found {len(goals)}")
    run=goals[0]["run_id"]
    return {"goal":goals[0],"jobs":sql(folder,"SELECT job_id,status FROM jobs WHERE run_id=?",(run,)),
        "turns":sql(folder,"""SELECT DISTINCT thread_id,turn_id FROM p0_root_deliveries
                        WHERE run_id=? AND method='turn/start' AND state='acknowledged' AND turn_id IS NOT NULL""",(run,)),
        "last_event":sql(folder,"SELECT max(event_id) AS event_id FROM events WHERE run_id=?",(run,))[0]["event_id"]}

def family(pid):
    # Linux app-server creates a new session. Collect actual descendants of our launch even across PGIDs.
    targets={pid};todo=[pid]
    while todo:
        current=todo.pop()
        for task in Path("/proc",str(current),"task").glob("*/children"):
            try: kids=[int(p) for p in task.read_text().split()]
            except (FileNotFoundError, ProcessLookupError):continue
            for child in kids:
                if child not in targets:
                    todo.append(child); targets.add(child)
    return targets

def terminal_before_fault(folder,checkpoint):
    path=folder/checkpoint["session"]
    with path.open() as rows:
        for line in rows:
            if "task_complete" not in line and "turn_aborted" not in line:continue
            try: row=json.loads(line)
            except ValueError:continue
            msg=row.get("payload",{})
            if (row.get("type")=="event_msg" and msg.get("type") in ("task_complete","turn_aborted")
                    and msg.get("turn_id")==checkpoint["turn_id"]):
                return {"kind":msg["type"],"time":row["timestamp"]}
    return None

def stop(pid):
    pids=family(pid)
    for num in [pid,*sorted(pids-{pid},reverse=True)]:
        try: os.kill(num,signal.SIGKILL)
        except ProcessLookupError: pass
    return len(pids)

def start(folder,phase,argv,cwd,env):
    stdout=(folder/(phase+".out")).open("ab");stderr=(folder/(phase+".err")).open("ab")
    try:
        return subprocess.Popen(argv,cwd=cwd,env=env,start_new_session=True,stdout=stdout,stderr=stderr)
    finally:
        stdout.close();stderr.close()

def gx_args(folder,release,phase):
    cmd=[str(release/"bin/gx-p0"),"research","--state",str(folder/"state")]
    if phase=="first":
        return cmd+["--run-key","blind-archive-original","--goal-file",str(PACKET/"question.txt"),
                    "--goal-config",str(folder/"goal.json")]
    return cmd+["--run-id",gx_facts(folder)["goal"]["run_id"]]

def stage(folder,arm,release,phase,deadline,env):
    marker=folder/(phase+"-launch.json")
    if marker.exists():
        if phase=="second" and ((arm=="stock" and (folder/"second-terminal.json").is_file()) or
                (arm!="stock" and gx_facts(folder)["goal"]["status"]=="CLOSED")):
            return "finished"
        raise ValueError("Phase was already launched. Reconcile the original process/Run before any further turn.")
    argv=([sys.executable,"-B",str(HERE/"native.py"),str(folder),phase] if arm=="stock" else gx_args(folder,release,phase))
    save(marker,{"phase":phase,"at":time.time()})
    p=start(folder,phase,argv,release,None if arm=="stock" else env)
    try:
        while time.time()<deadline:
            point=folder/"first-read.json"
            if phase=="first" and not point.exists():
                if arm=="stock":
                    reply=folder/"first-tool-reply.json"
                    found=[read(reply)] if reply.exists() else []
                else:
                    found=gx_checkpoint(folder)
                if found and (official:=official_read(folder,found[0])):
                    save(point,official | {"time":time.time()})
            if phase=="first" and point.exists():
                checkpoint=read(point)
                closed=terminal_before_fault(folder,checkpoint)
                if p.poll() is not None or closed:
                    save(folder/"missed-interruption.json",{"at":time.time(),"terminal_event":closed,"driver_rc":p.returncode})
                    return "missed_interruption"
                n=stop(p.pid);p.wait(timeout=15)
                # Official completion could have won the narrow race between the check and SIGKILL.
                if (closed:=terminal_before_fault(folder,checkpoint)) or p.returncode!=-signal.SIGKILL:
                    save(folder/"missed-interruption.json",{"at":time.time(),"terminal_event":closed,"driver_rc":p.returncode})
                    return "missed_interruption"
                save(folder/"interruption.json",{"logical_event":"first successful E1 read before close",
                    "at":time.time(),"signal":"SIGKILL","processes_signalled":n,"driver_returncode":p.returncode,
                    "checkpoint":read(point),"before":gx_facts(folder) if arm!="stock" else {"thread":read(folder/"thread.json")}})
                return "interrupted"
            rc=p.poll()
            if rc is not None:
                return "finished" if rc==0 and phase=="second" else f"{phase}_exited_{rc}"
            time.sleep(.18)
        return "deadline"
    finally:
        if p.poll() is None:stop(p.pid);p.wait(timeout=15)

def execute(work,arm,baseline,candidate):
    folder=work/arm; folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    if (folder/"outcome.json").exists():return read(folder/"outcome.json")
    release=(baseline if arm=="baseline" else ROOT).resolve()
    oid=subprocess.check_output(["git","-C",str(release),"rev-parse","HEAD"],text=True).strip()
    if arm=="baseline" and (oid!=BASE or subprocess.check_output(["git","-C",str(release),"status","--porcelain","--untracked-files=no"]).strip()):
        raise ValueError("Original GX source differs from its registered detached baseline")
    if not candidate or len(candidate)!=40 or not all(x in "0123456789abcdef" for x in candidate):
        raise ValueError("The registered reviewed candidate must be identified by its full commit SHA")
    save(work/"candidate.json",{"revision":candidate})
    if arm=="revised" and (oid!=candidate or subprocess.check_output(
            ["git","-C",str(release),"status","--porcelain","--","gx-h"]).strip()):
        raise ValueError("Revised arm must run the registered candidate with clean product bytes")
    lock(work)
    env=environment(folder)
    if not (folder/"began.json").exists():
        save(folder/"began.json",{"start":time.time(),"release":oid,"arm":arm,"status":"original"})
    begin=read(folder/"began.json");deadline=begin["start"]+LIMIT
    if arm!="stock":
        save(folder/"goal.json",{"project":"public-archive-curation","budget_profile":"oauth_research",
             "evidence_policy":"gx.evidence/6.2.4","research_effort":"deep",
             "capability_collections":[],"capabilities":[],"initial_job_limit":0,
             "input_files":[{"role":"archive_record","path":str(PACKET/"initial.json"),"sha256":sha(PACKET/"initial.json")}]})
    outcome="interrupted" if (folder/"interruption.json").exists() else stage(folder,arm,release,"first",deadline,env)
    if outcome=="interrupted":
        if arm!="stock":
            facts=gx_facts(folder);rid=facts["goal"]["run_id"]
            if facts["goal"]["status"]=="CLOSED": raise ValueError("Closed before intervention; cannot recreate this original")
            if not (folder/"late-delivery.json").exists():
                msg="Publisher response to your open question. Use E2 to finish the same curatorial decision.\n"+(PACKET/"late.json").read_text()
                cp=subprocess.run([str(release/"bin/gx-p0"),"message","--state",str(folder/"state"),
                    "--run-id",rid,"--message-id","gate4-publisher-response","--text",msg,"--queue-only"],
                    cwd=release,env=env,text=True,capture_output=True,check=True,timeout=20)
                save(folder/"late-delivery.json",{"at":time.time(),"source_sha256":sha(PACKET/"late.json"),"receipt":json.loads(cp.stdout)})
        elif not (folder/"late-delivery.json").exists():
            save(folder/"late-delivery.json",{"at":time.time(),"source_sha256":sha(PACKET/"late.json"),"route":"same native thread/resume"})
        outcome=stage(folder,arm,release,"second",deadline,env)
    facts={} if arm=="stock" else gx_facts(folder,required=outcome=="finished")
    if outcome=="finished" and arm=="stock" and not read(folder/"second-terminal.json")["answer"]:
        outcome="no_final_report"
    if outcome=="finished" and (arm!="stock" and facts["goal"]["status"]!="CLOSED" or
            arm=="stock" and read(folder/"second-terminal.json")["status"]!="completed"):
        outcome="research_open"
    result={"outcome":outcome,"arm":arm,"elapsed_seconds":round(time.time()-begin["start"],2),
            "finish":time.time(),"facts":facts}
    save(folder/"outcome.json",result)
    return result

if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("workspace",type=Path);cli.add_argument("arm",choices=ARMS)
    cli.add_argument("--candidate",required=True,help="Full immutable revised product commit")
    cli.add_argument("--baseline",type=Path,default=ROOT.parent/"harness-refactor-baseline-50d31f5")
    opt=cli.parse_args();w=opt.workspace.resolve();w.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (w/(opt.arm+".lock")).open("a") as lk:
        fcntl.flock(lk,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if not (w/"account.json").exists():
            p=oauth_host().preflight(BINARY,MODEL,EFFORT)
            if p.get("status")!="READY":raise SystemExit(str(p["reason_codes"]))
            save(w/"account.json",{k:p[k] for k in ("status","reason_codes","auth_type","subscription_usage_state")})
        print(json.dumps(execute(w,opt.arm,opt.baseline,opt.candidate),ensure_ascii=False))
