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


def completed_close(db, rid):
    """Resolve the Goal owner's closing operation to its commit and completion events."""
    row = db.execute("""
        SELECT g.goal_version, d.decision_ref, g.stop_op_digest AS op_digest,
               op.value AS resolved_op, requested.event_id AS requested_event,
               committed.event_id AS committed_event, closed.event_id AS closed_event,
               closed.ts AS closed_utc
        FROM v6_goals g
        JOIN v6_decisions d ON d.run_id=g.run_id AND d.decision_ref=g.stop_decision_ref
        JOIN v6_decision_tasks t ON t.task_id=d.task_id AND t.run_id=g.run_id
            AND t.scope='run' AND t.goal_version=g.goal_version
        JOIN json_each(d.receipt_json,'$.resolved_ops') op
            ON json_extract(op.value,'$.spec.op')='close_run'
            AND json_extract(op.value,'$.op_digest')=g.stop_op_digest
        JOIN events requested ON requested.run_id=g.run_id AND requested.subject_id=g.run_id
            AND requested.kind='v6.run.close_requested'
            AND json_extract(requested.payload_json,'$.decision_id')=d.decision_ref
            AND json_extract(requested.payload_json,'$.op_digest')=g.stop_op_digest
        JOIN events committed ON committed.run_id=g.run_id AND committed.subject_id=d.decision_ref
            AND committed.kind='v6.decision.committed'
            AND committed.event_id=json_extract(d.receipt_json,'$.event_id')
        JOIN events closed ON closed.run_id=g.run_id AND closed.subject_id=g.run_id
            AND closed.kind='v6.run.closed'
            AND json_extract(closed.payload_json,'$.decision_id')=d.decision_ref
            AND json_extract(closed.payload_json,'$.op_digest')=g.stop_op_digest
        WHERE g.run_id=? AND g.status='CLOSED'
            AND json_extract(d.receipt_json,'$.goal_version')=g.goal_version
            AND json_extract(d.receipt_json,'$.outcome')='adopted'
            AND requested.event_id < committed.event_id AND committed.event_id < closed.event_id
        ORDER BY closed.event_id DESC LIMIT 1
        """,(rid,)).fetchone()
    if row is None:
        raise ValueError("Missing enacted close for the current Goal version")
    closure = dict(row)
    closure["operation"] = json.loads(closure.pop("resolved_op"))["spec"]
    return closure


def report_lines(outcome):
    final, close = outcome["final_decision"], outcome["closure"]
    lines = ["# "+outcome["classification"], "", outcome["qc_recommendation"], "",
             f"Goal version {close['goal_version']} closed at event {close['closed_event']} "
             f"by decision {final['decision_ref']}.", ""]
    operation = close["operation"]
    if operation["reason"] != final["rationale"]:
        lines.extend([operation["reason"], ""])
    if operation["unresolved"]:
        lines.extend(["Unresolved at closure", ""])
        lines.extend("- "+limit for limit in operation["unresolved"])
    lines.extend(["", "Evidence cited in the closing decision", ""])
    lines.extend("- "+ref for ref in final["evidence_refs"])
    decisions = {d["decision_ref"]:d for d in outcome["decisions"]}
    for index, job in enumerate(outcome["jobs"]):
        heading = "Primary measurement" if index == 0 else "Follow-up measurement"
        lines.extend(["", "## "+heading, "", f"Job {job['job_id']}.", ""])
        if "table" not in job:
            lines.append(job["status"])
            continue
        table = job["table"]
        lines.extend([f"{outcome['opening']['commitment']['prediction']['blind_id']}, "
                      f"{table['interval']}, {table['mask']}, {table['endpoint']} ({table['unit']}). "
                      "Shared chains are dependent observations.", "",
                      "| Pair | Before | After | Absolute change |", "|---|---:|---:|---:|"])
        for key in ("pair12","pair13","pair23","median"):
            row = table["rows"][key]
            values = row[table["endpoint"]]
            cells = [key+" ("+",".join(row["chains"])+")"] + [
                "NA" if values[k] is None else str(values[k]) for k in ("before","after","delta")]
            lines.append("| "+" | ".join(cells)+" |")
        lines.extend(["", f"Matched {table['selected_residues']}, omitted {table['omitted_residues']}, "
                          f"retained {table['retained_residues']}.",
                      f"Independent maximum delta difference {job['independent']['max_delta_difference']} "
                      f"{table['unit']}, tolerance {table['tolerance']} {table['unit']}.", "",
                      f"Output {job['output']['artifact_ref']}. Original SHA-256 {table['input_sha256']}. "
                      f"Frozen mapping SHA-256 {table['mapping_sha256']}.",
                      f"Capability {table['method']}. Job manifest {job['manifest_sha256']}."])
        for ref in job["followup_refs"]:
            if ref == final["decision_ref"]:
                lines.extend(["", f"Cited by the closing decision at event {final['event_id']}."])
            else:
                lines.extend(["", f"Decision {ref} (event {decisions[ref]['event_id']})", "", decisions[ref]["rationale"]])
    return lines


