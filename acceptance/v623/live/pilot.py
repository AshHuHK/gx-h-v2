"""Register, execute or resume one original public pilot assignment with the selected host."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
import sys
from threading import Event
import time

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/"gx-h")]
from acceptance.v62c import materials,rehearse
from acceptance.v622.public import batch as native
from acceptance.v622.public.enrollment import read,save,digest
from acceptance.v622.public.funding import FundingBlocked
from acceptance.v623.live import hosts,registration
from acceptance.v623.live.registration import P


def reconcile_meter(meter):
    for row in meter.report()["requests"]:
        if row["state"]=="UNKNOWN":
            meter.reconcile(row["request_id"])
    report=meter.report()
    if report["batch"]["breaches"] or any(r["state"]=="UNKNOWN" for r in report["requests"]):
        raise FundingBlocked("Original provider exposure unresolved or quote exceeded")
    return report


def account(slot,roles,meter,closed):
    report=meter.report()
    rows={r["request_id"]:r for r in report["requests"] if r["slot"]==slot.name}
    path=slot/"raw/physical-http.jsonl"
    seen={}
    for line in path.read_text().splitlines() if path.exists() else ():
        row=json.loads(line)
        if row["phase"]=="RECEIVED":
            seen[row["request_id"]]=row
    turns={(r["thread_id"],t["turn_id"]):t for r in roles for t in r["turns"]}
    counted=defaultdict(int)
    links=[]
    for key,r in rows.items():
        w=seen.get(key)
        if not w or w["payload_sha256"]!=r["payload_sha256"] or r["scope"]!=w["scope"]:
            raise FundingBlocked("Physical request lacks its original GX host witness")
        turn=(w["thread_id"],w["turn_id"])
        if closed and (turn not in turns or r["state"]!="SETTLED"):
            raise FundingBlocked("An original native turn or its final physical charge is unresolved")
        if r["actual"] is not None:
            counted[turn]+=r["actual"]
        links.append({**{k:w[k] for k in ("run_id","scope","lease_id","role","thread_id","turn_id","model","effort")},
                      **{k:r[k] for k in ("request_id","quote_id","state","maximum","actual","provider_receipt_id","usage","upstream")}})
    differences=[{"thread_id":a,"turn_id":b,"native":t["total_tokens"],"physical":counted.get((a,b),0)}
                 for (a,b),t in turns.items() if t["total_tokens"]!=counted.get((a,b),0)]
    if closed and (differences or not links):
        raise FundingBlocked("Original Codex turn totals differ from physical provider receipts")
    return {"requests":links,"totals":report["slots"][slot.name],"scope":"all billable requests including follow-ups",
            "unmatched_turns":differences,"provider_usd":None,"source":meter.owner.source}


def drive(slot,work,item,rid,cli,parent,ready,deadline,errors,resuming):
    assignment=slot/"assignment.json"
    if assignment.exists():
        ready.set()
    elif (slot/"assignment-request.json").exists():
        with closing(rehearse.db_open(slot)) as db:
            prior=db.execute("SELECT 1 FROM events WHERE run_id=? AND kind='v621.material.assigned'",(rid,)).fetchone()
        if prior:
            native.assign(slot,work,item,rid,cli,parent,None,[])
            ready.set()
    state=cli("status","--run-id",rid,local=True)
    if state.get("goal_status")=="CLOSED":
        return state,"CLOSED"
    if time.time()>=deadline:
        return cli("stop","--run-id",rid,local=True),"TIMEOUT"
    def run(verb):
        seconds=max(0.1,deadline-time.time())
        return cli(verb,"--run-id",rid,"--timeout",seconds,timeout=seconds+20)
    verb="resume" if resuming else "run"
    while time.time()<deadline:
        with ThreadPoolExecutor(max_workers=1) as pool:
            task=pool.submit(run,verb)
            while not task.done():
                if errors or (slot/"stop-request.json").is_file():
                    ready.set()
                    state=cli("stop","--run-id",rid,local=True)
                    if errors:
                        return state,errors[0]["reason_code"] if errors[0]["reason_code"]=="BUDGET_STOP" else "HELD"
                    return state,"PAUSED"
                if not ready.is_set():
                    plan,modules,goal=native.first_plan(slot,rid)
                    if plan and goal=="OPEN":
                        native.assign(slot,work,item,rid,cli,parent,plan,modules)
                        ready.set()
                time.sleep(.04)
            state=task.result()
        if state["goal_status"]=="CLOSED":
            return state,"CLOSED"
        if not assignment.exists():
            break
        if (slot/"stop-request.json").is_file():
            return state,"PAUSED"
        if state["goal_status"]!="OPEN" or time.time()>=deadline:
            break
        if state.get("waiting_for") or state.get("wake",{}).get("state")=="quiescent":
            break
        verb="run"
    if time.time()>=deadline:
        return cli("stop","--run-id",rid,local=True),"TIMEOUT"
    return state,"WAITING"


def execute(work,item,reg,fd,owner=None):
    slot=work/item["slot"]
    slot.mkdir(mode=0o700,exist_ok=True)
    (slot/"raw").mkdir(exist_ok=True)
    first=slot/"first-attempt.json"
    resuming=first.exists()
    if not resuming:
        start=time.time()
        save(first,{"start":start,"deadline":start+P["time_limit_seconds"],"run_key":"public-pilot-"+slot.name,
                    "series":reg["series"],"slot":slot.name})
    start=read(first)
    save(slot/"grant.json",reg["grants"][item["architecture"]])
    ready=Event()
    with hosts.session(slot,rehearse.BINARY,reg,ready,owner) as (env,wire,meter,errors):
        cli=native.native_cli(slot,env,fd)
        try:
            begun=cli("start","--goal",rehearse.GOAL,"--run-key",start["run_key"],"--goal-config",slot/"grant.json")
            rid=begun["run_id"]
            with closing(rehearse.db_open(slot)) as db:
                parent,=db.execute("SELECT artifact_ref FROM artifacts WHERE run_id=? AND sha256=?",(rid,materials.PARENT_SHA)).fetchone()
            receipt=native.parent_observation(slot,rid,parent,cli)
            finished,status=drive(slot,work,item,rid,cli,parent,ready,start["deadline"],errors,resuming)
            if errors:
                status="BUDGET_STOP" if errors[0]["reason_code"]=="BUDGET_STOP" else "HELD"
            roles=hosts.role_witness(slot,rid,strict=status=="CLOSED")
            accounting=account(slot,roles,meter,status=="CLOSED")
            save(slot/"accounting.json",accounting)
            proof={} if reg["host"]=="offline" else {k:owner.proof.get(k) for k in
                                                     ("account_ref","model","provider_origin","provider_evidence_ref","contract_sha256","deployment_id","contract_id")}
            save(slot/"native-host.json",{"classification":reg["scope"],"provider":reg["provider"],
                 "verified_upstream":proof,"actual_upstream_models":sorted({r["upstream"]["model"] for r in accounting["requests"]
                                                                          if r.get("upstream") and r["upstream"].get("model")}),
                 "source":meter.owner.source,"deployment":meter.owner.deployment_id,"contract":meter.owner.contract_id,
                 "official_host_sha256":reg["official_host"]["binary_sha256"],"roots":roles})
            fingerprint=rehearse.state_hash(slot,rid)
            if status=="CLOSED":
                prior=meter.report()
                cli("resume","--run-id",rid,"--timeout",4,local=True)
                if rehearse.state_hash(slot,rid)!=fingerprint or meter.report()!=prior:
                    raise ValueError("Terminal cold recovery changed the original graph, Jobs or physical charges")
            with closing(rehearse.db_open(slot)) as db:
                jobs=[dict(r) for r in db.execute("SELECT job_id,status,capability_version,measured_json FROM jobs WHERE run_id=? ORDER BY rowid",(rid,))]
            assignment=read(slot/"assignment.json") if (slot/"assignment.json").exists() else None
            initial,_mods,_status=native.first_plan(slot,rid)
            summary={"run_id":rid,"slot":slot.name,"series":reg["series"],"host":reg["host"],"classification":reg["scope"],
                     "architecture":item["architecture"],"assignment":assignment,"parent_acquisition":receipt,"jobs":jobs,
                     "initial_plan_ref":initial,"status":status,"cold_state_sha256":fingerprint,"resources":finished["resources"],
                     "wall_seconds":round(time.time()-start["start"],3),"paid_model_calls":0 if reg["host"]=="offline" else
                     None if accounting["totals"]["unresolved_requests"] else len(accounting["requests"]),
                     "provider_usd":None,"transport_outcome":errors}
            save(slot/"summary.json",summary)
            return summary
        finally:
            save(work/"funding.json",meter.report())


def next_slot(work,num,approval,authority,resume):
    from acceptance.v623.live import deployment,adjudication
    with registration.locked(work) as fd:
        reg=read(work/"registration.json")
        roster=read(work/"approved-roster.json") if reg["host"]=="live" else None
        protocol_sha,pins=registration.declared(reg["host"],roster)
        if protocol_sha!=reg["protocol_sha256"] or pins!=read(work/"source-pins.json"):
            raise ValueError("Resume must use the original candidate and frozen protocol")
        result=read(work/"result.json")
        previous=[x["slot"] for x in reg["slots"] if x["slot"]<num]
        for number in previous:
            if (result["cases"][number]["status"] not in ("CLOSED","TIMEOUT","BUDGET_STOP") or
                    not adjudication.ready_for_next(work,number)):
                raise ValueError("Prior original assignment awaits reconciliation or adjudication")
        item=next(i for i in reg["slots"] if i["slot"]==num)
        case=result["cases"][num]
        if case["status"] in ("CLOSED","TIMEOUT","FAIL","BUDGET_STOP"):
            return case
        if (case["status"]!="REGISTERED")!=resume:
            raise ValueError("Resume the existing Run or begin its first assignment")
        owner=None
        if reg["host"]=="live":
            registration.clean_candidate(reg["source_commit"])
            pre=registration.inspect("live",rehearse.BINARY,roster,approval,authority)
            if pre["status"]!="READY" or pre["candidate"]!=reg["source_commit"]:
                raise FundingBlocked("Original production approval and hard-bound route must be current")
            owner=deployment.open_deployment()
            reg={**reg,"funding":pre["funding"]}
        if resume:
            (work/num/"stop-request.json").unlink(missing_ok=True)
        result["cases"][num]={"status":"ACTIVE"}
        save(work/"result.json",result)
        try:
            file=work/num/"summary.json"
            summary=read(file) if file.exists() and read(file)["status"]=="CLOSED" else execute(work,item,reg,fd,owner)
            if reg["host"]=="offline" and summary["status"] in ("CLOSED","TIMEOUT","BUDGET_STOP"):
                adjudication.assess_offline_slot(work,num)
            case={k:summary[k] for k in ("run_id","status","slot","architecture","paid_model_calls")}
        except (ValueError,FundingBlocked,RuntimeError) as exc:
            case={"status":"HELD" if isinstance(exc,FundingBlocked) else "FAIL","reason":str(exc)}
            state=work/num/"state/gx.db"
            if state.is_file():
                with closing(rehearse.db_open(work/num)) as db:
                    row=db.execute("SELECT run_id FROM runs LIMIT 1").fetchone()
                    if row:
                        case["run_id"]=row[0]
        result["cases"][num]=case
        save(work/"result.json",result)
        return case


def operate(work,op,num,approval,authority):
    from acceptance.v623.live import deployment,physical
    from acceptance.v623.live.transport import FixtureOwner
    if num not in {s["slot"] for s in P["slots"]}:
        raise ValueError("An original --slot is required")
    reg=read(work/"registration.json")
    slot=work/num
    if not (slot/"first-attempt.json").is_file():
        raise ValueError("The requested slot has not started")
    if op=="stop":
        terminal=read(work/"result.json")["cases"][num]
        if terminal["status"] in ("CLOSED","FAIL","BUDGET_STOP","TIMEOUT"):
            return terminal
        save(slot/"stop-request.json",{"slot":num,"requested_utc":time.time(),"reason":"OPERATOR"})
        return {"status":"STOP_REQUESTED","slot":num}
    with registration.locked(work):
        if reg["host"]=="offline":
            owner=FixtureOwner(slot,Event(),[])
        else:
            owner=deployment.open_deployment()
        meter=physical.FundedTransport(work/"funding.sqlite",series=reg["series"],slots=reg["slots"],
                 slot_cap=reg["slot_cap"],batch_cap=reg["batch_cap"],owner=owner,
                 **({"authorization":reg["funding"]["authorization"]} if reg["host"]=="live" else {}))
        try:
            reconcile_meter(meter)
        finally:
            save(work/"funding.json",meter.report())
        return meter.report()["slots"][num]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation",choices=("preflight","register","run","resume","reconcile","stop","status"))
    p.add_argument("--host",choices=("offline","live"))
    p.add_argument("--work",type=Path)
    p.add_argument("--slot",choices=("01","02","03","04"))
    p.add_argument("--roster",type=Path);p.add_argument("--approval",type=Path);p.add_argument("--authority",type=Path)
    a=p.parse_args()
    work=a.work.resolve() if a.work else None
    if a.operation!="preflight" and work is None:
        p.error("--work is required for this operation")
    if a.operation in ("preflight","register"):
        roster=read(a.roster) if a.roster else None
        args=(a.host or "live",rehearse.BINARY,roster,a.approval,a.authority)
        output=registration.inspect(*args) if a.operation=="preflight" else registration.register(work,*args)
    elif a.operation in ("run","resume"):
        registered_host=read(work/"registration.json")["host"]
        if a.host!=registered_host:
            raise ValueError("Set --host to the original registered backend before execution")
        if not a.slot:p.error("--slot required")
        output=next_slot(work,a.slot,a.approval,a.authority,a.operation=="resume")
    elif a.operation=="status":
        output=read(work/"result.json")
    else:
        output=operate(work,a.operation,a.slot,a.approval,a.authority)
    print(json.dumps(output,ensure_ascii=False,indent=2))
    return 0 if output.get("status") not in ("BLOCKED","FAIL","TIMEOUT","HELD","BUDGET_STOP") else 2


if __name__=="__main__":
    try:
        raise SystemExit(main())
    except (FundingBlocked, ValueError, BlockingIOError) as exc:
        print(json.dumps({"status":"BLOCKED","reason":str(exc)},ensure_ascii=False))
        raise SystemExit(2)
