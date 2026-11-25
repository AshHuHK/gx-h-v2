"""Operate a registered public Goal batch. The host chooses scientific work and may close early."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from threading import Event, Lock
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.v62c import materials, rehearse as old
from acceptance.v622.public import enrollment, funding, offline
from acceptance.v622.public.enrollment import PROTOCOL, digest, read, save


def native_cli(slot, env, batch_lock):
    output = slot / "raw/commands.json"
    commands = read(output) if output.exists() else []
    lock = Lock()

    def call(verb, *args, local=False, timeout=60):
        argv = [sys.executable, "-B", str(ROOT / "bin/gx-p0"), verb, "--state", str(slot / "state"), *map(str, args)]
        with subprocess.Popen(argv, env={**env, **({"GX_CODEX_OFFLINE":"1"} if local else {})},
                              cwd=slot, start_new_session=True, pass_fds=(batch_lock,), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) as proc:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except BaseException:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                raise
        with lock:
            commands.append({"argv":argv, "exit":proc.returncode, "stdout":stdout, "stderr":stderr})
            save(output, commands)
        if proc.returncode:
            raise ValueError(f"GX {verb} exited {proc.returncode}: {(stderr or stdout)[-800:]}")
        return json.loads(stdout)
    return call


def first_plan(slot, rid):
    with closing(old.db_open(slot)) as conn:
        plan = conn.execute("""SELECT d.decision_ref FROM v6_decisions d JOIN v6_decision_tasks t USING(task_id,run_id)
                               WHERE d.run_id=? AND t.scope='run' ORDER BY d.rowid LIMIT 1""", (rid,)).fetchone()
        modules = conn.execute("""SELECT module_instance_id FROM v62_modules
                                  WHERE run_id=? AND status='OPEN' AND module_id='data_inventory'""", (rid,)).fetchall()
        status = conn.execute("SELECT status FROM v6_goals WHERE run_id=?", (rid,)).fetchone()[0]
    return plan[0] if plan else None, [row[0] for row in modules], status


def assign(slot, work, item, rid, cli, parent, decision, modules):
    """The exact operation is stored before the call and can reconcile a lost operator receipt."""
    digest_expected = PROTOCOL["materials"][item["code"]]["sha256"]
    source = work / "custody" / item["code"]
    intent = slot / "assignment-request.json"
    if intent.exists():
        request = read(intent)
    else:
        request = {"decision": decision, "module": modules[0] if len(modules) == 1 else None}
        save(intent, request)
    transformation = read(source / "transform.json")
    evidence = {**materials.parent_acquisition(), "processing":["coordinate-record withholding/" + digest(source / "transform.json")],
                "transformation":transformation, "derived_from":[{"artifact_ref":parent,"sha256":materials.PARENT_SHA}]}
    save(slot / "assigned-acquisition.json", evidence)
    args = ["--run-id",rid,"--after-decision",request["decision"],"--source","assigned-specimen","--role","structure",
            "--input",source / "specimen.cif","--sha256",digest_expected,"--acquisition",slot / "assigned-acquisition.json",
            "--media-type","chemical/x-mmcif"]
    if request["module"]:
        args.extend(["--module-instance",request["module"]])
    receipt = cli("assign-material", *args)
    save(slot / "assignment.json", receipt)
    return receipt


def remaining(deadline):
    return max(0, deadline - time.time())


def drive(slot, work, item, rid, cli, parent, ready, deadline):
    """Use the normal Driver. Only the operator intervention depends on the first accepted global plan."""
    assignment_path = slot / "assignment.json"
    if assignment_path.exists():
        ready.set()
    elif (slot / "assignment-request.json").exists():
        # Acknowledgment may have been lost after GX committed the assignment. Reconcile
        # the original operator operation even if the same Goal closed before restart.
        with closing(old.db_open(slot)) as db:
            accepted = db.execute("SELECT 1 FROM events WHERE run_id=? AND kind='v621.material.assigned'", (rid,)).fetchone()
        if accepted:
            assign(slot,work,item,rid,cli,parent,None,[])
            ready.set()
    status = cli("status", "--run-id",rid,local=True)
    if status.get("goal_status") == "CLOSED":
        return status, "CLOSED"
    if not remaining(deadline):
        return cli("stop", "--run-id", rid, local=True), "TIMEOUT"

    def execute():
        seconds = max(0.1, remaining(deadline))
        return cli("run", "--run-id",rid,"--timeout",seconds,timeout=math.ceil(seconds)+20)

    with ThreadPoolExecutor(max_workers=1) as workers:
        task = workers.submit(execute)
        try:
            while not ready.is_set():
                plan, modules, goal = first_plan(slot,rid)
                if goal == "CLOSED":
                    break
                if plan:
                    assign(slot,work,item,rid,cli,parent,plan,modules)
                    ready.set()
                    break
                if task.done() or not remaining(deadline):
                    break
                time.sleep(.025)
        finally:
            ready.set()
        last = task.result()
    if last["goal_status"] == "OPEN" and assignment_path.exists() and remaining(deadline) > 0.1:
        last = execute()
    if last["goal_status"] == "CLOSED":
        return last, "CLOSED"
    if remaining(deadline) <= 0.1:
        return cli("stop","--run-id",rid,local=True), "TIMEOUT"
    return last, "WAITING"


def signed_evidence(slot, rid, wire):
    with closing(old.db_open(slot)) as conn:
        jobs = [dict(row) for row in conn.execute("""SELECT job_id,status,capability_id,
                          capability_version,measured_json FROM jobs WHERE run_id=? ORDER BY rowid""", (rid,))]
        claims = [json.loads(row[0]) for row in conn.execute("""SELECT document_json FROM hypothesis_revisions
                  JOIN hypotheses USING(hypothesis_id) WHERE run_id=? ORDER BY revision""", (rid,))]
        reports = set(row[0] for row in conn.execute("SELECT report_ref FROM v62_module_reports WHERE run_id=?", (rid,)))
        received = {row[0]:json.loads(row[1]) for row in conn.execute("""SELECT c.call_id,c.result_json FROM
                     p0_root_calls c JOIN p0_roots r USING(thread_id) WHERE r.run_id=? AND json_type(c.result_json,'$.report_ref')='text'""", (rid,))}
    replays = []
    for call in wire:
        if call["tool"] == "gx_submit_module_report" and received.get(call["call_id"],{}).get("report_ref") in reports:
            replays.append(old.replay_report(slot,rid,[call]))
    return jobs, claims, replays


def summarize(slot, rid, reg, item, wire, receipt, final, status, cli, ledger, wall):
    if not wire:
        raise ValueError("Official host has no captured delivery")
    raw = slot / "raw/requests.jsonl"
    rows = [json.loads(line) for line in raw.read_text().splitlines() if line.strip()] if raw.is_file() else []
    fingerprint = old.state_hash(slot,rid)
    before = len(wire)
    jobs, claims, replays = signed_evidence(slot,rid,wire)
    if status == "CLOSED":
        cold = cli("resume","--run-id",rid,"--timeout",5,local=True)
        if (cold["graph_head"] != final["graph_head"] or cold["resources"] != final["resources"]
                or old.state_hash(slot,rid) != fingerprint or len(wire) != before):
            raise ValueError("Terminal replay changed work or funding")
    initial = wire[0]
    for item_pin in PROTOCOL["materials"].values():
        if isinstance(item_pin,dict) and item_pin["sha256"] in json.dumps(initial["brief"]):
            raise ValueError("Specimen assignment leaked before the first plan")
    if any((r["model"],r["effort"]) != (reg["wire_model"],reg["wire_effort"]) for r in wire):
        raise ValueError("Observed model or effort differs from the registered settings")
    accounting = funding.check_slot_receipts(ledger,slot.name,rows,require_reconciled=(status == "CLOSED"))
    role_usage = {}
    for category in ("run","module"):
        scopes = [item for name,item in accounting.items() if (name=="run")==(category=="run")]
        totals = [item["fixture_total_tokens"] for item in scopes]
        briefs = {r["brief_text"] for r in wire if (r["brief"]["scope"]=="run")==(category=="run")}
        role_usage[category] = {"turns":len(briefs), "brief_utf8_bytes":sum(len(b.encode()) for b in briefs),
                                "scripted_model_requests":sum(item["requests"] for item in scopes),
                                "held_tokens":sum(item["held_tokens"] for item in scopes),
                                "scripted_response_tokens":sum(totals) if None not in totals else None}
    assignment = read(slot / "assignment.json") if (slot / "assignment.json").is_file() else None
    plan, _, _ = first_plan(slot,rid)
    return {"status":status,"series":reg["series"],"classification":reg["scope"],"architecture":item["architecture"],
            "slot":item["slot"],"run_id":rid,"initial_plan_ref":plan,"initial_brief":initial["brief"],
            "initial_tools":initial["tools"],"assignment":assignment,"parent_acquisition":receipt,
            "resources":final["resources"],"claims":claims,"jobs":jobs,"cold_state_sha256":fingerprint,
            "cold_report_replays":replays,"cold_report_replay":replays[0] if len(replays)==1 else {"redeliveries":sum(r["redeliveries"] for r in replays)},
            "role_usage":role_usage,"accounting_by_scope":accounting,"wall_seconds":round(wall,3),
            "paid_model_calls":0,"provider_tokens":None,"provider_usd":None}


def parent_observation(slot, rid, parent, cli):
    source = "archive-reference"
    save(slot / "parent.json",materials.parent_acquisition())
    with closing(old.db_open(slot)) as db:
        observed = db.execute("""SELECT event_id,payload_json FROM events WHERE run_id=?
                                   AND kind='v6.observation.added' AND subject_id=?""",(rid,source)).fetchall()
    if observed:
        payload = json.loads(observed[0]["payload_json"])
        if (len(observed)!=1 or payload.get("scope")!="run" or payload.get("source")!=source
                or payload.get("artifact_ref")!=parent or payload.get("sha256")!=materials.PARENT_SHA
                or (payload.get("acquisition") or {}).get("source_identity",{}).get("status")!="matched_accession"):
            raise ValueError("Existing parent observation differs from the registered source")
        return {"event_id":observed[0]["event_id"], **payload}
    return cli("observe","--run-id",rid,"--source",source,"--artifact-ref",parent,
               "--sha256",materials.PARENT_SHA,"--acquisition",slot / "parent.json")


def run_slot(work, item, reg, binary, ledger, batch_lock):
    slot = work / item["slot"]
    slot.mkdir(mode=0o700,exist_ok=True)
    (slot / "raw").mkdir(exist_ok=True)
    attempt = slot / "first-attempt.json"
    if not attempt.exists():
        started = time.time()
        save(attempt,{"start":started,"deadline":started+PROTOCOL["time_limit_seconds"],
                      "slot":item["slot"],"run_key":"public-coverage"})
    enrolled = read(attempt)
    save(slot / "grant.json",reg["grants"][item["architecture"]])
    ready = Event()
    with offline.session(slot,binary,item.get("behavior","standard"),ready,ledger) as (env,wire,peer):
        cli = native_cli(slot,env,batch_lock)
        status = cli("start","--goal",old.GOAL,"--run-key",enrolled["run_key"],"--goal-config",slot/"grant.json")
        rid = status["run_id"]
        with closing(old.db_open(slot)) as db:
            parent, = db.execute("SELECT artifact_ref FROM artifacts WHERE run_id=? AND sha256=?",
                                 (rid,materials.PARENT_SHA)).fetchone()
        parent_receipt = parent_observation(slot,rid,parent,cli)
        final,status = drive(slot,work,item,rid,cli,parent,ready,enrolled["deadline"])
        if any(r.get("error") for r in peer.rows):
            raise ValueError("The trusted local Responses endpoint reported an error")
        summary = summarize(slot,rid,reg,item,wire,parent_receipt,final,status,cli,ledger,time.time()-enrolled["start"])
        save(slot / "summary.json",summary)
        return summary


def process(work, suite, binary, resume=False, diagnostic=None, assess=None):
    if (work.exists() != resume or work.is_relative_to(ROOT)):
        raise ValueError("Choose a fresh external directory or explicitly resume its original enrollment")
    work.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (work / ".batch.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        reg = enrollment.register(work,suite,binary)
        if diagnostic:
            if resume or len(reg["slots"]) != 4:
                raise ValueError("Use an independent fresh diagnostic directory")
            reg["slots"] = [next(x for x in reg["slots"] if x["slot"]==diagnostic)]
            reg["diagnostic_only"] = True
            save(work/"registration.json",reg)
        if resume and reg.get("diagnostic_only"):
            raise ValueError("A single-slot diagnostic cannot enroll a formal batch")
        ledger = funding.BatchFunding(work / "funding.sqlite",series=reg["series"],slots=reg["slots"],
                                     slot_cap=PROTOCOL["budget"]["tokens_per_slot"],
                                     batch_cap=PROTOCOL["budget"]["tokens_whole_public_batch"])
        result = {"series":reg["series"],"classification":reg["scope"],"registration_sha256":digest(work / "registration.json"),
                  "public_live_slots_enrolled":0,"original_private_slots_enrolled":0,"provider_tokens":None,
                  "paid_model_calls":0,"provider_usd":None,"cases":{s["slot"]:{"status":"NOT_STARTED"} for s in reg["slots"]}}
        if (work/"result.json").is_file():
            result = read(work / "result.json")
        try:
            for item in reg["slots"]:
                num = item["slot"]
                case = result["cases"][num]
                if case["status"] == "CLOSED":
                    continue
                if case["status"] in ("TIMEOUT","FAIL","HELD"):
                    break
                try:
                    slot = work / num
                    if (slot / "summary.json").is_file() and read(slot / "summary.json")["status"] != "WAITING":
                        summary = read(slot / "summary.json")
                    else:
                        result["cases"][num] = {"status":"ACTIVE"}
                        save(work/"result.json",result)
                        summary = run_slot(work,item,reg,binary,ledger,lock.fileno())
                    terminal = {k:summary[k] for k in ("status","run_id","wall_seconds","role_usage","paid_model_calls")}
                    terminal["job_count"] = len(summary["jobs"])
                    if summary["status"] == "CLOSED" and assess:
                        result["cases"][num] = {**terminal,"status":"AWAITING_REVIEW"}
                        save(work/"result.json",result)
                        rating = assess(work,num)
                        if rating.get("unsupported_support") or rating.get("independent_quantity_mismatch"):
                            raise ValueError("Formal result conflicts with original data or evidence qualification")
                    result["cases"][num] = terminal
                    if summary["status"] != "CLOSED":
                        break
                except Exception as exc:
                    current = result["cases"][num]
                    try:
                        with closing(old.db_open(work/num)) as conn:
                            row = conn.execute("SELECT run_id FROM runs ORDER BY rowid LIMIT 1").fetchone()
                        if row:
                            current["run_id"] = row[0]
                    except sqlite3.Error:
                        pass
                    current.update(status="HELD" if isinstance(exc,funding.FundingBlocked) else "FAIL",
                                   reason=f"{type(exc).__name__}: {exc}")
                    break
                finally:
                    save(work/"result.json",result)
        finally:
            save(work/"funding.json",ledger.report())
        return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work",type=Path,required=True)
    ap.add_argument("--suite",choices=("standard","controls"),default="standard")
    ap.add_argument("--host",choices=("offline","live"),required=True)
    ap.add_argument("--resume",action="store_true")
    ap.add_argument("--diagnostic-slot",choices=("01","02","03","04"))
    ap.add_argument("--binary",type=Path,default=old.BINARY)
    args = ap.parse_args()
    try:
        funding.require_backend(args.host)
    except funding.FundingBlocked as exc:
        print(json.dumps({"status":"BLOCKED","reason":str(exc),"paid_calls":0}))
        return 2
    from acceptance.v622.public import rate
    result = process(args.work.resolve(),args.suite,args.binary.resolve(),args.resume,args.diagnostic_slot,
                     assess=None if args.diagnostic_slot else rate.assess_slot)
    print(json.dumps(result,indent=2))
    return int(any(c["status"]!="CLOSED" for c in result["cases"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
