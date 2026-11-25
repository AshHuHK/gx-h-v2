"""Replay original diagnostic records and bind operator review to the same observation."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.science_execution import approval, diagnostic
from acceptance.v622.public.enrollment import digest, read
from acceptance.v623.live import action_witness
from acceptance.v623.oauth import export_results, usage
from acceptance.v623.oauth.judge import save
from acceptance.v62b.study import receipts
from gx_core.domain.ids import sha256_of_obj


def _event(db, rid, key):
    if key is None:
        return None
    row = db.execute("SELECT event_id,kind,ts FROM events WHERE run_id=? AND event_id=?", (rid,key)).fetchone()
    return dict(row) if row else None


def _utc(event):
    return datetime.fromisoformat(event["ts"].replace("Z","+00:00")).timestamp()


def submission_record(db, rid, delivery_id, decision_id):
    leads = db.execute("""SELECT r.thread_id,d.turn_id FROM p0_roots r
        JOIN p0_root_deliveries d USING(run_id,thread_id)
        JOIN v6_decision_tasks t USING(run_id,task_id)
        WHERE r.run_id=? AND t.scope='run' AND d.method='turn/start' AND d.state='acknowledged'""", (rid,))
    subjects = {sha256_of_obj(list(row)) for row in leads}
    rejected = []
    for row in db.execute("""SELECT event_id,ts,subject_id,payload_json FROM events
              WHERE run_id=? AND kind='v624.decision.rejected' AND actor='gx' ORDER BY event_id""", (rid,)):
        if row["subject_id"] in subjects:
            data = json.loads(row["payload_json"])
            rejected.append({"event_id":row["event_id"],"utc":row["ts"],"field":data["field"],"reason":data["reason"]})
    accepted = [json.loads(r[0])["event_id"] for r in db.execute("""SELECT receipt_json FROM v6_decisions
        WHERE run_id=? AND json_extract(receipt_json,'$.scope')='run'""",(rid,))]
    interval = [key for key in accepted if delivery_id is not None and key>delivery_id]
    interval += [r["event_id"] for r in rejected if delivery_id is not None and r["event_id"]>delivery_id]
    return {"accepted":len(accepted),"rejected":rejected,"first_after_delivery":min(interval,default=None),
            "first_post_delivery_accepted":bool(decision_id and interval and min(interval)==decision_id)}


def accounting(slot, reg, rid):
    archive = read(slot/"accounting.json") if (slot/"accounting.json").is_file() else {}
    if reg["host"] == diagnostic.OAUTH:
        verified = export_results.scientific_accounting(slot,rid,reg)
        native = usage.reconcile(slot,rid)
        settled = (export_results.route(slot/"oauth-account.json",reg)
                   and native["can_continue"] and verified["native_status"]=="SETTLED" and verified["settled_total_tokens"] is not None
                   and archive.get("receipt_sha256")==native["receipt_sha256"]
                   and archive.get("total_tokens")==verified["settled_total_tokens"])
        actors,_,_ = export_results.roles(slot,rid,reg)
        by_role = verified["validated_tokens_by_role"]
        extra = {"provider_usd":None,"confirmed_response_count":native.get("covered_response_count"),
                 "confirmed_lower_bound":native.get("covered_response_tokens"),"physical_requests":None}
    else:
        from acceptance.v623.live import hosts, pilot
        roots = hosts.role_witness(slot,rid,strict=False)
        raw = pilot.account(slot,roots,diagnostic.OfflineLedger(slot.parent,reg),False)
        rows = raw["requests"]
        actors = {(r["thread_id"],r["turn_id"]):("lead" if r["scope"]=="run" else "module" if r["scope"].startswith("module:") else "branch") for r in rows}
        by_role = Counter()
        for row in rows:
            if row["run_id"] == rid and row["state"]=="SETTLED":
                by_role[actors[row["thread_id"],row["turn_id"]]] += row["actual"]
        settled = (bool(rows) and not raw["unmatched_turns"]
                   and not raw["totals"]["unresolved_requests"] and not raw["totals"]["breaches"]
                   and len({r["request_id"] for r in rows})==len(rows)
                   and all(r["state"]=="SETTLED" and r["run_id"]==rid for r in rows)
                   and all(archive.get(k)==v for k,v in raw.items()) and archive.get("status")=="SETTLED"
                   and sum(by_role.values())==archive.get("total_tokens"))
        extra = {"provider_usd":None,"physical_requests":len(rows),"simulated_tokens":sum(by_role.values()),"actual_model_inference_calls":0}
    return {"settled":settled,"turns_by_role":dict(Counter(actors.values())),"tokens_by_role":dict(by_role),**extra},actors


def evaluate(work,number):
    work = Path(work).resolve()
    slot = work/number
    reg = read(work/"registration.json")
    header = {"series":reg["series"],"candidate":reg["source_commit"],"slot":number,
              "live":reg["host"]==diagnostic.OAUTH}
    result = read(work/"result.json")
    if result["registration_sha256"]!=digest(work/"registration.json"):
        raise ValueError("The diagnostic registration changed")
    if not (slot/"summary.json").is_file():
        return {**header,"status":result["cases"][number]["status"],"mechanical_pass":False}
    summary = read(slot/"summary.json")
    rid = summary["run_id"]
    first = read(slot/"first-attempt.json")
    if (rid!=first["run_id"] or rid!=result["cases"][number]["run_id"]
            or reg["series"]!=first["series"] or first["slot"]!=number):
        raise ValueError("Inconsistent original allocation")
    trace = action_witness.trace_slot(work,number,reg,True)
    reader,opening = action_witness.reader_packet(trace)
    outcome,facts = action_witness.outcome_packet(trace,reg,opening)
    win,cues,_ = action_witness.first_window(trace)
    delivery = min((win["delivery_receipts"][c["metric"]["metric_ref"]]["event_id"] for c in cues), default=None)
    account,actors = accounting(slot,reg,rid)
    action = facts["decision_event"]
    effect = facts["effect"] or {}
    with closing(receipts.database(slot)) as db:
        times = {"delivery":_event(db,rid,delivery),"decision":_event(db,rid,action),"effect":None}
        if effect.get("kind")=="completed_job":
            done = db.execute("""SELECT event_id FROM events WHERE run_id=? AND subject_id=?
               AND kind='job.transition' AND new_state='SUCCEEDED' ORDER BY event_id LIMIT 1""",(rid,effect["job_id"])).fetchone()
            if done:
                times["effect"] = _event(db,rid,done[0])
        elif effect and action:
            times["effect"] = times["decision"]
        submissions = submission_record(db,rid,delivery,action)
        linked = []
        for cue in cues:
            metric = cue["metric"]
            proof = trace["measurements"][metric["artifact_ref"]]
            module = db.execute("SELECT thread_id FROM p0_roots WHERE run_id=? AND task_id=?",(rid,proof["actor_task"])).fetchone()
            actor = (module[0],proof["actor_turn"]) if module else None
            published = next((c for c in win["candidates"] if cue["report_ref"] and c.get("report_ref")==cue["report_ref"]
                              and c["metric"]["metric_ref"]==metric["metric_ref"]),{})
            linked.append({"metric_ref":metric["metric_ref"],"numerator":metric["numerator"],"denominator":metric["denominator"],
                "role":actors.get(actor),"job":cue["job"],"assigned_event":trace["assignment"]["event_id"],
                "grant_event":proof["grant_event"],"commission_event":proof["commission_event"],"settled_event":proof["settled_event"],
                "published_event":published.get("published_event"),"released_event":published.get("released_event"),
                "report_ref":cue["report_ref"],"read_event":win["delivery_receipts"][metric["metric_ref"]]["event_id"],
                "different_thread":bool(actor and actor[0]!=win["thread_id"])})
    clock = first["research_window"]
    at = _utc(times["effect"]) if times["effect"] else None
    phase = "NO_EFFECT" if at is None else ("OBSERVATION" if clock["start"]<=at<clock["new_work_until"]
            else "DRAIN" if clock["new_work_until"]<=at<clock["drain_until"] else "OUTSIDE")
    authorized_lead = bool(win and actors.get((win["thread_id"],win["turn_id"]))=="lead")
    if number=="02":
        valid_arm = (facts["delegation"]=="USED_AND_CITED" and account["tokens_by_role"].get("module",0)>0
                     and any(m["role"]=="module" and m["different_thread"] and m["published_event"]
                             and m["released_event"] for m in linked))
    else:
        valid_arm = (facts["delegation"]=="NOT_OFFERED" and submissions["first_post_delivery_accepted"]
                     and any(m["role"]=="lead" for m in linked))
    passed = all([account["settled"],authorized_lead,valid_arm,facts["independent_match"],facts["endpoint_cited"],
                  facts["action_matches_intent"],facts["scientifically_interpretable"],phase=="OBSERVATION",
                  bool(str(outcome["lines"]["R"]).strip())])
    return {**header,"run_id":rid,"status":summary["status"],"mechanical_pass":passed,"arm_evidence":valid_arm,
            "accounting":account,"events":times,"submission":submissions,"observation":phase,
            "seconds_delivery_to_effect":round(at-_utc(times["delivery"]),3) if at and times["delivery"] else None,
            "measurements":linked,"predeclared_plan":reader["lines"]["P"],"decision":outcome["lines"],"facts":facts}


def review(work,number,operator,rating,basis):
    record = evaluate(work,number)
    if not record.get("live") or not record.get("run_id") or not operator.strip() or not basis.strip():
        raise ValueError("Review requires the original subscription case, a reviewer and a scientific basis")
    if rating not in ("PASS","HOLD") or rating=="PASS" and not record["mechanical_pass"]:
        raise ValueError("The original trace does not meet the registered mechanical criteria")
    path = Path(work)/number/"scientific-review.json"
    value = {"slot":number,"operator":operator,"rating":rating,"basis":basis,"witness_sha256":sha256_of_obj(record)}
    if path.exists():
        saved = read(path)
        if any(saved.get(k)!=v for k,v in value.items()):
            raise ValueError("Conflicting prior review")
        return saved
    value["utc"] = datetime.now(timezone.utc).isoformat()
    save(path,value)
    return value


def freeze_ready(work,*,candidate,account_ref,approval_path=None):
    work = Path(work).resolve()
    reg = read(work/"registration.json")
    from acceptance.v623.live import registration
    if (reg["host"]!=diagnostic.OAUTH or reg["series"]!=approval.DIAGNOSTIC or reg["source_commit"]!=candidate
            or reg["account_ref"]!=account_ref or not account_ref
            or reg["protocol_sha256"]!=registration.sha(diagnostic.protocol(diagnostic.OAUTH))):
        raise ValueError("The required subscription cohort, candidate or account is missing")
    # Registration already checked scoped PI approval. Require its private original at freeze as well.
    path = approval_path or approval.find_enrollment(reg["approval_sha256"])
    verified = approval.verify(path,work=work,candidate=candidate,series=approval.DIAGNOSTIC,account_ref=account_ref)
    if reg["approval_sha256"]!=verified["approval_sha256"]:
        raise ValueError("The original enrollment changed")
    proofs = {}
    for n in diagnostic.SLOTS:
        doc = evaluate(work,n)
        path = work/n/"scientific-review.json"
        signed = read(path) if path.exists() else {}
        if (not doc["mechanical_pass"] or signed.get("witness_sha256")!=sha256_of_obj(doc)
                or signed.get("rating")!="PASS" or not signed.get("operator") or not signed.get("basis")):
            raise ValueError("Missing demonstrated diagnostic chain or its scientific review in "+n)
        proofs[n] = {"run_id":doc["run_id"],"decision_event":doc["facts"]["decision_event"],"review_sha256":digest(path)}
    return {"status":"FROZEN","candidate":candidate,"series":approval.DIAGNOSTIC,"proofs":proofs}


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation",choices=["show","review","freeze"])
    parser.add_argument("--work",type=Path,required=True)
    parser.add_argument("--slot",choices=diagnostic.SLOTS)
    parser.add_argument("--approval",type=Path)
    for item in ("operator","rating","basis"):
        parser.add_argument("--"+item)
    args = parser.parse_args()
    if args.operation=="freeze":
        reg = read(args.work/"registration.json")
        result = freeze_ready(args.work,candidate=reg["source_commit"],account_ref=reg["account_ref"],approval_path=args.approval)
        save(args.work/"v62-freeze.json",result)
        print(json.dumps(result,indent=2))
        sys.exit(0)
    if not args.slot:
        parser.error("show and review require --slot")
    if args.operation=="review" and not all([args.operator,args.rating,args.basis]):
        parser.error("review requires --operator, --rating PASS|HOLD and --basis")
    doc = (review(args.work,args.slot,args.operator,args.rating,args.basis)
           if args.operation=="review" else evaluate(args.work,args.slot))
    print(json.dumps(doc,indent=2,ensure_ascii=False))
