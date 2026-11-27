"""Export a portable index to the original native calls, jobs, products, and adopted science."""
import argparse
from contextlib import closing
import hashlib
import json
import re
from pathlib import Path
import sqlite3


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def native_errors(session_folder):
    """Rejected tool drafts do not appear among the successfully committed callbacks."""
    failures = []
    for session in sorted(session_folder.rglob("*.jsonl")):
        calls = {}
        with session.open() as stream:
            for line in stream:
                record = json.loads(line)
                payload = record.get("payload", {})
                if record["type"] != "response_item":
                    continue
                call = payload.get("call_id")
                if payload.get("type") in {"custom_tool_call", "function_call"}:
                    calls[call] = sorted(set(re.findall(r"tools\.([a-z0-9_]+)\s*\(", payload.get("input", ""))))
                if payload.get("type") not in {"custom_tool_call_output", "function_call_output"}:
                    continue
                blocks = payload.get("output", [])
                if isinstance(blocks, str):
                    blocks = [{"text": blocks}]
                for block in blocks:
                    text = block.get("text", "")
                    for reason in re.findall(r'((?:SCHEMA_INVALID|SCOPE_DENIED|STALE_REVISION): [^\\"\r\n]+)', text):
                        failures.append({"utc":record["timestamp"], "host_call":call, "tools_in_call":calls.get(call, []),
                                         "reason":reason})
    return failures


def export(parent, case):
    deployment=parent/case/"release"
    state=parent/case/"state"
    enrollment=json.loads((deployment/"a1-deployment.json").read_text())
    with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro",uri=True)) as db:
        db.row_factory=sqlite3.Row
        rid,status=db.execute("SELECT run_id,status FROM runs").fetchone()
        data={"case":case, "run_id":rid, "status":status, "goal_status":db.execute(
            "SELECT status FROM v6_goals WHERE run_id=?",(rid,)).fetchone()[0],
              "enrollment":enrollment, "material_sha256":sha(deployment/"input/material.json"),
              "question_sha256":sha(deployment/"input/question.md")}
        data["host_turns"]=[dict(row) for row in db.execute(
            "SELECT thread_id,turn_id,event_id,state FROM p0_root_deliveries WHERE run_id=? AND method='turn/start' AND turn_id IS NOT NULL ORDER BY event_id",(rid,))]
        calls=[]
        for row in db.execute("""SELECT c.request_json,r.result_json FROM p0_v6_native_calls c
                                JOIN p0_root_calls r USING(thread_id,call_id) ORDER BY c.rowid"""):
            x,y=map(json.loads,row)
            call={"tool":x["tool"], "turn":x["turnId"], "call_id":x["callId"]}
            args=x["arguments"]
            if x["tool"]=="gx_query_catalog":
                call.update(arguments=args, total=y.get("total"), status=y.get("status"))
                if "items" in y:
                    call["top_references"]=[i["reference"] for i in y["items"][:3]]
                    call["subject_candidates"]=[dict(rank=n, **{k:r[k] for k in (
                        "reference","provider","kind","status","input_roles","output_names","match_reason","execution_granted")
                        if k in r}) for n,r in enumerate(y["items"],1) if
                        r["reference"].startswith(("discharge-","gauge-logbook@"))]
                if y.get("survey_event"):
                    call["survey_event"]=y["survey_event"]
            elif x["tool"]=="gx_read_artifact":
                call["read_ref"]=args["ref"]
                call["result_schema"]=y.get("schema")
            elif x["tool"]=="gx_select_capability":
                call["arguments"]=args
                call["revision"]=y.get("revision")
            elif x["tool"] in {"gx_decide_revision","gx_stage_claim"}:
                if ref:=y.get("decision_ref") or y.get("claim_ref"):
                    call["accepted_ref"]=ref
            if y.get("error"):
                call["error"]=y["error"]
            calls.append(call)
        data["native_calls"]=calls
        data["rejected_host_tool_results"]=native_errors(parent/case/"official"/"codex"/"sessions")
        data["surveys"]=[{"event_id":i,"utc":ts,**json.loads(raw)} for i,ts,raw in db.execute(
            "SELECT event_id,ts,payload_json FROM events WHERE kind='research.catalog.surveyed' ORDER BY event_id")]
        data["endpoint_deliveries"]=[dict(event_id=i,utc=ts,**json.loads(raw)) for i,ts,raw in db.execute(
            "SELECT event_id,ts,payload_json FROM events WHERE kind='v623.endpoints.received' ORDER BY event_id")]
        jobs=[]
        for j in db.execute("SELECT job_id,capability_id,capability_version,status,submitted_at,finished_at FROM jobs WHERE run_id=? ORDER BY created_at",(rid,)):
            job=dict(j)
            outputs=[]
            for item in db.execute("""SELECT artifact_ref,kind,sha256,rel_path FROM artifacts
                   WHERE run_id=? AND job_id=? AND kind NOT IN ('job_stdout','job_stderr')""",(rid,j["job_id"])):
                path=state/"artifacts"/rid/"jobs"/j["job_id"]/item["rel_path"]
                if sha(path)!=item["sha256"]:
                    raise ValueError("Failed integrity check for "+item["artifact_ref"])
                outputs.append({**dict(item),"values":json.loads(path.read_text())})
            jobs.append({**job,"outputs":outputs})
        data["jobs"]=jobs
        data["decisions"]=[]
        for d in db.execute("SELECT * FROM v6_decisions WHERE run_id=? ORDER BY rowid",(rid,)):
            x=json.loads(d["request_json"])["decision"]
            y=json.loads(d["receipt_json"])
            data["decisions"].append({
                "decision_ref":d["decision_ref"],"turn":d["turn_id"],"event":y.get("event_id"),
                "rationale":x["rationale"],"evidence_refs":x.get("evidence_refs",[]),"ops":x.get("ops",[]),
                "outcome":y.get("outcome"),"endpoint_use":y.get("endpoint_use",{})})
        result=state/"research"/rid/"result.json"
        if result.exists():
            doc=json.loads(result.read_text())
            data.update(closure=doc["closure"], rendered_final=doc["final_decision"]["rendered"],
                        rejected_decisions=doc["rejected_decisions"], report_sha256=sha(result.parent/"REPORT.md"),
                        decision_md_sha256=sha(result.parent/"DECISION.md"))
    return data


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work",type=Path)
    parser.add_argument("output",type=Path)
    args=parser.parse_args()
    saved={k:export(args.work.resolve(),k) for k in ("positive","negative")}
    args.output.write_text(json.dumps(saved,ensure_ascii=False,indent=2)+"\n")
