"""Read back original Goal events, native wire receipts, Job outputs and independent recomputation."""
from contextlib import closing
import json
from acceptance.v622.public import funding
from acceptance.v622.public.enrollment import digest, read, save
from acceptance.v62b.study import receipts as native
from acceptance.v62c import rehearse
from acceptance.v623.oauth import host
from gx_core.p0 import manifest
from . import independent, protocol


def thread_items(path):
    """A completed rollout tool exchange establishes exactly which bytes reached this thread."""
    result, calls = {}, {}
    turn = None
    with path.open("rb") as stream:
        for line_no, line in enumerate(stream, 1):
            event = json.loads(line)
            p = event["payload"]
            if event["type"] == "turn_context":
                turn = p["turn_id"]
            if event["type"] != "response_item":
                continue
            key = (turn, p.get("call_id"))
            if p.get("type") == "function_call":
                calls[key] = {"tool":p["name"].split(".")[-1],"args":json.loads(p["arguments"]),"line":line_no}
            if p.get("type") == "function_call_output" and key in calls:
                try:
                    value = rehearse.responses.output_json(p["output"])
                except (TypeError, ValueError):
                    continue
                result[key] = {**calls[key],"value":value,"output_line":line_no,"utc":event["timestamp"]}
    return calls,result


def export(work, rid, reg, cold_hash, ledger):
    slot = work/"lead"
    op = read(work/"custody/opening.json")
    primary = op["commitment"]
    sessions = native.native_session(slot/"codex/sessions")
    history = {t:thread_items(path) for t,path in sessions.items()}
    decisions, readings, refused, jobs = [], [], [], []
    with closing(rehearse.db_open(slot)) as db:
        for d in protocol.accepted(db,rid):
            thread,turn,cid=d["thread_id"],d["turn_id"],d["call_id"]
            root = db.execute("SELECT brief FROM p0_roots WHERE run_id=? AND thread_id=?", (rid,thread)).fetchone()
            session,ctx,when,calls,_ = native.delivered(sessions[thread],turn,root[0])
            if (session["model_provider"] != ("gx_loopback" if reg["synthetic"] else "openai")
                    or (ctx["model"],ctx["effort"]) != (reg["model"],reg["effort"]) or cid not in calls):
                raise ValueError("Missing original Lead profile, decision or brief receipt")
            native_args = calls[cid][1].get("decision") or {}
            if (calls[cid][0]!="gx_decide_revision" or native_args.get("rationale")!=d["decision"]["rationale"]
                    or (d["decision_ref"]==primary["decision_ref"] and
                        native_args.get("comparison_plan",{}).get("prediction")!=primary["prediction"])):
                raise ValueError("GX accepted a different native call")
            usage = db.execute("SELECT terminal_status,total_tokens,measured_tokens,metering_reason FROM p0_root_turn_usage "
                               "WHERE thread_id=? AND turn_id=?", (thread,turn)).fetchone()
            decisions.append({k:v for k,v in d.items() if k not in ("decision","receipt")})
            decisions[-1].update(rationale=d["decision"]["rationale"],operations=d["decision"]["ops"],
                evidence_refs=d["decision"].get("evidence_refs", []),metric_refs=d["receipt"].get("metric_refs", []),
                brief_delivered_utc=when,native_line=calls[cid][2],native_sha256=digest(sessions[thread]),
                turn_usage=dict(usage) if usage else None)
        run_calls = db.execute("""SELECT n.*,c.result_json FROM p0_v6_native_calls n JOIN p0_root_calls c
            USING(thread_id,turn_id,call_id) JOIN p0_roots r USING(thread_id) WHERE r.run_id=? ORDER BY c.rowid""",(rid,))
        for r in run_calls:
            result,request = json.loads(r["result_json"]),json.loads(r["request_json"])
            if isinstance(result,dict) and ("decision_refusal" in result or "error" in result):
                refused.append({"thread_id":r["thread_id"],"turn_id":r["turn_id"],"call_id":r["call_id"],"result":result})
            if request["tool"]!="gx_read_artifact":
                continue
            item = history[r["thread_id"]][1].get((r["turn_id"],r["call_id"]))
            if not item or item["tool"]!="gx_read_artifact" or item["args"]!=request["arguments"] or item["value"]!=result:
                continue
            readings.append({"thread_id":r["thread_id"],"turn_id":r["turn_id"],"call_id":r["call_id"],
                             "ref":item["args"]["ref"],"utc":item["utc"],"output_line":item["output_line"],
                             "complete_segment":result.get("schema")=="gx.mapped-segment/2" if isinstance(result,dict) else False})
        for row in db.execute("""SELECT j.job_id,j.status,j.measured_json,n.decision_ref FROM jobs j
                JOIN v6_nodes n USING(job_id,run_id) WHERE j.run_id=? ORDER BY j.rowid""",(rid,)):
            j = manifest.read_manifest(db,row["job_id"])
            settings = j["admission"]["parameters"]
            inputs = [a["sha256"] for a in j["admission"]["inputs"]]
            job = {"job_id":row["job_id"],"status":row["status"],"commission":row["decision_ref"],"parameters":settings,
                   "measured":json.loads(row["measured_json"] or "{}"),"input_sha256":inputs,
                   "admission_sha256":j["admission_hash"],"manifest_sha256":j["manifest_hash"]}
            jobs.append(job)
            if row["status"]!="SUCCEEDED":
                continue
            out = next(x for x in j["completion"]["outputs"] if x["output_name"]=="segment.json")
            _, raw = native.artifact(db,slot,rid,out["artifact_ref"])
            table = json.loads(raw)
            if inputs!=[a["sha256"] for a in op["assignments"]]:
                raise ValueError("Unexpected Job original inputs")
            submitted = db.execute("SELECT event_id,ts FROM events WHERE run_id=? AND kind='job.created' AND subject_id=?",
                                   (rid,row["job_id"])).fetchone()
            published = db.execute("SELECT event_id,ts FROM events WHERE run_id=? AND kind='artifact.published' AND subject_id=?",
                                   (rid,out["artifact_ref"])).fetchone()
            if not (primary["event_id"] < min(a["event_id"] for a in op["assignments"]) <=
                    max(a["event_id"] for a in op["assignments"]) < submitted[0] < published[0]):
                raise ValueError("Primary, opening and Job chronology disagree")
            checked = independent.verify((work/"custody/coordinates.cif").read_bytes(),(work/"custody/mapping.json").read_bytes(),table)
            received = [r for r in readings if r["ref"]==out["artifact_ref"] and r["complete_segment"]]
            followups = [d for d in decisions if d["event_id"]>published[0] and out["artifact_ref"] in d["evidence_refs"]
                         and any(r["thread_id"]==d["thread_id"] and r["turn_id"]==d["turn_id"] and r["output_line"]<d["native_line"]
                                 for r in received)]
            job.update(table=table, output=out,admitted=dict(submitted),published=dict(published),
                       independent=checked, received=received,followup_refs=[d["decision_ref"] for d in followups])
    primaries = [j for j in jobs if j["parameters"]==primary["method"]["parameters"] and "table" in j]
    if len(primaries)!=1 or jobs[0]!=primaries[0] or not primaries[0]["followup_refs"]:
        raise ValueError("No complete, source-bound primary table was received and cited in an enacted action")
    final = next(d for d in decisions if d["decision_ref"]==primaries[0]["followup_refs"][0])
    for later in jobs[1:]:
        if later["commission"] not in primaries[0]["followup_refs"]:
            raise ValueError("Additional measurement lacks a new decision after receipt of the primary result")
    if ledger:
        costs = funding.check_slot_receipts(ledger,slot.name,[json.loads(l) for l in (slot/"raw/requests.jsonl").read_text().splitlines()])
        account = {"fixture_costs":costs,"real_model_requests":0,"provider_usd":None}
    else:
        account = host.reconcile(slot,rid)
    classification = "SCRIPTED_SYNTHETIC" if reg["synthetic"] else "PROSPECTIVE_ARCHIVE"
    outcome = {"schema":"gx.blind-study.result/1","series":reg["series"],"run_id":rid,"qualification":reg["qualification"],
               "classification":classification,"status":"CLOSED","registration_sha256":digest(work/"seal.json"),
               "research_window":read(slot/"first-attempt.json")["research_window"],
               "opening":op,"decisions":decisions,"reads":readings,"jobs":jobs,"qc_recommendation":final["rationale"],
               "readback":{"physical_jobs":len(jobs),"artifact_reads":len(readings),"read_induced_jobs":0},
               "cost":account,"field_retries":refused,"cold_state_sha256":cold_hash}
    save(work/"study-receipt.json",outcome)
    table = primaries[0]["table"]
    lines = ["# "+classification,"",final["rationale"],"",f"Scope: {reg['blind_id']}, {table['interval']}, {table['mask']}, "
             f"{table['endpoint']} ({table['unit']}). Shared chains are dependent observations.","",
             "| Pair | Before | After | Absolute change |","|---|---:|---:|---:|"]
    for key in ("pair12","pair13","pair23","median"):
        row = table["rows"][key]
        vals = row[table["endpoint"]]
        lines.append("| "+" | ".join([key+" ("+",".join(row["chains"])+")",*[str(vals[a]) if vals[a] is not None else "NA" for a in ("before","after","delta")]])+" |")
    lines += ["",f"Matched {table['selected_residues']}, omitted {table['omitted_residues']}, retained {table['retained_residues']}.",
              f"Independent maximum delta difference {primaries[0]['independent']['max_delta_difference']} {table['unit']}, "
              f"tolerance {primary['operating_limits']['tolerance']} {table['unit']}.","",
              f"Original SHA-256 {table['input_sha256']}. Frozen mapping SHA-256 {table['mapping_sha256']}.",
              f"Capability {primary['method']['capability']}. Manifest {primary['method_manifest_sha256']}."]
    (work/"report.md").write_text("\n".join(lines)+"\n")
    return outcome
