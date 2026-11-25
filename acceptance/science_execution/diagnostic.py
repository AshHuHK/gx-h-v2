"""Enroll and operate the two original successor diagnostics through the ordinary GX Goal."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from functools import partial
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.v622.public import batch as native
from acceptance.v622.public.enrollment import digest, read, save
from acceptance.v623.live import hosts, physical, registration, transport
from acceptance.v623.live import pilot as local
from acceptance.v623.oauth import host, pilot as subscription
from acceptance.v62c import materials, rehearse

SLOTS = ("01", "02")
OAUTH = "direct_chatgpt_oauth"
SERIES = {"offline":"GX-V62-CLOSE-ACTION-LOCAL-01", OAUTH:"GX-V62-CLOSE-ACTION-OAUTH-01"}
TERMINAL = {"CLOSED","TIMEOUT","TERMINATED"}
DELEGATE = ("For this diagnostic, delegate the assigned specimen inventory and its named measurement "
            "to a scoped data-inventory module. Read its published report. Decide the scientific action yourself.")


def protocol(route):
    p = registration.P
    return {
        "schema":"gx.science.execution.diagnostic/1", "series":SERIES[route],
        "slots":[{**p["slots"][i],"delegation_required":i==1} for i in range(2)],
        **{k:p[k] for k in ("model","model_effort","research_effort","sampling","assay","threshold","materials","module")},
        "goals":{"01":rehearse.GOAL,"02":rehearse.GOAL+" "+DELEGATE},
        "method_card_sha256":digest(registration.HERE/"comparison-card.json"),
        "evidence_policy":"gx.evidence/6.2.4","registered_slots":2,"replacement":False,
        "new_work_seconds":900,"drain_seconds":360,"reconcile_seconds":120,
        "observation_window":"[start,new_work_until); report effects during drain separately",
        "admission_layer":"GX turn/start, steer and Job admission; a native turn may initiate multiple physical requests",
        "criteria":{"01":"First disposition submission accepted; independent count, cited delivered row, matching effect, final usage and operator scientific review.",
                    "02":"Required native commission, measured by its own model thread, published, received, cited and enacted; independent count, per-role reconciled usage and operator scientific review."},
        "additional_currency_authorized":0,"api_key_fallback":False,"purchase_credits":False,
        "provider":"gx_loopback" if route=="offline" else "openai",
        "scope":"OFFICIAL_CODEX_LOOPBACK" if route=="offline" else "PUBLIC_OAUTH_SUBSCRIPTION",
    }


def preflight(route,binary):
    p = protocol(route)
    if route==OAUTH:
        proof=host.preflight(binary,p["model"],p["model_effort"])
    else:
        proof={**hosts.inspect_binary(binary),"status":"READY","inference_calls":0,"account_ref":None}
    return {**proof,"series":p["series"],"candidate":subscription.candidate(),"protocol_sha256":registration.sha(p)}


def authority(work,binary,approval,reg=None):
    proof=preflight(OAUTH,binary)
    if proof["status"]!="READY":
        raise ValueError("Included OAuth preflight: "+", ".join(proof["reason_codes"]))
    registration.clean_candidate(proof["candidate"])
    if not approval:
        raise ValueError("The successor series requires its scoped operator approval")
    from acceptance.science_execution.approval import verify
    signed=verify(approval,work=work,candidate=proof["candidate"],series=SERIES[OAUTH],account_ref=proof["account_ref"])
    if reg and (signed["approval_sha256"]!=reg["approval_sha256"] or proof["account_ref"]!=reg["account_ref"]):
        raise ValueError("The original account or approval changed")
    return proof,signed["approval_sha256"]


def validate_materials(work,p):
    if p.get("oracle_sha256") and digest(work/"custody/oracle.json")!=p["oracle_sha256"]:
        raise ValueError("The independent reference changed after registration")
    for item in p["slots"]:
        path=work/"custody"/item["code"]
        pin=p["materials"][item["code"]]
        if digest(path/"specimen.cif")!=pin["sha256"] or digest(path/"transform.json")!=pin["transform_sha256"]:
            raise ValueError("Material or transformation differs from its registered bytes")


def register(work,route,binary,approval):
    if work.is_relative_to(ROOT):
        raise ValueError("Use a cohort directory outside the source checkout")
    p=protocol(route)
    proof,approval_sha=authority(work,binary,approval) if route==OAUTH else (preflight(route,binary),None)
    work.mkdir(mode=0o700,parents=True,exist_ok=True)
    with registration.locked(work):
        registry=work/"registration.json"
        if not (work/"custody").is_dir():
            if registry.exists():
                raise ValueError("Missing original custody")
            with TemporaryDirectory(dir=work) as tmp:
                path=Path(tmp)/"custody"
                materials.prepare(path)
                path.rename(work/"custody")
        validate_materials(work,p)
        grants={}
        for arm in ("single","delegation_available"):
            g=(subscription.cohort_grant(arm,subscription.protocol()) if route==OAUTH else registration.grant(arm))
            grants[arm]={**g,"evidence_policy":p["evidence_policy"]}
        reg={**p,"host":route,"source_commit":proof["candidate"],"protocol_sha256":registration.sha(p),
             "official_binary_sha256":digest(binary),"binary":str(binary.resolve()),"effort":p["model_effort"],
             "account_ref":proof["account_ref"],"approval_sha256":approval_sha,"oracle_sha256":digest(work/"custody/oracle.json"),
             "grants":grants,"wire_model":p["model"],"wire_effort":p["model_effort"],
             "slot_cap":1_000_000 if route=="offline" else None,"batch_cap":2_000_000 if route=="offline" else None}
        if registry.exists() and read(registry)!=reg:
            raise ValueError("Registration differs from this original diagnostic")
        if not registry.exists():
            save(registry,reg)
            save(work/"preflight.json",proof)
        if not (work/"result.json").exists():
            if any((work/n/"first-attempt.json").exists() for n in SLOTS):
                raise ValueError("Original result index missing")
            save(work/"result.json",{"series":p["series"],"host":route,"registration_sha256":digest(registry),
                                     "cases":{i["slot"]:{"status":"REGISTERED","code":i["code"]} for i in p["slots"]}})
        return {k:reg[k] for k in ("series","slots","source_commit","scope","approval_sha256")}


def fixture_step(brief,replies,*,behavior):
    name,args=transport.scientific_step(brief,replies,behavior=behavior)
    if name=="gx_decide_revision" and brief["scope"]=="run":
        decision=args["decision"]
        ops={op["op"] for op in decision["ops"]}
        if decision.get("metric_refs"):
            shown=[row for evidence in brief.get("observations",[])+brief.get("module_reports",[])
                   for row in evidence.get("endpoint_rows",[])]
            keys=[]
            for ref in decision["metric_refs"]:
                choices={row["key"] for row in shown if ref.removeprefix("metric_").startswith(row["key"])}
                if len(choices)!=1:
                    raise ValueError("The fixture cannot find its printed endpoint row")
                keys.append(choices.pop())
            decision["metric_refs"]=keys
        if decision.get("metric_refs") and ops & {"stop_branch","add_node"}:
            return name,{"decision":{k:decision[k] for k in ("ops","rationale","metric_refs")}}
        if "close_run" in ops:  # The reference measurement completes the work after the comparison action.
            for k in ("comparison_id","disposition","stop_reason","affected_scope"):
                decision.pop(k,None)
    return name,args


@contextmanager
def native_session(slot,binary,reg,ready):
    if reg["host"]=="offline":
        step=partial(fixture_step,behavior="direct" if slot.name=="01" else "late")
        with hosts.session(slot,binary,reg,ready,fixture_step=step) as (env,_wire,_meter,errors):
            yield env,errors
    else:
        with host.session(slot,binary,reg) as env:
            yield env,[]


def first_attempt(slot,reg,now):
    path=slot/"first-attempt.json"
    if path.exists():
        return read(path)
    end=now+reg["new_work_seconds"]
    drain=end+reg["drain_seconds"]
    window={"start":now,"new_work_until":end,"drain_until":drain,"reconcile_until":drain+reg["reconcile_seconds"]}
    first={"series":reg["series"],"slot":slot.name,"run_key":reg["series"]+"-"+slot.name,
           "start":now,"research_window":window}
    save(path,first)
    return first


def local_stop(slot,rid):
    # stop is a local SQLite command in gx-p0 and does not open an App Server.
    argv=[sys.executable,"-B",str(ROOT/"bin/gx-p0"),"stop","--state",str(slot/"state"),"--run-id",rid]
    response=subprocess.check_output(argv,env={**os.environ,"GX_CODEX_OFFLINE":"1"},cwd=slot,text=True,timeout=15)
    return json.loads(response)


def stop_original(slot,rid,reason,action):
    state=action()
    with closing(rehearse.db_open(slot)) as db:
        originals=[dict(r) for r in db.execute("""SELECT event_id,thread_id,turn_id,reservation_ref,state
                    FROM p0_root_deliveries WHERE run_id=? ORDER BY rowid""",(rid,))]
    doc={"run_id":rid,"reason":reason,"utc_unix":time.time(),"gx_status":state["status"],
         "original_deliveries":originals,"provider_cancellation":"UNCONFIRMED_BY_GX_STOP"}
    # Distinct receipts avoid competing operator and driver writes during a manual stop.
    save(slot/("stop-"+str(time.time_ns())+".json"),doc)
    return state


def drive(slot,work,item,rid,cli,parent,ready,errors,window,resuming):
    final_at=window["drain_until"]
    def stop(reason):
        return stop_original(slot,rid,reason,lambda:cli("stop","--run-id",rid,local=True,timeout=15))
    if (slot/"assignment.json").is_file():
        ready.set()
    state=cli("status","--run-id",rid,local=True)
    if state["goal_status"]=="CLOSED":
        return "CLOSED",state
    verb="resume" if resuming else "run"
    while time.time()<final_at:
        seconds=max(.1,final_at-time.time())
        with ThreadPoolExecutor(max_workers=1) as pool:
            running=pool.submit(cli,verb,"--run-id",rid,"--timeout",seconds,timeout=seconds+20)
            try:
                while not running.done():
                    now=time.time()
                    outcome=None
                    if now>=final_at:
                        outcome="TIMEOUT"
                    elif errors:
                        outcome="HELD"
                    elif (slot/"stop-request.json").is_file():
                        outcome="PAUSED"
                    elif item["host"]==OAUTH and subscription.active_provider_hold(slot,rid):
                        outcome="PROVIDER_HOLD"
                    if outcome:
                        ready.set()
                        return outcome,stop(outcome)
                    if not ready.is_set() and now<window["new_work_until"]:
                        if subscription.reconcile_assignment(slot,work,item,rid,cli,parent):
                            ready.set()
                    time.sleep(.15)
                state=running.result()
            except BaseException:
                ready.set()
                stop("CONTROLLER_EXCEPTION")
                raise
        if state["goal_status"]=="CLOSED":
            return "CLOSED",state
        if (slot/"stop-request.json").is_file() or state.get("status")=="PAUSED":
            return "PAUSED",state
        # The Driver can exit quiescent just before the operator publishes its first assignment.
        if not ready.is_set() and time.time()<window["new_work_until"]:
            if subscription.reconcile_assignment(slot,work,item,rid,cli,parent):
                ready.set()
                verb="run"
                continue
        if time.time()>=window["new_work_until"]:
            pending_jobs=any(j["status"] not in {"SUCCEEDED","FAILED","CANCELLED"} for j in state.get("jobs",[]))
            if time.time()<final_at and (pending_jobs or state.get("unsettled_turns")):
                time.sleep(.25)
                verb="run"
                continue
            break
        if state.get("waiting_for") or state.get("wake",{}).get("state")=="quiescent":
            return "WAITING",state
        verb="run"
    return "TIMEOUT",stop("TIMEOUT")


def summarize_run(slot,rid,item,reg,window,status,*,state=None,errors=(),recovered=False):
    plan,_,_=native.first_plan(slot,rid)
    with closing(rehearse.db_open(slot)) as db:
        jobs=[dict(r) for r in db.execute("SELECT job_id,status,capability_version FROM jobs WHERE run_id=? ORDER BY rowid",(rid,))]
        parent=db.execute("""SELECT event_id,payload_json FROM events WHERE run_id=?
                             AND kind='v6.observation.added' AND subject_id='archive-reference'""",(rid,)).fetchone()
    doc={**item,"series":reg["series"],"status":status,"run_id":rid,"host":reg["host"],"real_model":reg["host"]==OAUTH,
         "classification":reg["scope"],"research_window":window,"jobs":jobs,"first_attempt":True,"initial_plan_ref":plan,
         "parent_acquisition":({"event_id":parent[0],**json.loads(parent[1])} if parent else None),
         "assignment":read(slot/"assignment.json") if (slot/"assignment.json").exists() else None,
         "resources":(state or {}).get("resources"),"snapshot_at":time.time(),"recovered_from_gx":recovered,
         "cold_state_sha256":rehearse.state_hash(slot,rid)}
    if errors:
        doc["transport_errors"]=errors
    save(slot/"summary.json",doc)
    return doc


def execute(work,item,reg,binary,fd,resuming):
    slot=work/item["slot"]
    first=first_attempt(slot,reg,time.time())
    window=first["research_window"]
    gr={**reg["grants"][item["architecture"]],"research_window":window}
    grantfile=slot/"grant.json"
    if grantfile.exists() and read(grantfile)!=gr:
        raise ValueError("The enrolled research window or Goal grant changed")
    save(grantfile,gr)
    ready=Event()
    with native_session(slot,binary,reg,ready) as (env,errors):
        env["PATH"]=subscription.science_plugin_path(env)
        cli=native.native_cli(slot,env,fd)
        begun=cli("start","--goal",reg["goals"][slot.name],"--run-key",first["run_key"],"--goal-config",grantfile,local=True)
        rid=begun["run_id"]
        if first.get("run_id",rid)!=rid:
            raise ValueError("The original Run changed")
        save(slot/"first-attempt.json",{**first,"run_id":rid})
        with closing(rehearse.db_open(slot)) as db:
            parent,=db.execute("SELECT artifact_ref FROM artifacts WHERE run_id=? AND sha256=?",(rid,materials.PARENT_SHA)).fetchone()
        native.parent_observation(slot,rid,parent,cli)
        status,state=drive(slot,work,{**item,"host":reg["host"]},rid,cli,parent,ready,errors,window,resuming)
        return summarize_run(slot,rid,item,reg,window,status,state=state,errors=errors)



class OfflineLedger:
    """Use the original fixture transport's accounting projection with a read-only SQLite connection."""
    owner=transport.FixtureOwner
    report=physical.FundedTransport.report
    _totals=staticmethod(physical.FundedTransport._totals)

    def __init__(self,work,reg):
        self.path=work/"funding.sqlite"
        with self._db() as db:
            self.contract=json.loads(db.execute("SELECT contract FROM enrollment WHERE id=1").fetchone()[0])
        self.slots=self.contract["slots"]
        if self.contract["series"]!=reg["series"] or tuple(self.slots)!=SLOTS or self.contract["source"]!=self.owner.source:
            raise ValueError("Local meter differs from this enrollment")

    @contextmanager
    def _db(self):
        with closing(sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro",uri=True)) as db:
            db.row_factory=sqlite3.Row
            yield db


def usage(slot,reg,rid):
    roles=hosts.role_witness(slot,rid,strict=False)
    save(slot/"roles.json",{"roots":roles,"official_host_sha256":reg["official_binary_sha256"]})
    if reg["host"]==OAUTH:
        return host.reconcile(slot,rid)
    account=local.account(slot,roles,OfflineLedger(slot.parent,reg),False)
    turns={(r["thread_id"],t["turn_id"]) for r in roles for t in r["turns"]}
    totals=account["totals"]
    settled=(bool(account["requests"]) and not account["unmatched_turns"] and not totals["unresolved_requests"]
             and not totals["breaches"] and all(r["state"]=="SETTLED" and (r["thread_id"],r["turn_id"]) in turns for r in account["requests"]))
    return {**account,"status":"SETTLED" if settled else "HELD","total_tokens":totals["settled_tokens"] if settled else None,
            "covered_response_tokens":totals["settled_tokens"],"covered_response_count":len(account["requests"]),
            "fixture_only":True,"actual_model_inference_calls":0,"additional_currency_paid":0}


def account_case(slot,reg,rid,*,wait=False):
    end=read(slot/"first-attempt.json")["research_window"]["reconcile_until"]
    until=min(end,time.time()+reg["reconcile_seconds"]) if wait else 0
    while True:
        account=usage(slot,reg,rid)
        save(slot/"accounting.json",account)
        if subscription.settled(account) or time.time()>=until:
            from acceptance.science_execution.diagnostic_cost import summarize
            save(slot/"role-accounting.json",summarize(slot,rid,account,fixture=reg["host"]=="offline"))
            return account
        time.sleep(min(2,max(0,until-time.time())))


def recorded_run(slot):
    first=slot/"first-attempt.json"
    if not first.exists():
        return None
    doc=read(first)
    if doc.get("run_id"):
        return doc["run_id"]
    if not (slot/"state/gx.db").exists():
        return None
    with closing(rehearse.db_open(slot)) as db:
        from gx_core.p0 import workspace
        row=workspace.find_run(db,run_key=doc["run_key"])
    if row:
        save(first,{**doc,"run_id":row["run_id"]})
        return row["run_id"]
    return None


def snapshot(work,n):
    from acceptance.science_execution.witness import evaluate
    costs=work/n/"role-accounting.json"
    return {**evaluate(work,n),"role_accounting":read(costs) if costs.is_file() else None}


def operate(work,n,verb,route,binary,approval):
    with registration.locked(work) as fd:
        reg=read(work/"registration.json")
        result=read(work/"result.json")
        if (reg["host"]!=route or result["registration_sha256"]!=digest(work/"registration.json")
                or reg["protocol_sha256"]!=registration.sha(protocol(route))):
            raise ValueError("Select the registered host, protocol and original work directory")
        case=result["cases"][n]
        slot=work/n
        item=next(i for i in reg["slots"] if i["slot"]==n)
        def update(**changes):
            case.update(changes)
            save(work/"result.json",result)
            return dict(case)
        if verb=="run" and case["status"]!="REGISTERED":
            raise ValueError("This original case already started; use status, reconcile or resume")
        if verb!="run" and case["status"]=="REGISTERED":
            raise ValueError("This original case has not started")
        slot.mkdir(mode=0o700,exist_ok=True)
        (slot/"raw").mkdir(exist_ok=True)
        rid=recorded_run(slot)
        summary=read(slot/"summary.json") if (slot/"summary.json").exists() else {}
        first=read(slot/"first-attempt.json") if (slot/"first-attempt.json").exists() else {}
        expired=bool(first and time.time()>=first["research_window"]["drain_until"])
        closed=False
        if rid:
            with closing(rehearse.db_open(slot)) as db:
                found=db.execute("SELECT status FROM v6_goals WHERE run_id=?",(rid,)).fetchone()
                closed=bool(found and found[0]=="CLOSED")
        if verb=="reconcile" or closed or summary.get("status") in TERMINAL or expired:
            if not rid:
                return update(status="TIMEOUT" if expired else "HELD",
                              reason="WINDOW_EXPIRED_BEFORE_DURABLE_RUN" if expired else "ORIGINAL_RUN_ID_PENDING",
                              scientific_status="TIMEOUT" if expired else "INTERRUPTED")
            local_timeout=expired and not closed and summary.get("status")!="TERMINATED"
            if local_timeout and verb=="resume":
                from gx_core.domain import machines
                with closing(rehearse.db_open(slot)) as db:
                    run=db.execute("SELECT status FROM runs WHERE run_id=?",(rid,)).fetchone()
                if run and run[0] not in {"PAUSED",*machines.terminal_states("run")}:
                    stop_original(slot,rid,"COLD_DRAIN_EXPIRED",lambda:local_stop(slot,rid))
            science="CLOSED" if closed else "TIMEOUT" if local_timeout else summary.get("status","INTERRUPTED")
            if summary.get("status")!=science and science in TERMINAL:
                summarize_run(slot,rid,item,reg,first["research_window"],science,recovered=True)
            acct=account_case(slot,reg,rid)
            return update(status=science if subscription.settled(acct) else "HELD",run_id=rid,accounting=acct["status"],
                          total_tokens=acct["total_tokens"],scientific_status=science)
        # The second case can execute after a scientifically unsuccessful first case with settled final usage.
        if n=="02":
            prior=result["cases"]["01"]
            if prior.get("scientific_status") not in TERMINAL or not prior.get("run_id"):
                raise ValueError("The first original case is not terminal")
            original=account_case(work/"01",reg,prior["run_id"])
            if not subscription.settled(original) or not original["total_tokens"]:
                raise ValueError("The first original case has unconfirmed usage")
        if digest(binary)!=reg["official_binary_sha256"] or subscription.candidate()!=reg["source_commit"]:
            raise ValueError("The original candidate or official host changed")
        validate_materials(work,reg)
        if route==OAUTH:
            authority(work,binary,approval,reg)
            if rid:
                original=account_case(slot,reg,rid)
                if not subscription.settled(original):
                    return update(status="HELD",run_id=rid,accounting=original["status"],reason="ORIGINAL_EXPOSURE_PENDING")
        if verb=="resume":
            (slot/"stop-request.json").unlink(missing_ok=True)
        update(status="ACTIVE")
        try:
            summary=execute(work,item,reg,binary,fd,verb=="resume")
            rid=summary["run_id"]
            acct=account_case(slot,reg,rid,wait=route==OAUTH and summary["status"] in TERMINAL)
        except (ValueError,RuntimeError,OSError,sqlite3.Error,subprocess.SubprocessError) as err:
            update(status="HELD",run_id=recorded_run(slot),reason="ORIGINAL_RUN_REQUIRES_RECONCILIATION",error_type=type(err).__name__)
            raise
        return update(status=summary["status"] if subscription.settled(acct) else "HELD",run_id=rid,
                      scientific_status=summary["status"],accounting=acct["status"],total_tokens=acct["total_tokens"])


def status(work,result,number):
    from gx_core.p0 import research_window, workspace
    current={}
    for name,case in result["cases"].items():
        current[name]=dict(case)
        slot=work/name
        first=slot/"first-attempt.json"
        if not first.is_file():
            continue
        info=read(first)
        window=info["research_window"]
        current[name].update(research_window=window,research_window_phase=research_window.phase(window))
        if (slot/"state/gx.db").is_file():
            with closing(rehearse.db_open(slot)) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='p0_workspace_runs'").fetchone():
                    continue
                run=workspace.find_run(db,run_key=info["run_key"])
                if run:
                    current[name].update(run_id=run["run_id"],gx_status=run["status"])
    return current[number] if number else {**result,"cases":current}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation",choices=["preflight","register","run","resume","stop","reconcile","status","export"])
    parser.add_argument("--host",choices=("offline","oauth"),default="offline")
    parser.add_argument("--binary",type=Path,default=rehearse.BINARY)
    parser.add_argument("--work",type=Path)
    parser.add_argument("--approval",type=Path)
    parser.add_argument("--slot",choices=SLOTS)
    args=parser.parse_args(argv)
    work=args.work.resolve() if args.work else None
    route=OAUTH if args.host=="oauth" else "offline"
    op=args.operation
    if op!="preflight" and not work:
        parser.error("--work is required")
    if op in {"run","resume","stop","reconcile"} and not args.slot:
        parser.error("--slot is required")
    try:
        if op=="preflight":
            out=preflight(route,args.binary)
        elif op=="register":
            out=register(work,route,args.binary,args.approval)
        elif op in {"status","stop","export"}:
            reg=read(work/"registration.json")
            if reg["host"]!=route:
                raise ValueError("Select the original registered host")
            result=read(work/"result.json")
            if result["registration_sha256"]!=digest(work/"registration.json"):
                raise ValueError("Registration digest differs from the original series")
            if op=="status":
                out=status(work,result,args.slot)
            elif op=="export":
                out={"series":reg["series"],"host":route,"candidate":reg["source_commit"],"real_model":route==OAUTH,
                     "cases":{n:snapshot(work,n) if (work/n/"summary.json").exists() else result["cases"][n]
                              for n in ((args.slot,) if args.slot else SLOTS)}}
            else:
                if not (work/args.slot/"first-attempt.json").exists():
                    raise ValueError("This original case has not started")
                slot=work/args.slot
                save(slot/"stop-request.json",{"slot":args.slot,"time":time.time(),"reason":"operator requested stop"})
                rid=recorded_run(slot)
                if rid:
                    state=stop_original(slot,rid,"OPERATOR_STOP",lambda:local_stop(slot,rid))
                    out={"status":"STOP_REQUESTED","slot":args.slot,"run_id":rid,"gx_status":state["status"]}
                else:
                    out={"status":"STOP_REQUESTED","slot":args.slot,"run_id":None}
        else:
            out=operate(work,args.slot,op,route,args.binary,args.approval)
    except (ValueError,OSError,RuntimeError,sqlite3.Error,subprocess.SubprocessError) as exc:
        print(json.dumps({"status":"BLOCKED","reason":str(exc)},ensure_ascii=False))
        return 2
    print(json.dumps(out,indent=2,ensure_ascii=False))
    return 2 if out.get("status") in {"HELD","BLOCKED","PROVIDER_HOLD"} else 0


if __name__=="__main__":
    raise SystemExit(main())
