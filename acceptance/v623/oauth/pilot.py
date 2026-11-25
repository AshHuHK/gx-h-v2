"""Run the four original public assignments in order on the approved included ChatGPT subscription."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.v622.public import batch as native
from acceptance.v622.public.enrollment import digest, read, save
from acceptance.v623.live import registration
from acceptance.v62c import materials, rehearse
from acceptance.v623.oauth import authorization

SETTLED = "SETTLED"
ENDS = {"CLOSED", "TIMEOUT", "TERMINATED"}
SLOTS = ("01", "02", "03", "04")


def protocol():
    route = read(HERE / "protocol.json")
    base = registration.P
    if route["science_protocol_sha256"] != digest(registration.HERE / "protocol.json"):
        raise ValueError("The frozen public scientific protocol changed")
    data = {k: base[k] for k in ("slots", "model", "model_effort", "research_effort", "sampling", "materials",
        "assay", "threshold", "evidence_policy", "module", "time_limit_seconds", "public_slots", "intervention")}
    return {**data, **route}


def cohort_grant(architecture, p):
    spec = registration.grant(architecture)
    spec["budget_profile"] = p["gx_accounting_profile"]
    for name in ("run_budget", "workspace_budget"):
        spec[name] = {**spec[name], "tokens": p["gx_accounting_integer_max"]}
    return spec


def science_plugin_path(env):
    from scripts.acceptance_dependencies import check_version, select_node, versions
    version = read(ROOT / "release-assets/lock.json")["deterministic_test_runtime"]["node"]
    node = select_node(ROOT, version, None, {"PATH": os.environ.get("PATH", os.defpath)})
    check_version("Node", versions([str(node), "--version"], env), "v" + version)
    return str(node.parent) + os.pathsep + env["PATH"]


def candidate():
    return subprocess.check_output(["git","rev-parse","HEAD"], cwd=ROOT, text=True).strip()


def check_candidate(ref):
    if candidate() != ref or subprocess.check_output(["git","status","--porcelain"], cwd=ROOT):
        raise ValueError("A direct OAuth admission requires the approved, clean original candidate")


def check_approval(work, approval, approval_sha):
    if not approval or not approval_sha:
        raise ValueError("The PI conversation approval file and SHA are required")
    grant = authorization.verify(approval, approval_sha)
    if Path(grant["work"]).resolve() != work or grant["series"] != authorization.SERIES:
        raise ValueError("The existing approval is bound to a different cohort")
    check_candidate(grant["candidate"])
    return grant


def ensure_ready(binary, reg):
    from acceptance.v623.oauth import host
    proof = host.preflight(binary, reg["model"], reg["model_effort"])
    if proof["status"] != "READY":
        raise ValueError("Direct included OAuth unavailable: " + str(proof.get("reason_codes", proof["status"])))
    if reg.get("account_ref") and proof.get("account_ref") != reg["account_ref"]:
        raise ValueError("The original subscription account changed")
    return proof


def enrollment(work, binary, approval, approval_sha):
    grant = check_approval(work, approval, approval_sha)
    if work.is_relative_to(ROOT):
        raise ValueError("The registered material and judge custody belong outside the source checkout")
    p = protocol()
    pre = ensure_ready(binary, p)
    work.mkdir(parents=True, mode=0o700, exist_ok=True)
    with registration.locked(work):
        registry = work / "registration.json"
        if not (work / "custody").exists():
            if registry.exists():
                raise ValueError("An existing enrollment lost its original material custody")
            with TemporaryDirectory(dir=work) as path:
                stage = Path(path) / "custody"
                materials.prepare(stage)
                stage.rename(work / "custody")
        for code in ("P4", "K7"):
            owner = work / "custody" / code
            expected = p["materials"][code]
            if digest(owner/"specimen.cif") != expected["sha256"] or digest(owner/"transform.json") != expected["transform_sha256"]:
                raise ValueError("Public material does not match the frozen source and transformation")
        doc = {**p, "host":"direct_chatgpt_oauth", "scope":"PUBLIC_OAUTH_SUBSCRIPTION",
               "source_commit": grant["candidate"], "protocol_sha256":registration.sha(p),
               "approval_sha256":approval_sha, "official_binary_sha256":digest(binary), "account_ref":pre["account_ref"],
               "binary":str(binary.resolve()), "effort":p["model_effort"], "wire_model":p["model"], "wire_effort":p["model_effort"],
               "oracle_sha256":digest(work/"custody/oracle.json"),
               "grants":{arm:cohort_grant(arm,p) for arm in ("single", "delegation_available")}}
        if registry.exists() and read(registry) != doc:
            raise ValueError("The existing four original assignments are bound to different admission data")
        if not registry.exists():
            save(registry, doc)
            save(work/"preflight.json", pre)
        result = work/"result.json"
        if not result.exists():
            if any((work/slot/"first-attempt.json").exists() for slot in SLOTS):
                raise ValueError("The original batch result index is missing; preserve existing Runs")
            save(result, {"series":p["series"], "registration_sha256":digest(registry),
                          "cases":{slot:{"status":"REGISTERED"} for slot in SLOTS}})
        return doc


def reconcile_assignment(slot, work, item, rid, cli, parent):
    """GX refuses assignment unless this is still the first accepted Lead plan."""
    if (slot/"assignment.json").exists():
        return True
    plan, modules, goal = native.first_plan(slot,rid)
    if not plan:
        return False
    if goal != "OPEN":
        if not (slot/"assignment-request.json").exists():
            return False
        with closing(rehearse.db_open(slot)) as db:
            found = db.execute("SELECT 1 FROM events WHERE run_id=? AND kind='v621.material.assigned'", (rid,)).fetchone()
        if not found:
            return False
    native.assign(slot,work,item,rid,cli,parent,plan,modules)
    return True


def active_provider_hold(slot,rid):
    # Use GX's original official account/turn notifications. An expired throttle remains historical.
    with closing(rehearse.db_open(slot)) as conn:
        if conn.execute("SELECT 1 FROM p0_provider_limits WHERE provider='openai' AND blocked_until>? LIMIT 1",
                        (time.time(),)).fetchone():
            return True
        return bool(conn.execute("""SELECT 1 FROM p0_attempt_waits w JOIN p0_roots r USING(root_key)
            WHERE r.run_id=? AND w.reason='provider_backoff' AND w.ended_at IS NULL LIMIT 1""",(rid,)).fetchone())


def drive(slot,work,item,rid,cli,parent,deadline,resume):
    status = cli("status","--run-id",rid,local=True)
    assigned = reconcile_assignment(slot,work,item,rid,cli,parent)
    if status["goal_status"] == "CLOSED":
        return "CLOSED",status
    verb = "resume" if resume else "run"
    while time.time() < deadline:
        seconds = max(0.1, deadline-time.time())
        if active_provider_hold(slot,rid):
            return "PROVIDER_HOLD",cli("stop","--run-id",rid,local=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(cli, verb,"--run-id",rid,"--timeout",seconds,timeout=seconds+20)
            try:
                while not running.done():
                    if time.time() >= deadline:
                        return "TIMEOUT",cli("stop","--run-id",rid,local=True)
                    if active_provider_hold(slot,rid):
                        return "PROVIDER_HOLD",cli("stop","--run-id",rid,local=True)
                    if (slot/"stop-request.json").is_file():
                        return "PAUSED",cli("stop","--run-id",rid,local=True)
                    if not assigned:
                        assigned = reconcile_assignment(slot,work,item,rid,cli,parent)
                    time.sleep(0.05)
            except BaseException:
                cli("stop","--run-id",rid,local=True)
                raise
            status = running.result()
        previously_assigned = assigned
        if not assigned:
            assigned = reconcile_assignment(slot,work,item,rid,cli,parent)
        if status["goal_status"] == "CLOSED":
            return "CLOSED",status
        if time.time() >= deadline:
            break
        if not assigned or (previously_assigned and (status.get("waiting_for") or status.get("wake",{}).get("state")=="quiescent")):
            return "WAITING",status
        verb = "run"
    return "TIMEOUT",cli("stop","--run-id",rid,local=True)


def run_science(slot,work,item,reg,binary,fd,resume):
    from acceptance.v623.oauth import host
    first = slot/"first-attempt.json"
    if first.exists():
        original = read(first)
    else:
        now = time.time()
        original = {"series":reg["series"],"slot":item["slot"],"run_key":"oauth-public-"+item["slot"],
                    "start":now,"deadline":now+reg["time_limit_seconds"]}
        save(first, original)
    gr = reg["grants"][item["architecture"]]
    if (slot/"grant.json").exists() and read(slot/"grant.json") != gr:
        raise ValueError("The saved Run grant changed")
    save(slot/"grant.json",gr)
    with host.session(slot,binary,reg) as env:
        env["PATH"] = science_plugin_path(env)
        cli = native.native_cli(slot,env,fd)
        begun = cli("start","--goal",rehearse.GOAL,"--run-key",original["run_key"],"--goal-config",slot/"grant.json",local=True)
        rid = begun["run_id"]
        if original.get("run_id", rid) != rid:
            raise ValueError("The original slot cannot be rebound to another Run")
        save(first, {**original,"run_id":rid})
        with closing(rehearse.db_open(slot)) as db:
            parent, = db.execute("SELECT artifact_ref FROM artifacts WHERE run_id=? AND sha256=?",(rid,materials.PARENT_SHA)).fetchone()
        origin = native.parent_observation(slot,rid,parent,cli)
        outcome,state = drive(slot,work,item,rid,cli,parent,original["deadline"],resume)
        initial,_,_ = native.first_plan(slot,rid)
        with closing(rehearse.db_open(slot)) as db:
            jobs = [dict(row) for row in db.execute("SELECT job_id,status,capability_version FROM jobs WHERE run_id=? ORDER BY rowid",(rid,))]
        summary = {"series":reg["series"],"slot":item["slot"],"run_id":rid,"architecture":item["architecture"],
                   "status":outcome,"host":reg["host"],"classification":reg["scope"],"jobs":jobs,
                   "initial_plan_ref":initial,"parent_acquisition":origin,
                   "assignment":read(slot/"assignment.json") if (slot/"assignment.json").exists() else None,
                   "resources":state.get("resources"),"original_deadline":original["deadline"],
                   "wall_seconds":round(time.time()-original["start"],3),"cold_state_sha256":rehearse.state_hash(slot,rid)}
        save(slot/"summary.json",summary)
        return summary


def settled(receipt):
    return (receipt.get("status") == SETTLED and type(receipt.get("total_tokens")) is int
            and receipt["total_tokens"] >= 0 and receipt.get("can_continue") is not False)


def previous_ready(result, number):
    for prev in SLOTS[:SLOTS.index(number)]:
        if result["cases"][prev]["status"] != SETTLED:
            raise ValueError("Earlier original slot still requires completion, usage reconciliation or adjudication")


def verified_prior_usage(work, number, reg, result, binary):
    from acceptance.v623.oauth import host, judge
    previous_ready(result,number)
    for prev in SLOTS[:SLOTS.index(number)]:
        original = result["cases"][prev]
        scientific = host.reconcile(work/prev,original["run_id"])
        review = judge.reconcile(work,prev,reg,binary=binary,reg=reg)
        if (not settled(scientific) or not settled(review)
                or review.get("judgment_status") not in ("REVIEWED","NO_INTERPRETABLE_DECISION")
                or scientific["total_tokens"] != original["scientific_tokens"]
                or review["total_tokens"] != original["judge_tokens"]):
            original.update(status="HELD",reason="EARLIER_RECEIPT_CHANGED")
            save(work/"result.json",result)
            raise ValueError("An earlier slot or its judge requires usage reconciliation")


def process(work, number, verb, binary, approval, approval_sha):
    from acceptance.v623.oauth import host, judge
    authorization_doc = check_approval(work, approval, approval_sha)
    with registration.locked(work) as fd:
        reg, result = read(work/"registration.json"), read(work/"result.json")
        if (reg["source_commit"] != authorization_doc["candidate"] or reg["approval_sha256"] != approval_sha
                or result["registration_sha256"] != digest(work/"registration.json")
                or reg["protocol_sha256"] != registration.sha(protocol()) or reg["official_binary_sha256"] != digest(binary)):
            raise ValueError("Original candidate, protocol, authorization or host does not match this enrollment")
        verified_prior_usage(work,number,reg,result,binary)
        slot = work/number
        item = next(i for i in reg["slots"] if i["slot"] == number)
        old_case = result["cases"][number]
        if old_case["status"] == SETTLED and verb != "reconcile":
            return old_case
        if verb == "run" and old_case["status"] != "REGISTERED":
            raise ValueError("Resume the original slot")
        if verb != "run" and old_case["status"] == "REGISTERED":
            raise ValueError("The requested original slot has not started")
        slot.mkdir(mode=0o700,exist_ok=True)
        (slot/"raw").mkdir(exist_ok=True)
        progress = dict(old_case)
        def update(**data):
            progress.update(data)
            result["cases"][number]=progress
            save(work/"result.json",result)
            return progress

        resume = verb == "resume"
        if resume:
            (slot/"stop-request.json").unlink(missing_ok=True)
        if verb != "reconcile":
            ensure_ready(binary,reg)
        summary = read(slot/"summary.json") if (slot/"summary.json").exists() else {}
        attempt = read(slot/"first-attempt.json") if (slot/"first-attempt.json").exists() else {}
        rid = summary.get("run_id", attempt.get("run_id"))
        if rid:
            science_usage = host.reconcile(slot,rid)
            save(slot/"accounting.json",science_usage)
            if not settled(science_usage):
                return update(status="HELD", run_id=rid, accounting=science_usage["status"])
        if summary.get("status") not in ENDS and verb != "reconcile":
            update(status="ACTIVE")
            try:
                summary = run_science(slot,work,item,reg,binary,fd,resume)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                first = read(slot/"first-attempt.json") if (slot/"first-attempt.json").exists() else {}
                update(status="HELD",run_id=first.get("run_id"),reason="ORIGINAL_RUN_REQUIRES_RECONCILIATION",
                       error_type=type(exc).__name__)
                raise
            rid = summary["run_id"]
        if not rid:
            return update(status="HELD", reason="ORIGINAL_RUN_ID_PENDING")
        usage = host.reconcile(slot,rid)
        save(slot/"accounting.json",usage)
        if not settled(usage):
            return update(status="HELD", run_id=rid, accounting=usage["status"])
        if summary.get("status") not in ENDS:
            return update(status=summary.get("status","INTERRUPTED"),run_id=rid,accounting=SETTLED)
        if not usage["total_tokens"]:
            return update(status="HELD",run_id=rid,reason="NO_CONFIRMED_ORIGINAL_RESEARCH_TURN")
        update(status="AWAITING_JUDGE",run_id=rid,scientific_status=summary["status"],accounting=SETTLED,
               scientific_tokens=usage["total_tokens"])
        if verb == "reconcile":
            opinion = judge.reconcile(work,number,reg,binary=binary,reg=reg)
        else:
            ensure_ready(binary,reg)
            judge.prepare(work,number,reg)
            opinion = judge.run(work,number,reg,binary=binary,reg=reg,approval=approval)
        save(slot/"judge-accounting.json",opinion)
        if not settled(opinion) or opinion.get("judgment_status") not in ("REVIEWED","NO_INTERPRETABLE_DECISION"):
            return update(status="AWAITING_JUDGE",judge_accounting=opinion["status"],
                          judgment=opinion.get("judgment_status"))
        return update(status=SETTLED,judge_accounting=SETTLED,judge_tokens=opinion["total_tokens"],
                      judgment=opinion["judgment_status"],total_tokens=usage["total_tokens"]+opinion["total_tokens"],
                      provider_usd=usage.get("provider_usd") if opinion.get("provider_usd")==0 else None)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation",choices=("preflight","register","run","resume","stop","reconcile","status"))
    p.add_argument("--work",type=Path)
    p.add_argument("--binary",type=Path,default=rehearse.BINARY)
    p.add_argument("--approval",type=Path)
    p.add_argument("--approval-sha")
    p.add_argument("--slot",choices=SLOTS)
    a = p.parse_args()
    work = a.work.resolve() if a.work else None
    try:
        if a.operation == "preflight":
            output = ensure_ready(a.binary,protocol())
        else:
            if work is None:
                p.error("--work is required")
            if a.operation == "register":
                reg = enrollment(work,a.binary,a.approval,a.approval_sha)
                output = {k:reg[k] for k in ("series","slots","source_commit","scope","approval_sha256")}
            elif a.operation == "status":
                output = read(work/"result.json")
            else:
                if not a.slot:
                    p.error("--slot is required")
                if a.operation == "stop":
                    if not (work/a.slot/"first-attempt.json").exists():
                        raise ValueError("This slot has not started")
                    save(work/a.slot/"stop-request.json", {"slot":a.slot,"reason":"operator requested stop","time":time.time()})
                    output = {"status":"STOP_REQUESTED","slot":a.slot}
                else:
                    output = process(work,a.slot,a.operation,a.binary,a.approval,a.approval_sha)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status":"BLOCKED","reason":str(exc)},ensure_ascii=False))
        return 2
    print(json.dumps(output,indent=2,ensure_ascii=False))
    return 0 if output.get("status") not in {"HELD","BLOCKED","AWAITING_JUDGE"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
