"""Seal a public archive study, then operate its staged GX Goal using an official local host."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime,timezone
import fcntl
import json
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event
import time

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/"gx-h")]
from acceptance.v62c import rehearse
from acceptance.v622.public import batch,funding
from acceptance.v622.public.enrollment import save,digest,read
from acceptance.v623.study import host,scripted_lead
from gx_core.p0 import v621_policy

PROTOCOL=HERE/"operator/reference_protocol.json"
GOAL=HERE/"lead/SCIENCE_GOAL.md"
CARD=HERE/"lead/method-card.json"
ACTOR="OFFICIAL_CODEX_HOST_SCRIPTED_LOOPBACK"
CAPABILITIES=["deposited-chain-geometry@2",scripted_lead.METHOD,"deposited-block-reading@1"]
PLUGINS=["org.gx.protein@0.3.0","org.gx.block-influence@1.0.0"]


def now():
    return datetime.now(timezone.utc).isoformat()


def freeze_files():
    roots=[ROOT/"gx-h/plugins/installed/org.gx.block-influence/1.0.0",
           ROOT/"gx-h/plugins/installed/org.gx.protein/0.3.0", ROOT/"gx-h/gx_core/p0"]
    files=[p for root in roots for p in root.rglob("*") if p.is_file() and p.suffix in {".py",".md",".json"}
           and "operator" not in p.parts and "independent" not in p.name]
    files.extend([Path(__file__),HERE/"host.py",HERE/"scripted_lead.py",HERE/"run_receipt.py",GOAL,CARD,PROTOCOL,ROOT/"gx-h/contracts/CONTRACT_SHA",ROOT/"gx-h/adapters/scientific/tl1a_geometry/worker.py"])
    return {str(p.relative_to(ROOT)):digest(p) for p in sorted(files)}


def grant(lead):
    limits={"tokens":1_000_000,"worker_tasks":40,"api_calls":40,"job_submissions":4,"cpu_seconds":600,
            "wall_hours":1,"gpu_jobs":0,"gpu_minutes":0}
    return {"project":"archive-coordinate-report","research_effort":"deep","evidence_policy":v621_policy.VERSION,
            "branch_delegation":False,"research_modules":[],"plugins":PLUGINS,"capabilities":CAPABILITIES,
            "run_budget":limits,"workspace_budget":limits,"initial_job_limit":4,"turn_token_budget":32768,
            "input_files":[{"role":"workflow_spec","path":str(lead/"method-card.json"),"sha256":digest(CARD)}]}


def seal(work,binary,synthetic):
    if work.exists() or work.is_relative_to(ROOT):
        raise ValueError("Choose a fresh directory outside the source checkout")
    protocol=read(PROTOCOL)
    if digest(GOAL)!=protocol["neutral_goal_sha256"]:
        raise ValueError("The supplied Lead goal differs from the protocol")
    binary_pin=read(ROOT/"release-assets/lock.json")["linux_x64"]["binary_sha256"]
    if digest(binary)!=binary_pin:
        raise ValueError("Unexpected official host binary")
    work.mkdir(parents=True,mode=0o700)
    lead=work/"lead";lead.mkdir()
    custody=work/"custody";custody.mkdir(mode=0o700)
    shutil.copyfile(CARD,lead/"method-card.json")
    shutil.copyfile(GOAL,lead/"goal.md")
    items=[]
    for n,pin in enumerate(protocol["inputs"]):
        directory=custody/str(n+1);directory.mkdir()
        dest=directory/"coordinates.cif";descr=directory/"acquisition.json"
        if synthetic:
            from acceptance.v623.test_block_method import synthetic_cif
            dest.write_bytes(synthetic_cif(deformed=(n==1)))
            save(descr,{"source":"local synthetic coordinate fixture","acquisition":"local engineering",
                        "publication":None,"sample":None,"sampling":None,"processing":None,
                        "applicability":"Synthetic coordinate observations, model 1, entity 1 and A/B/C only."})
            entry=f"synthetic-{n+1}"
        else:
            if digest(ROOT/pin["repository_path"])!=pin["sha256"]:
                raise ValueError("Registered public source changed")
            shutil.copyfile(ROOT/pin["repository_path"],dest)
            shutil.copyfile(ROOT/pin["descriptor"],descr)
            entry=pin["entry"]
        items.append({"entry":entry,"file":str(dest.relative_to(work)),"sha256":digest(dest),
                      "descriptor":str(descr.relative_to(work)),"descriptor_sha256":digest(descr)})
    save(custody/"grant.json",grant(lead))
    receipt={"schema":"gx.archive-study-enrollment/1","series":protocol["series"]+("-SYNTHETIC" if synthetic else ""),
             "source_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
             "protocol_commit":subprocess.check_output(["git","log","-1","--format=%H","--",str(PROTOCOL.relative_to(ROOT))],cwd=ROOT,text=True).strip(),
             "registered_utc":now(),"public_inputs":not synthetic,"operator_reference_sha256":digest(PROTOCOL),
             "mode":"SYNTHETIC_FIXTURE" if synthetic else "PUBLIC_RETROSPECTIVE_CPU",
             "known_before":"Original public coordinates and legacy global fits." if not synthetic else "Public synthetic fixtures.",
             "actor_class":ACTOR,"live_scientific_decisions":0,"paid_provider_calls":0,
             "host_binary_sha256":binary_pin,"frozen_files":freeze_files(),"inputs":items,
             "grant_sha256":digest(custody/"grant.json"),"lead_goal_sha256":digest(lead/"goal.md"),
             "native_profile":{"model":rehearse.P["model"],"effort":rehearse.P["model_effort"],"provider":"gx_loopback"},
             "operator_method":"Direct sequential full-backbone and alpha-carbon fits, each on one newly assigned file."}
    save(work/"seal.json",receipt)
    return receipt


def verify_seal(work,binary):
    record=read(work/"seal.json")
    files={**record["frozen_files"]}
    if (record["source_commit"]!=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip() or
            any(digest(ROOT/k)!=v for k,v in files.items()) or digest(binary)!=record["host_binary_sha256"] or
            digest(work/"lead/goal.md")!=record["lead_goal_sha256"] or
            digest(work/"lead/method-card.json")!=files[str(CARD.relative_to(ROOT))] or
            digest(work/"custody/grant.json")!=record["grant_sha256"] or record["actor_class"]!=ACTOR):
        raise ValueError("The frozen study configuration differs; preserve this enrollment")
    for item in record["inputs"]:
        if digest(work/item["file"])!=item["sha256"] or digest(work/item["descriptor"])!=item["descriptor_sha256"]:
            raise ValueError("A sealed material changed")
    return record


def decisions(db,rid):
    result=[]
    for row in db.execute("""SELECT d.*,e.ts FROM v6_decisions d JOIN v6_decision_tasks t USING(task_id,run_id)
            JOIN events e ON e.event_id=json_extract(d.receipt_json,'$.event_id') WHERE d.run_id=? AND t.scope='run'
            ORDER BY e.event_id""",(rid,)):
        product=json.loads(row["request_json"])["decision"]
        result.append({"decision_ref":row["decision_ref"],"thread_id":row["thread_id"],"turn_id":row["turn_id"],
                       "call_id":row["call_id"],"event_id":json.loads(row["receipt_json"])["event_id"],"utc":row["ts"],
                       **{k:product[k] for k in ("mode","ops","rationale","evidence_refs","metric_refs") if k in product}})
    return result


def opening(work,slot,rid,cli,seal,stage,events):
    ack=work/"custody"/f"opening-{stage}.json"
    if ack.exists():
        events[stage-1].set()
        return True
    item=seal["inputs"][stage-1]
    with closing(rehearse.db_open(slot)) as db:
        accepted=decisions(db,rid)
        if stage==1:
            commitment=next((d for d in accepted if any(o["op"]=="open_branch" for o in d["ops"])),None)
        else:
            commitment=next((d for d in accepted if d["mode"]=="resolve" and
                             d["rationale"].startswith("Second-deposition prediction.") and d["evidence_refs"]),None)
    if not commitment:
        return False
    record=cli("assign-material","--run-id",rid,"--after-decision",commitment["decision_ref"],
               "--source",item["entry"],"--role","structure","--input",work/item["file"],"--sha256",item["sha256"],
               "--acquisition",work/item["descriptor"],"--media-type","chemical/x-mmcif")
    if commitment["event_id"]>=record["event_id"]:
        raise ValueError("Material preceded its required accepted commitment")
    with closing(rehearse.db_open(slot)) as db:
        utc=db.execute("SELECT ts FROM events WHERE event_id=? AND run_id=?",(record["event_id"],rid)).fetchone()[0]
    save(ack,{"entry":item["entry"],"opened_utc":utc,"assignment":record,"commitment":commitment})
    events[stage-1].set()
    return True


def run(work,binary):
    seal=verify_seal(work,binary)
    slot=work/"lead"
    with (work/".study.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        ledger=funding.BatchFunding(work/"custody/accounting.sqlite",series=seal["series"],slots=[slot.name],batch_cap=1_000_000)
        events=[Event(),Event()]
        with host.scripted(slot,binary,ledger,events) as (env,wire,peer):
            cli=batch.native_cli(slot,env,lock.fileno())
            status=cli("start","--goal",(slot/"goal.md").read_text().strip(),"--run-key","archive-coordinate-study",
                       "--goal-config",work/"custody/grant.json")
            rid=status["run_id"]
            if status["goal_status"]!="CLOSED":
                with ThreadPoolExecutor(max_workers=1) as ex:
                    for index, ev in enumerate(events):
                        if (work/"custody"/f"opening-{index+1}.json").exists():
                            ev.set()
                    driver=ex.submit(cli,"run","--run-id",rid,"--timeout",360,timeout=385)
                    try:
                        for stage in (1,2):
                            while not opening(work,slot,rid,cli,seal,stage,events):
                                if driver.done():
                                    break
                                if any(r.get("error") for r in peer.rows):
                                    raise ValueError("Scripted transport failed; inspect the protected local request log")
                                time.sleep(.03)
                    except BaseException:
                        for ev in events:ev.set()
                        cli("stop","--run-id",rid,local=True)
                        raise
                    status=driver.result()
            if status["goal_status"]!="CLOSED":
                raise RuntimeError("The study Goal is not yet closed; inspect its persisted state")
            before=rehearse.state_hash(slot,rid)
            cold=cli("resume","--run-id",rid,"--timeout",5,local=True)
            if cold["goal_status"]!="CLOSED" or before!=rehearse.state_hash(slot,rid) or status["resources"]!=cold["resources"]:
                raise RuntimeError("Cold replay changed the study")
            from acceptance.v623.study import run_receipt
            result=run_receipt.collect(work,rid,seal,ledger,wire,before)
            save(work/"study-receipt.json",result)
            return result


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("seal","operate"))
    parser.add_argument("--work",type=Path,required=True)
    parser.add_argument("--binary",type=Path,default=rehearse.BINARY)
    parser.add_argument("--synthetic",action="store_true",help="Seal two transparent artificial coordinate specimens")
    args=parser.parse_args()
    target=args.work.resolve()
    result=(seal(target,args.binary.resolve(),args.synthetic) if args.command=="seal" else run(target,args.binary.resolve()))
    print(json.dumps({k:result[k] for k in ("series","actor_class","run_id","status","paid_provider_calls") if k in result},sort_keys=True))
