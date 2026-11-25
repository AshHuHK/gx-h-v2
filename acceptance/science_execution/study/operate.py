"""Enroll and operate a blind deposition through a normal GX Goal and official host."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT),str(ROOT/"gx-h")]
from acceptance.v622.public import batch, funding
from acceptance.v622.public.enrollment import digest, read, save
from acceptance.v623.oauth import host, pilot
from acceptance.v62c import rehearse
from acceptance.science_execution.study import protocol, synthetic
from gx_core.p0 import v621_policy
from acceptance.science_execution.diagnostic import first_attempt, recorded_run

METHOD_PATH = ROOT/"gx-h/plugins/installed/org.gx.block-influence/2.0.0/METHOD.md"


def now():
    return datetime.now(timezone.utc).isoformat()


def fixture(custody):
    from acceptance.v623.test_block_method import synthetic_cif
    src, desc, mapping = (custody/f for f in ("coordinates.cif","acquisition.json","mapping.json"))
    src.write_bytes(synthetic_cif(size=160))
    save(desc, {"source":"engineering synthetic coordinates","acquisition":"locally constructed geometric fixture",
                "publication":None,"sample":None,"sampling":None,"processing":None,
                "applicability":"Synthetic data; no biological independence claim."})
    save(mapping, {"schema":"gx.coordinate-position-map/1","structure_sha256":digest(src),"model":"1","entity":"1",
        "chains":["A","B","C"],"reference":"2RE9/synthetic-coordinate-system","segments":{
            "21-40":list(range(21,41)), "101-120":list(range(101,121))}})
    packet = {"blind_id":"engineering-specimen","windows":{w:{"eligible_masks":list(protocol.MASKS)} for w in protocol.INTERVALS},
              "scope_notes":["Transparent synthetic data for transport and numerical verification."]}
    return {"schema":"gx.archived-deposition.custody/1","status":"ELIGIBLE","blind_id":packet["blind_id"],"lead_packet":packet,
            **{k+"_path":str(p) for k,p in (("source",src),("mapping",mapping),("descriptor",desc))},
            **{k+"_sha256":digest(p) for k,p in (("source",src),("mapping",mapping),("descriptor",desc))}}


def seal(work, binary, *, custody=None, fixture_only=False):
    if work.exists() or work.is_relative_to(ROOT) or bool(custody)==fixture_only:
        raise ValueError("Choose a fresh external directory and exactly one custody source")
    record = read(custody) if custody else None
    if record is not None:
        archived = read(ROOT/"acceptance/v624/custody/receipts/eligibility.json")
        pins = ("schema","status","protocol_sha256","screening_sha256","blind_id","source_sha256","mapping_sha256","descriptor_sha256","lead_packet")
        if record.get("schema")!="gx.archived-deposition.custody/1" or any(record.get(k)!=archived.get(k) for k in pins):
            raise ValueError("Custody qualification differs from this series' registered screening result")
    work.mkdir(parents=True,mode=0o700)
    vault = work/"custody"
    vault.mkdir(mode=0o700)
    if fixture_only:
        record = fixture(vault)
    series = protocol.SERIES + ("-SYNTHETIC" if fixture_only else "")
    reg = {"schema":"gx.blind-study.enrollment/1","series":series,"synthetic":fixture_only,
           "candidate":pilot.candidate(),"registered_at":now(),"qualification":record["status"],
           "qualification_sha256":digest(custody) if custody else None,
           "new_work_seconds":90 if fixture_only else 1800,"drain_seconds":30 if fixture_only else 360,
           "reconcile_seconds":10 if fixture_only else 120, "model":rehearse.P["model"],"effort":rehearse.P["model_effort"]}
    if record["status"] != "ELIGIBLE":
        reg.update(status="NOT_ENROLLED", live_model_requests=0, physical_jobs=0)
        save(work/"seal.json",reg)
        return reg
    pin = read(ROOT/"release-assets/lock.json")["linux_x64"]["binary_sha256"]
    if digest(binary)!=pin or record["lead_packet"]["blind_id"]!=record["blind_id"]:
        raise ValueError("Invalid host or blind specimen binding")
    for key, filename in (("source","coordinates.cif"),("descriptor","acquisition.json"),("mapping","mapping.json")):
        src = Path(record[key+"_path"])
        if digest(src)!=record[key+"_sha256"]:
            raise ValueError("Qualification artifact changed: "+key)
        dest = vault/filename
        if dest!=src:
            shutil.copyfile(src,dest)
    slot = work/"lead"
    slot.mkdir()
    (slot/"raw").mkdir()
    shutil.copyfile(HERE/"SCIENCE_GOAL.md",slot/"goal.md")
    card = protocol.method_card(record["lead_packet"],METHOD_PATH.read_text())
    save(slot/"method-card.json",card)
    save(vault/"mapping-acquisition.json",{"source":"operator-frozen reference position correspondence","sha256":digest(vault/"mapping.json"),
         "acquisition":"Sequence alignment registered before geometry","applicability":"Companion to the original coordinate file."})
    save(vault/"companion.json", {"source":card["blind_id"]+"-mapping","path":str(vault/"mapping.json"),
        "metadata_path":str(vault/"mapping-acquisition.json"),"sha256":digest(vault/"mapping.json"),"role":"mapping","media_type":"application/json"})
    lim = {"tokens":1_000_000 if fixture_only else pilot.protocol()["gx_accounting_integer_max"],"worker_tasks":24,
           "api_calls":36,"job_submissions":2,"cpu_seconds":360,"wall_hours":1,"gpu_jobs":0,"gpu_minutes":0}
    grant = {"project":"blinded-deposition-quality","research_effort":"deep","evidence_policy":v621_policy.VERSION,
        "branch_delegation":False,"research_modules":[],"plugins":[protocol.PLUGIN],"capabilities":[protocol.METHOD],
        "run_budget":lim,"workspace_budget":lim,"initial_job_limit":2,"turn_token_budget":32768,
        "input_files":[{"role":"workflow_spec","path":str(slot/"method-card.json"),"sha256":digest(slot/"method-card.json")}]}
    if not fixture_only:
        grant["budget_profile"] = pilot.protocol()["gx_accounting_profile"]
    save(vault/"grant.json",grant)
    reg.update(status="ENROLLED", blind_id=card["blind_id"], host_sha256=pin,
               sealed_files={str(p.relative_to(work)):digest(p) for p in (slot/"goal.md",slot/"method-card.json",*sorted(vault.glob("*")))})
    save(work/"seal.json",reg)
    return reg


def open_once(work, rid, cli):
    vault = work/"custody"
    slot = work/"lead"
    card = read(slot/"method-card.json")
    with closing(rehearse.db_open(slot)) as db:
        inputs = json.loads(db.execute("SELECT inputs_json FROM runs WHERE run_id=?", (rid,)).fetchone()[0])
        card_ref = next(x["artifact_ref"] for x in inputs["files"] if x["role"]=="workflow_spec")
        primary = protocol.first_primary(protocol.accepted(db,rid),card,card_ref)
    if primary is None:
        return None
    assignments = cli("assign-material","--run-id",rid,"--after-decision",primary["decision_ref"],
        "--comparison-id",primary["comparison_id"],"--source",card["blind_id"]+"-structure","--role","structure",
        "--input",vault/"coordinates.cif","--sha256",digest(vault/"coordinates.cif"),"--media-type","chemical/x-mmcif",
        "--acquisition",vault/"acquisition.json","--companion",vault/"companion.json")["assignments"]
    if any(a["decision_ref"]!=primary["decision_ref"] or a["event_id"]<=primary["event_id"] for a in assignments):
        raise ValueError("Assignment must follow the accepted comparison")
    opening = {"run_id":rid,"commitment":primary,"assignments":assignments}
    prior = vault/"opening.json"
    if prior.is_file() and read(prior)!=opening:
        raise ValueError("The prior opening and GX disagree")
    save(prior,opening)
    return opening


def partial(work, rid, reg, status, reason, **data):
    slot = work/"lead"
    resources = []
    if rid:
        with closing(rehearse.db_open(slot)) as db:
            resources = [dict(r) for r in db.execute("SELECT job_id,status,measured_json FROM jobs WHERE run_id=? ORDER BY rowid",(rid,))]
    receipt = {"schema":"gx.blind-study.progress/1","run_id":rid,"series":reg["series"],"status":status,
               "reason":reason,"qualification":reg["qualification"],"jobs":resources,"recorded_at":now(),**data}
    save(work/"study-progress.json",receipt)
    return receipt


def account_original(slot, rid, window, *, wait=False):
    until = min(window["reconcile_until"],time.time()+120) if wait else 0
    while True:
        account = host.reconcile(slot,rid)
        save(slot/"accounting.json",account)
        if pilot.settled(account) or time.time()>=until:
            return account
        time.sleep(min(2,max(0,until-time.time())))


def run(work, binary, approval, diagnostics, *, reconcile_only=False):
    reg = read(work/"seal.json")
    if reg["status"]=="NOT_ENROLLED":
        return reg
    if (reg["candidate"]!=pilot.candidate() or digest(binary)!=reg["host_sha256"] or
            any(digest(work/k)!=v for k,v in reg["sealed_files"].items())):
        raise ValueError("Candidate or sealed materials changed")
    slot = work/"lead"
    ready = Event()
    with (work/".study.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        prior = recorded_run(slot)
        first_path = slot/"first-attempt.json"
        if prior and not reg["synthetic"]:
            window = read(first_path)["research_window"]
            account = account_original(slot,prior,window,wait=reconcile_only)
            if not pilot.settled(account):
                return partial(work,prior,reg,"HELD","ORIGINAL_USAGE_UNSETTLED",accounting=account["status"])
        if (work/"study-receipt.json").exists():
            old = read(work/"study-receipt.json")
            if prior!=old["run_id"] or old["cold_state_sha256"]!=rehearse.state_hash(slot,prior):
                raise ValueError("Stored original and closed cold replay disagree")
            return old
        if reconcile_only:
            return partial(work,prior,reg,"RECONCILED" if prior else "NOT_STARTED","READ_ONLY")
        ledger = None
        if reg["synthetic"]:
            ledger = funding.BatchFunding(work/"custody/account.sqlite",series=reg["series"],slots=["lead"],batch_cap=1_000_000)
            session = synthetic.session(slot,binary,ledger,ready)
        else:
            from acceptance.science_execution import approval as authorization
            if not approval or not diagnostics:
                raise ValueError("Supply this scientific series' separate OAuth authorization and both completed live diagnostics")
            pilot.check_candidate(reg["candidate"])
            control = host.preflight(binary,reg["model"],reg["effort"])
            if control["status"]!="READY":
                return partial(work,prior,reg,"HELD","STOCK_OAUTH_BLOCKED",codes=control["reason_codes"])
            account = control["account_ref"]
            authorization.verify_diagnostics(diagnostics,candidate=reg["candidate"],account_ref=account,approval_path=approval)
            receipt = authorization.verify(approval,work=work,candidate=reg["candidate"],series=reg["series"],account_ref=account)
            save(work/"custody/authorized.json", {"approval_sha256":receipt["approval_sha256"],"account_ref":account})
            session = host.session(slot,binary,{**reg,"account_ref":account})
        first = first_attempt(slot,reg,time.time())
        window = first["research_window"]
        if not prior and time.time()>=window["new_work_until"]:
            return partial(work,None,reg,"TIMEOUT","ORIGINAL_ADMISSION_WINDOW_ENDED",research_window=window)
        goal_grant = {**read(work/"custody/grant.json"), "research_window":window}
        grant_path = work/"custody/timed-grant.json"
        if grant_path.exists() and read(grant_path)!=goal_grant:
            raise ValueError("Original timed Goal grant changed")
        save(grant_path,goal_grant)
        rid = prior
        try:
            with session as env:
                env["PATH"] = pilot.science_plugin_path(env)
                cli = batch.native_cli(slot,env,lock.fileno())
                status = cli("start","--goal",(slot/"goal.md").read_text().strip(),"--run-key",first["run_key"],
                             "--goal-config",grant_path,local=True)
                rid = status["run_id"]
                if prior and prior!=rid:
                    raise ValueError("Resume did not select the enrolled original Run")
                save(first_path,{**first,"run_id":rid})
                if (work/"custody/opening.json").exists() or (prior and status["goal_status"]=="CLOSED"):
                    if open_once(work,rid,cli):
                        ready.set()
                if status["goal_status"]!="CLOSED":
                    seconds = max(0,window["drain_until"]-time.time())
                    if seconds:
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            driver = pool.submit(cli,"resume" if prior else "run","--run-id",rid,"--timeout",seconds,timeout=seconds+20)
                            try:
                                while not driver.done():
                                    if not reg["synthetic"] and pilot.active_provider_hold(slot,rid):
                                        ready.set()
                                        cli("stop","--run-id",rid,local=True)
                                        return partial(work,rid,reg,"HELD","PROVIDER_HOLD",research_window=window)
                                    if time.time()>=window["drain_until"]:
                                        break
                                    if not ready.is_set() and time.time()<window["new_work_until"] and open_once(work,rid,cli):
                                        ready.set()
                                    time.sleep(.12)
                                status = driver.result()
                            except BaseException:
                                ready.set()
                                cli("stop","--run-id",rid,local=True)
                                raise
                if status["goal_status"]!="CLOSED":
                    status = cli("stop","--run-id",rid,local=True)
                    return partial(work,rid,reg,"INCOMPLETE","DRAIN_ENDED_OR_QUIESCENT",research_window=window,state=status)
                if ready.is_set():
                    unchanged = rehearse.state_hash(slot,rid)
                    open_once(work,rid,cli)
                    if unchanged!=rehearse.state_hash(slot,rid):
                        raise ValueError("Repeated opening changed a closed Run")
                before = rehearse.state_hash(slot,rid)
                cold = cli("resume","--run-id",rid,"--timeout",4,local=True)
                if cold["goal_status"]!="CLOSED" or before!=rehearse.state_hash(slot,rid):
                    raise ValueError("Cold recovery changed completed work")
            if not reg["synthetic"]:
                account = account_original(slot,rid,window,wait=True)
                if not pilot.settled(account):
                    return partial(work,rid,reg,"HELD","ORIGINAL_USAGE_UNSETTLED",accounting=account["status"])
            if not ready.is_set():
                return partial(work,rid,reg,"INCOMPLETE","GOAL_CLOSED_WITHOUT_QUALIFYING_PRIMARY",research_window=window)
            from acceptance.science_execution.study import receipt
            return receipt.export(work,rid,reg,before,ledger)
        except (ValueError,RuntimeError,OSError,subprocess.SubprocessError) as exc:
            state = partial(work,rid,reg,"INCOMPLETE",str(exc),research_window=window)
            if rid and not reg["synthetic"]:
                account_original(slot,rid,window)
            if reg["synthetic"]:
                raise
            return state


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("cmd",choices=("seal","operate","reconcile"))
    p.add_argument("--work",type=Path,required=True)
    p.add_argument("--binary",type=Path,default=rehearse.BINARY)
    p.add_argument("--custody",type=Path)
    p.add_argument("--synthetic",action="store_true")
    p.add_argument("--approval",type=Path)
    p.add_argument("--diagnostics",type=Path)
    a=p.parse_args()
    w,b=a.work.resolve(),a.binary.resolve()
    result=seal(w,b,custody=a.custody,fixture_only=a.synthetic) if a.cmd=="seal" else run(w,b,a.approval,a.diagnostics,reconcile_only=a.cmd=="reconcile")
    print(json.dumps({k:result.get(k) for k in ("series","status","run_id","qualification","classification")},ensure_ascii=False))
