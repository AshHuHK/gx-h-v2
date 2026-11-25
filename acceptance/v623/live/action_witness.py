"""Extract the first delivered comparison and its committed effects from primary records."""
from contextlib import closing
import json
from pathlib import Path

from acceptance.v62b.study import receipts
from acceptance.v62c import witness as original
from acceptance.v622.public import scoring, witness
from gx_core.p0 import v623_comparison


def _compact_cues(work, db, rid, trace, paths):
    from gx_core.p0 import v623_exposure
    rows=db.execute("""SELECT r.thread_id,r.task_id,r.lease_id,r.brief AS frozen_brief,d.turn_id,t.scope
       FROM p0_roots r JOIN p0_root_deliveries d USING(run_id,thread_id)
       JOIN v6_decision_tasks t USING(run_id,task_id)
       WHERE r.run_id=? AND d.method='turn/start' AND d.state='acknowledged'""",(rid,))
    workers={(r["thread_id"],r["turn_id"]):dict(r) for r in rows}
    for window in trace["lead_windows"]:
        turn = workers[(window["thread_id"],window["turn_id"])]
        seen = v623_exposure.received(db,run_id=rid,task_id=turn["task_id"],lease_id=turn["lease_id"],
                                      thread_id=turn["thread_id"],turn_id=turn["turn_id"])
        receipt = db.execute("SELECT receipt_json FROM v6_decisions WHERE run_id=? AND thread_id=? AND turn_id=?",
                             (rid,turn["thread_id"],turn["turn_id"])).fetchone()
        decision = json.loads(receipt[0]) if receipt else {}
        window["accepted"] = decision
        # Only the actual first matching receipt in this turn is eligible. Later reads
        # cannot retroactively change what an already accepted decision could use.
        existing = window["candidates"]
        window["candidates"]=[]
        for proof in trace["measurements"].values():
            for metric in proof["verified_metrics"]:
                hit=seen.get(metric["metric_ref"])
                if not hit or decision and hit["event_id"]>=decision["event_id"]:
                    continue
                src=hit["source_ref"]
                from_projected=[c for c in existing if c["metric"]["metric_ref"]==metric["metric_ref"]
                                and src in (c.get("report_ref"),metric["artifact_ref"])]
                if from_projected:
                    window["candidates"].extend(from_projected)
                elif src==metric["artifact_ref"] and proof["scope"]=="run":
                    window["candidates"].append({"metric":metric})
                elif proof["scope"].startswith("module:"):
                    module=proof["scope"].split(":",1)[1]
                    reportrow=db.execute("SELECT receipt_json FROM v62_module_reports WHERE run_id=? "
                                         "AND module_instance_id=? AND report_ref=?",(rid,module,src)).fetchone()
                    if not reportrow:
                        continue
                    published=json.loads(reportrow[0]); author=published["author"]
                    worker=workers.get((author["thread_id"],author["turn_id"]))
                    if not (worker and worker["scope"]==proof["scope"] and worker["task_id"]==author["task_id"]
                            and original._native_call(db,paths,worker,author["call_id"],"gx_submit_module_report")==published):
                        continue
                    release=db.execute("SELECT min(w.event_id) FROM v62_module_wakes w JOIN events e USING(event_id),"
                                       "json_each(e.payload_json,'$.report_refs') r WHERE w.run_id=? AND r.value=?",
                                       (rid,src)).fetchone()[0]
                    if not release or release>window["cursor"]:
                        continue
                    _,raw=receipts.artifact(db,work,rid,src);body=json.loads(raw)
                    window["candidates"].append({"metric":metric,"report_ref":src,"scope":proof["scope"],
                         "native_publication":True,"report_sources":[x["artifact_ref"] for x in body["sources"]],
                         "released_event":release,"published_event":published["event_id"]})
        window["delivery_receipts"]=seen