def export(work, rid, reg, cold_hash, ledger, *, destination=None):
    slot = work/"lead"
    op = read(work/"custody/opening.json")
    if op["run_id"] != rid:
        raise ValueError("Opening belongs to another Run")
    primary = op["commitment"]
    sessions = native.native_session(slot/"codex/sessions")
    history = {t:thread_items(path) for t,path in sessions.items()}
    decisions, readings, refused, jobs = [], [], [], []
    with closing(rehearse.db_open(slot)) as db:
        db.execute("BEGIN")
        closure = completed_close(db, rid)
        if primary["goal_version"] != closure["goal_version"]:
            raise ValueError("Primary commitment belongs to another Goal version")
        for d in protocol.accepted(db,rid):
            thread,turn,cid=d["thread_id"],d["turn_id"],d["call_id"]
            root = db.execute("SELECT brief FROM p0_roots WHERE run_id=? AND thread_id=?", (rid,thread)).fetchone()
            if not root or thread not in sessions:
                raise ValueError("Missing original Lead transcript or brief for "+d["decision_ref"])
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
            received = [r for r in readings if r["ref"]==out["artifact_ref"] and r["complete_segment"]
                        and history[r["thread_id"]][1][r["turn_id"],r["call_id"]]["value"] == table]
            followups = [d for d in decisions if d["event_id"]>published[0] and out["artifact_ref"] in d["evidence_refs"]
                         and any(r["thread_id"]==d["thread_id"] and r["turn_id"]==d["turn_id"] and r["output_line"]<d["native_line"]
                                 for r in received)]
            job.update(table=table, output=out,admitted=dict(submitted),published=dict(published),
                       independent=checked, received=received,followup_refs=[d["decision_ref"] for d in followups])
    if not jobs or jobs[0]["parameters"] != primary["method"]["parameters"] or not jobs[0].get("followup_refs"):
        raise ValueError("No complete, source-bound primary table was received and cited in an enacted action")
    for later in jobs[1:]:
        if later["commission"] not in jobs[0]["followup_refs"]:
            raise ValueError("Additional measurement lacks a new decision after receipt of the primary result")
    final = next(d for d in decisions if d["decision_ref"] == closure["decision_ref"])
    last_measurement = max((j for j in jobs if "table" in j), key=lambda j: j["published"]["event_id"])
    if final["decision_ref"] not in last_measurement["followup_refs"]:
        raise ValueError("Final decision lacks receipt and citation of the last measurement")
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
               "closure":closure,"final_decision":final,
               "readback":{"physical_jobs":len(jobs),"artifact_reads":len(readings),"read_induced_jobs":0},
               "cost":account,"field_retries":refused,"cold_state_sha256":cold_hash}
    dest = destination if destination is not None else work
    lines = report_lines(outcome)
    (dest/"report.md").write_text("\n".join(lines)+"\n")
    save(dest/"study-receipt.json",outcome)
    return outcome
