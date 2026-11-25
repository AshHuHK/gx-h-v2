"""Export redacted, replay-verified public slot receipts. Reads local originals; never starts a host."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys
from tempfile import TemporaryDirectory
from uuid import UUID

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.v62b.study import receipts
from acceptance.v622.public.enrollment import digest, read, save
from acceptance.v623.live import action_witness as witness, adjudication
from acceptance.v623.oauth import judge, pilot, usage
from gx_core.errors import GxError
from gx_core.p0 import v623_endpoints

ABSENT = "MISSING"
ERRORS = (OSError, ValueError, AssertionError, KeyError, TypeError, AttributeError,
          IndexError, StopIteration, RuntimeError, sqlite3.Error, GxError)
PRIVATE = re.compile(r"(?i)(?:[\w.%+-]+@[\w.-]+\.[a-z]{2,}|(?<![\w.])(?:~?/|[a-z]:[\\/])"
                     r"|(?:https?|file)://|authorization|bearer|cookie|password|credential|api[_ -]?key|oauth"
                     r"|account[_ -]?(?:ref|id)|(?:access|refresh)[_ -]?token|client[_ -]?secret|passphrase|eyJ[\w.-]{8,}|gh[pousr]_|github_pat_|sk-[\w-]{8,}"
                     r"|\b[a-z0-9_-]{45,}\b)")


def excerpt(value):
    if not isinstance(value, str) or not value.strip():
        return None
    if PRIVATE.search(value) or any(ord(c)<32 and c not in "\n\r\t" for c in value):
        return "[private text withheld]"
    value = " ".join(value.split())
    return value if len(value)<=360 else value[:359]+"…"


def archived(path):
    return read(path) if path.is_file() else None


def file_proof(path, value):
    if archived(path) != value:
        raise ValueError("Stored receipt differs from its native replay")


def route(path, reg):
    evidence = archived(path)
    admission = (evidence or {}).get("admission",evidence) or {}
    return bool(admission.get("auth_type")=="chatgpt" and admission.get("provider")=="openai"
                and admission.get("origin")=="https://chatgpt.com/backend-api/codex"
                and admission.get("binary_sha256")==reg["official_binary_sha256"]
                and admission.get("model")==reg["model"] and admission.get("effort")==reg["model_effort"]
                and admission.get("status")=="READY" and admission.get("account_ref")
                and admission["account_ref"]==reg["account_ref"])


def original_id(value):
    try:
        return str(UUID(value))==value
    except (TypeError,ValueError,AttributeError):
        return False


def roles(slot, rid, reg):
    with closing(receipts.database(slot)) as db:
        roots = {r["thread_id"]:dict(r) for r in db.execute("""SELECT r.thread_id,r.model,r.effort,r.provider,d.scope
            FROM p0_roots r JOIN v6_decision_tasks d USING(run_id,task_id)
            WHERE r.run_id=? AND r.thread_id IS NOT NULL""", (rid,))}
        descendants = {r[0] for r in db.execute("SELECT child_id FROM p0_native_child_meters WHERE run_id=?", (rid,))}
        goal = db.execute("SELECT status FROM v6_goals WHERE run_id=?", (rid,)).fetchone()
        children = db.execute("SELECT count(*) FROM v62_modules WHERE run_id=?", (rid,)).fetchone()[0]
    histories,_ = usage._rollouts(slot/"codex",roots.keys()|descendants)
    started = {}
    for thread, source in histories.items():
        for turn,value in source["turns"].items():
            if not original_id(thread) or not original_id(turn):
                continue
            if source.get("provider")!="openai" or (value.get("model"),value.get("effort"))!=(reg["model"],reg["model_effort"]):
                continue
            actor=roots.get(thread)
            if actor and (actor["model"],actor["effort"],actor["provider"])!=(reg["model"],reg["model_effort"],"openai"):
                continue
            role = ("lead" if actor["scope"]=="run" else "module" if actor["scope"].startswith("module:") else "branch") if actor else "native_child"
            started[thread, turn] = role
    return started, children, goal[0] if goal else None


def lifecycle(db, rid, started):
    """Accepted GX actions on confirmed Lead turns, including decisions outside the scored comparison."""
    events = []
    op_names = {"open_branch", "add_node", "stop_branch", "cancel_node", "close_run"}
    for r in db.execute("""SELECT thread_id,turn_id,request_json,receipt_json FROM v6_decisions
                           WHERE run_id=? ORDER BY rowid""", (rid,)):
        if started.get((r["thread_id"], r["turn_id"])) != "lead":
            continue
        decision = json.loads(r["receipt_json"])
        if decision.get("scope") != "run" or decision.get("task_status") != "ACCEPTED":
            continue
        ops = [i["spec"]["op"] for i in decision["resolved_ops"] if i["spec"]["op"] in op_names]
        request = json.loads(r["request_json"])["decision"]
        reason = request.get("rationale")
        events.append({"event": decision["event_id"], "operations": ops,
                       "rationale_excerpt": excerpt(witness.visible_text(reason)) if reason else None})
    jobs = db.execute("SELECT status,count(*) FROM jobs WHERE run_id=? GROUP BY status", (rid,))
    reports = db.execute("SELECT count(*) FROM v62_module_reports WHERE run_id=?", (rid,)).fetchone()[0]
    return {"accepted_lead_decisions":len(events), "first":events[0] if events else None,
            "last":events[-1] if len(events)>1 else None, "published_module_reports":reports,
            "job_counts":{k:v for k,v in jobs if k in {"REQUESTED","QUEUED","STARTING","RUNNING","SUCCEEDED",
                                                       "FAILED","CANCELLED","SUBMITTED","INTERRUPTED"}}}


def scientific_accounting(slot, rid, reg):
    started, children, goal_status = roles(slot,rid,reg)
    actual = usage.reconcile(slot,rid)
    before = archived(slot/"accounting.json")
    match = ABSENT if before is None else "MATCH" if before==actual else "MISMATCH"
    totals=Counter()
    counts=Counter(started.values())
    turns=[]
    for r in actual["turns"]:
        role = started.get((r["thread"],r["turn"]))
        if not role or r["tokens"] is None:
            continue
        nested=r.get("descendants",[])
        if any((c["thread"],c["turn"]) not in started for c in nested):
            continue
        parent=r["tokens"]-sum(c["tokens"] for c in nested)
        totals[role] += parent
        turns.append({"role":role,"tokens":parent,"official_receipt_sha256":r["receipt_sha256"]})
        for child in nested:
            totals["native_child"]+=child["tokens"]
            turns.append({"role":"native_child","tokens":child["tokens"],"official_receipt_sha256":child["receipt_sha256"]})
    valid = actual["usage_status"]=="SETTLED" and actual["total_tokens"]==sum(totals.values())
    with closing(receipts.database(slot)) as db:
        activity=lifecycle(db,rid,started)
    return {"gx_activity":activity,"archive_check":match,"native_status":actual["usage_status"],"turns":turns,"started_turns":dict(counts),
            "validated_tokens_by_role":dict(totals),"settled_total_tokens":actual["total_tokens"] if valid else None,
            "native_reconciliation_sha256":actual["receipt_sha256"],"commissioned_modules":children,"goal_status":goal_status}


def assay_metric(m, spec):
    method = m["method"]
    return (method["id"]+"@"+str(method["version"]) == spec["capability"] and
            (m["output_name"], m["metric_id"], m["unit"]) == (spec["output_name"], spec["metric_id"], spec["unit"]) and
            m["selectors"] == {key:spec[key] for key in ("numerator","denominator")} and
            all(m["scope"].get(k)==v for k,v in spec["parameters"].items()))


def quantified(slot,rid,item,reg):
    """Only a settled authorized Job with matching declared endpoint, source and original bytes."""
    allowed={reg["materials"]["parent_sha256"],reg["materials"][item["code"]]["sha256"]}
    spec=reg["assay"]
    if not (slot/"state/artifacts").is_dir():
        return None
    values={}
    with closing(receipts.database(slot)) as db:
        outputs=db.execute("""SELECT DISTINCT a.artifact_ref FROM v6_nodes n JOIN artifacts a USING(run_id,job_id)
            WHERE n.run_id=? AND n.status IN ('SUCCEEDED','REUSED') AND a.origin='JOB_OUTPUT' AND a.published=1""",(rid,))
        for (ref,) in outputs:
            for m in v623_endpoints.registered(db,rid,ref):
                src=m.get("scope",{}).get("input_sha256")
                if src not in allowed or not assay_metric(m,spec):
                    continue
                values[m["metric_ref"]]={"metric":m["metric_id"],"numerator":m["numerator"],"denominator":m["denominator"],
                    "unit":m["unit"],"mask":m["scope"]["mask"],"output_sha256":m["sha256"],"specimen_sha256":src}
    return list(values.values())


def science(work, n, reg):
    trace = witness.trace_slot(work,n,reg,True)
    packet, first = witness.reader_packet(trace)
    outcome, facts = witness.outcome_packet(trace,reg,first)
    readings = {}
    for window in trace.get("lead_windows",[]):
        for candidate in window["candidates"]:
            m = candidate["metric"]
            proof = window["delivery_receipts"].get(m["metric_ref"])
            if not proof or not assay_metric(m,reg["assay"]):
                continue
            readings.setdefault(m["metric_ref"], {"metric":m["metric_id"], "numerator":m["numerator"],
                "denominator":m["denominator"], "unit":m["unit"], "mask":m["scope"]["mask"],
                "output_sha256":m["sha256"], "specimen_sha256":m["scope"]["input_sha256"],
                "delivery_event":proof["event_id"], "delivery":proof["delivery_kind"]})
    view={"status":"REPLAYED","quantified_readings":list(readings.values()),"opportunity_state":first["state"],
          "opportunity_endpoint_count":len(first["metric_refs"]), "comparison_registered":bool(trace.get("binding")),
          "planned_excerpt":excerpt(packet["lines"]["P"]),"reasoning_excerpt":excerpt(outcome["lines"]["R"]),
          "intent":facts["intent"],"enacted":facts["enacted"],"stop_reason":facts["stop_reason"],
          "endpoint_cited":facts["endpoint_cited"],"independent_match":facts["independent_match"],
          "delegation":facts["delegation"],"decision_event":facts["decision_event"]}
    return view,(packet,first,outcome,facts)


def independent(work,n,reg,packets):
    path=work/n/"judging"
    if not path.exists():
        return {"status":"NOT_STARTED","validated_tokens":None,"score":None}
    if not route(path/"host/oauth-account.json",reg):
        return {"status":"NO_VERIFIED_JUDGE_ROUTE","validated_tokens":None,"score":None}
    saved_thread=archived(path/"thread.json") or {}
    tid=saved_thread.get("thread_id")
    if tid:
        with closing(receipts.database(work/n)) as db:
            if (not original_id(tid) or db.execute("SELECT 1 FROM p0_roots WHERE thread_id=?", (tid,)).fetchone()
                    or (saved_thread.get("model"),saved_thread.get("effort"))!=(reg["model"],reg["model_effort"])):
                raise ValueError("Independent registered judge scope differs")
    packet, first, outcome, facts=packets
    for stage,blind,machine in (("opportunity",packet,first),("reason",outcome,facts)):
        name="opportunity" if stage=="opportunity" else "outcome"
        if (path/(name+"-packet.json")).exists():
            file_proof(path/(name+"-packet.json"),{**blind,"blinding":read(adjudication.RUBRIC)["blinding"],
                                                    "rubric_sha256":adjudication.file_hash(adjudication.RUBRIC)})
            file_proof(path/("private-"+name+".json"),machine)
        if intent:=archived(path/(stage+"-intent.json")):
            archive_packet=read(path/(name+"-packet.json"))
            if intent["prompt"]!=judge._prompt(stage,archive_packet) or intent["packet_sha256"]!=adjudication.digest(archive_packet):
                raise ValueError("Judge prompt differs from its original stage packet")
    verified={}
    locked=False
    def verify(path,value):
        nonlocal locked
        file_proof(path,value)
        if path.name=="opportunity-lock.json":
            locked=True
        if path.name in ("opportunity-native.json","reason-native.json"):
            verified[value["stage"]]=value
    fresh=judge.reconcile(work,n,reg,reg=reg,write_receipt=verify)
    account=archived(work/n/"judge-accounting.json")
    match=ABSENT if account is None else "MATCH" if account==fresh else "MISMATCH"
    stages=[]; started=[]
    for stage in judge.STAGES:
        ack=archived(path/(stage+"-accepted.json")) or {}
        if tid and ack.get("thread_id")==tid and original_id(ack.get("turn_id")):
            started.append(stage)
        f=verified.get(stage)
        if f:
            if f.get("native_prefix_sha256") is None or f["credit_exposure"]:
                raise ValueError("Unconfirmed original judge turn")
            stages.append({k:f[k] for k in ("stage","total_tokens","native_prefix_sha256","prompt_sha256","response_sha256")})
    stored=archived(path/"score.json")
    score=None
    if len(stages)==2 and stored and match=="MATCH" and stored==fresh and fresh["status"]=="SETTLED":
        score={"status":stored["judgment_status"],"interpretable":stored["interpretable"],"science_pass":stored["science_pass"],
               "marks":{k:v["mark"] for k,v in stored["criteria"].items()},"score_sha256":digest(path/"score.json")}
    return {"status":fresh["judgment_status"],"archive_check":match,"started_stages":started,"stages":stages,"opportunity_locked":locked,
            "validated_tokens":sum(x["total_tokens"] for x in stages) if stages else None,"score":score}


def project(work, reg, cases, assignment):
    n=assignment["slot"]; slot=work/n
    case=cases[n]; summary=archived(slot/"summary.json") or {}; first=archived(slot/"first-attempt.json") or {}
    ids={d["run_id"] for d in (case,summary,first) if d.get("run_id")}
    if len(ids)>1 or summary and (summary["slot"]!=n or summary["series"]!=reg["series"] or
            summary["architecture"]!=assignment["architecture"]):
        raise ValueError("Conflicting original slot identity")
    rid=next(iter(ids),None)
    if rid and not re.fullmatch(r"[a-z]+_[A-Za-z0-9]{18}",rid):
        raise ValueError("Invalid GX Run identifier")
    state=case["status"]
    if state not in {"REGISTERED","ACTIVE","AWAITING_JUDGE","SETTLED","HELD","INTERRUPTED","WAITING","PAUSED","TIMEOUT","TERMINATED","CLOSED","RATE_LIMITED"}:
        state="UNKNOWN"
    row={**assignment,"run_id":rid,"controller_status":state,"research_started":False if not first and not rid else None,
         "research_terminal":None,"quantified_endpoints":None,"scientific_accounting":None,"research":None,"judge":None,"provider_usd":None}
    if not rid or not route(slot/"oauth-account.json",reg):
        row["evidence_status"]="NOT_STARTED" if not first and not rid else "NO_VERIFIED_NATIVE_ROUTE"
        return row
    try:
        stmt=scientific_accounting(slot,rid,reg)
        row.update(scientific_accounting=stmt,research_started=True if stmt["started_turns"] else None)
        if row["research_started"] and summary.get("status") in pilot.ENDS and (
                summary["status"]!="CLOSED" or stmt["goal_status"]=="CLOSED"):
            row["research_terminal"]=summary["status"]
    except ERRORS:
        row["evidence_status"]="NATIVE_ACCOUNTING_UNAVAILABLE"
    if row["research_started"]:
        try:
            row["quantified_endpoints"]=quantified(slot,rid,assignment,reg)
        except ERRORS:
            row["quantified_endpoints"]=None
    if row["research_started"] and summary:
        try:
            if not (slot/"state/artifacts").is_dir():
                raise ValueError("Original Run artifacts unavailable")
            row["research"], packets=science(work,n,reg)
        except ERRORS:
            row["research"]={"status":"ORIGINAL_WITNESS_UNAVAILABLE"}
        else:
            try:
                row["judge"]=independent(work,n,reg,packets)
            except ERRORS:
                row["judge"]={"status":"ARCHIVE_UNVERIFIED","validated_tokens":None,"score":None}
    row.setdefault("evidence_status","AVAILABLE" if row["research_started"] else "START_UNCONFIRMED")
    return row


def export(work, output):
    work,output=Path(work).resolve(strict=True),Path(output).resolve()
    if work.is_relative_to(ROOT) or output.is_relative_to(work) or work.is_relative_to(output) or output.exists():
        raise ValueError("Export a registered external work directory into a new separate destination")
    reg=read(work/"registration.json"); result=read(work/"result.json"); protocol=pilot.protocol()
    if (reg.get("series")!=protocol["series"] or reg.get("host")!="direct_chatgpt_oauth" or
        reg.get("scope")!="PUBLIC_OAUTH_SUBSCRIPTION" or any(reg.get(k)!=v for k,v in protocol.items()) or
        reg["protocol_sha256"]!=pilot.registration.sha(protocol) or set(result["cases"])!=set(pilot.SLOTS)
        or result["series"]!=reg["series"] or result["registration_sha256"]!=digest(work/"registration.json")
        or not re.fullmatch("[a-f0-9]{40}",reg["source_commit"])
        or not re.fullmatch("[a-f0-9]{64}",reg["official_binary_sha256"])):
        raise ValueError("The archived cohort is not the original four-slot OAuth registration")
    output.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as staging:
        temp=Path(staging)/"published";temp.mkdir()
        rows=[project(work,reg,result["cases"],a) for a in protocol["slots"]]
        for r in rows:save(temp/(r["slot"]+".json"),r)
        scores=[r["judge"]["score"] for r in rows if r.get("judge") and r["judge"].get("score")]
        science_total=[(r["scientific_accounting"] or {}).get("settled_total_tokens") for r in rows]
        judge_total=[(r["judge"] or {}).get("validated_tokens") for r in rows]
        clean=all((r["scientific_accounting"] or {}).get("archive_check")=="MATCH"
                  and (r["judge"] or {}).get("archive_check")=="MATCH" and (r["judge"] or {}).get("score")
                  for r in rows)
        index={"schema":"gx.oauth.public-export/1","series":reg["series"],"source_commit":reg["source_commit"],
               "official_binary_sha256":reg["official_binary_sha256"],"registration_sha256":result["registration_sha256"],
               "original_assignments":len(rows),"research_started":sum(r["research_started"] is True for r in rows),
               "start_unconfirmed":sum(r["research_started"] is None for r in rows),
               "research_terminal":dict(Counter(r["research_terminal"] for r in rows if r["research_terminal"])),
               "quantified_slots":sum(bool(r["quantified_endpoints"]) for r in rows),
               "slots_with_proven_delivered_cues":sum(bool((r["research"] or {}).get("quantified_readings")) for r in rows),
               "independently_locked_opportunities":sum((r["research"] or {}).get("opportunity_state")=="DELIVERED"
                   and (r["judge"] or {}).get("opportunity_locked") is True for r in rows),
               "scored_opportunities":sum((r["research"] or {}).get("opportunity_state")=="DELIVERED"
                   and (((r["judge"] or {}).get("score") or {}).get("status")=="REVIEWED")
                   and isinstance(((r["judge"] or {}).get("score") or {}).get("science_pass"),bool) for r in rows),
               "interpretable_slots":sum(s["interpretable"] is True for s in scores),
               "scientific_passes":sum(s["science_pass"] is True for s in scores),
               "total_verified_science_and_judge_tokens":sum(science_total+judge_total)
                   if clean and all(isinstance(x,int) for x in science_total+judge_total) else None,
               "provider_usd":None,"cost_status":"UNKNOWN",
               "slots":[{"slot":r["slot"],"file":r["slot"]+".json","sha256":digest(temp/(r["slot"]+".json"))} for r in rows]}
        if result!=read(work/"result.json"):
            raise ValueError("The batch advanced during export; repeat against the updated originals")
        save(temp/"index.json",index);temp.rename(output)
    return index


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work",required=True,type=Path);parser.add_argument("--output",required=True,type=Path)
    args=parser.parse_args(); print(json.dumps(export(args.work,args.output),indent=2))