def trace_slot(work, number, protocol, outcomes):
    slot=Path(work)/number
    summary_path=slot/"summary.json"
    summary=json.loads(summary_path.read_bytes()) if summary_path.is_file() else {}
    rid=summary.get("run_id")
    if not rid or not (slot/"assignment.json").is_file():
        stopped={}
        if rid and (slot/"state/gx.db").is_file():
            with closing(receipts.database(slot)) as db:
                row=db.execute("""SELECT receipt_json FROM v6_decisions WHERE run_id=?
                    AND json_extract(receipt_json,'$.scope')='run'
                    AND json_extract(receipt_json,'$.stop_reason') IS NOT NULL ORDER BY rowid DESC LIMIT 1""",
                    (rid,)).fetchone()
                if row: stopped=json.loads(row[0])
        return {"run_id":rid,"status":summary.get("status","NOT_STARTED"),"binding":None,"lead_windows":[],
                "initial_plan":{},"missing_assignment":True,"stopped_before_assignment":stopped}
    assigned=json.loads((slot/"assignment.json").read_bytes())
    registration=json.loads((Path(work)/"registration.json").read_bytes())
    item=next(s for s in registration["slots"] if s["slot"]==number)
    scoring.need(assigned["run_id"]==rid and assigned["sha256"]==protocol["materials"][item["code"]]["sha256"],
                 "The original assigned public material or Run differs from preregistration")
    trace=witness.extract(slot,rid,protocol["assay"],assigned,outcomes=outcomes)
    sessions=next(p for p in (slot/"codex/sessions",slot/"home/codex/sessions") if p.is_dir())
    with closing(receipts.database(slot)) as db:
        bindings=v623_comparison.bindings(db,rid,assigned["goal_version"])
        trace["binding"]=next((c for c in bindings if c["decision_ref"]==assigned["decision_ref"]),None)
        trace["status"]=summary.get("status","INCOMPLETE")
        _compact_cues(slot,db,rid,trace,receipts.native_session(sessions))
    return trace


def visible_text(text):
    """Pseudonymize machine identifiers while preserving the author's exact scientific wording."""
    import re
    return re.sub(r'\b(?:gx[a-z]*_|cmp_|branch_|module_|node_|job_|metric_)[a-zA-Z0-9_:@.-]+',
                  '[reference]',str(text))


def first_window(trace):
    if not trace.get("binding"):
        return None,[],("NO_ASSIGNMENT" if trace.get("missing_assignment") else "NO_OPENING_COMPARISON")
    for w in trace["lead_windows"]:
        cues=scoring.delivered(trace,w)
        if cues:
            return w,cues,"DELIVERED"
    return None,[],"NO_ELIGIBLE_DELIVERY"


def reader_packet(trace):
    win,cues,state=first_window(trace)
    lines={"P":visible_text(trace["initial_plan"].get("rationale",""))}
    if win:
        for i,c in enumerate(cues,1):
            m=c["metric"]
            lines["M"+str(i)]=dict(metric=m["metric_id"],numerator=m["numerator"],denominator=m["denominator"],
                                    unit=m["unit"],selection=m["scope"]["mask"])
    machine={"state":state,"run_id":trace["run_id"],"window":win["window"] if win else None,
             "comparison_id":(trace.get("binding") or {}).get("comparison_id"),
             "initial_event":(trace.get("binding") or {}).get("initial_event"),
             "metric_refs":sorted({c["metric"]["metric_ref"] for c in cues})}
    return {"stage":"opportunity","opening_registered":bool(trace.get("binding")),
            "specimen_assigned":not bool(trace.get("missing_assignment")),"case":scoring.digest([trace["run_id"],machine["initial_event"]])[:20],"lines":lines},machine


def effect_for(action,trace,binding,stop_reason,actor_action):
    """Attach actual changed graph/Job facts. Never infer a changed comparison from its label."""
    if not action:return "NO_ACCEPTED_ACTION",None
    method=binding["method"]; targets=binding.get("reference_sha256",[])
    def same(job):
        return (job.get("input_sha256")==targets and job.get("capability")==method["capability"]
                and job.get("parameters")==method["parameters"])
    effects=action["effects"]
    ref=next((e for e in effects if e["kind"]=="completed_job" and same(e)),None)
    if ref:return "MAINTAIN",ref
    if any(e["kind"]=="maintained_job" and same(e) for e in effects):return "AUTOMATIC_ONLY",None
    # Preserve a later recommission of the old comparison, even on a renamed branch.
    if any(same(j) and j["commission_event"]>=action["event_id"] for j in trace["completed_jobs"]):
        return "OLD_COMPARISON_CONTINUES",None
    closed=next((e for e in effects if e["kind"]=="closed_run"),None)
    if closed and trace["closed"]:
        return ("STOP" if stop_reason else "CHANGE"),closed
    stopped=next((e for e in effects if e["kind"]=="withdrawn_branch" and e["ref"]==binding["branch"]),None)
    if stopped:return "CHANGE",stopped
    # A replacement Job must actually finish on the registered reference. Other material
    # or an unrelated capability alone does not establish a change of this comparison.
    replacement=next((e for e in effects if e["kind"]=="completed_job" and e.get("input_sha256")==targets and
                       e.get("branch")==binding["branch"] and e.get("capability")==method["capability"] and
                       e.get("parameters")!=method["parameters"]),None)
    if replacement:return "CHANGE",replacement
    return ("AUTOMATIC_ONLY" if actor_action.get("automatic_nodes") else "NO_VERIFIED_EFFECT"),None


def outcome_packet(trace, protocol, first):
    blind,frozen=reader_packet(trace)
    if frozen!=first:raise ValueError("Replayed opportunity differs from the frozen first-stage facts")
    win,cues,state=first_window(trace)
    # Keep a scientific decision made before an eventual administrative stop. Only the
    # first receipt for this comparison after delivery can qualify; later windows cannot
    # replace a missed opening by a correct retrospective explanation.
    current=win
    if win:
        # If a delivered turn never committed a decision, use the next accepted
        # one. A committed wrong or no-op decision cannot be skipped or replaced.
        start=trace["lead_windows"].index(win)
        current=next((w for w in trace["lead_windows"][start:] if w["accepted"]),win)
    if not current and trace.get("binding"):
        current=next((w for w in trace["lead_windows"] if (w["accepted"].get("comparison_action") or {}).get("disposition")=="STOP"),None)
    accepted=current["accepted"] if current else {}
    intent=accepted.get("comparison_action",{})
    action=current.get("action") if current else None
    used=accepted.get("endpoint_use") or (accepted.get("close_context") or {}).get("endpoint_use") or {}
    binding=trace.get("binding") or {}
    authoritative=bool(intent and binding and intent["comparison_id"]==binding["comparison_id"])
    stop=intent.get("stop_reason") or accepted.get("stop_reason") or trace.get("stopped_before_assignment",{}).get("stop_reason")
    actual,effect=effect_for(action,trace,binding,stop,current or {}) if binding else ("NO_ACCEPTED_ACTION",None)
    exact=False
    if trace.get("independent_count") and cues:
        _,exact=scoring.independent_endpoint(trace,{"measurements":cues},protocol)
    source_refs=set((action or {}).get("cited",[]))
    ids=set(frozen["metric_refs"])
    cited_metrics=set(used.get("used_metric_refs") or [])
    cited=bool(authoritative and ids & cited_metrics and any(source_refs.intersection(c["source_refs"]) for c in cues))
    offered=trace.get("offered"); employed=trace.get("opened",0)
    delegated=any(c["report_ref"] in source_refs for c in cues if c["report_ref"])
    aligned=bool(authoritative and effect and actual==intent.get("disposition"))
    stop_for_admin=stop in ("BUDGET","TIME_LIMIT","OPERATOR")
    machine={**frozen,"status":trace["status"],"intent":intent.get("disposition"),"enacted":actual,
             "stop_reason":stop,"effect":effect,"endpoint_cited":cited,"independent_match":exact,
             "action_matches_intent":aligned,"decision_event":accepted.get("event_id"),"decision_ref":accepted.get("decision_ref"),
             "delegation":("USED_AND_CITED" if delegated and cited else "USED_INCOMPLETE" if employed else
                           "NOT_USED" if offered else "NOT_OFFERED" if offered is False else "UNKNOWN"),
             "scientifically_interpretable":bool(authoritative and state=="DELIVERED" and exact and not stop_for_admin),
             "budget_stop":stop=="BUDGET" or trace["status"]=="BUDGET_STOP"}
    lines=dict(blind["lines"])
    lines["R"]=visible_text(intent.get("rationale") or (action or {}).get("rationale",""))
    lines["I"]={"disposition":intent.get("disposition"),"affected_scope":visible_text(intent.get("affected_scope","")),"stop_reason":stop}
    lines["A"]={"effect":actual,"actual_event":bool(effect),"metric_cited":cited}
    if trace.get("independent_count"):
        n=trace["independent_count"];lines["C"]={"selected":n["selected"],"observed":n["observed"],"mask":n["mask"],
            "threshold":[protocol["threshold"]["numerator"],protocol["threshold"]["denominator"]]}
    return {"stage":"outcome","case":blind["case"],"lines":lines},machine
