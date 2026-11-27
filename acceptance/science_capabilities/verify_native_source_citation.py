"""Verify a closed Run's official same-turn evidence delivery and adoption, then reexport it."""
import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gx-h"))
from gx_core.p0.research_routes import _turns


def require(value, message):
    if not value:
        raise ValueError(message)
    return value


def native_items(work, thread, turn):
    paths = list((work/"codex/sessions").rglob(f"rollout-*-{thread}.jsonl"))
    require(len(paths) == 1, "Expected one official session file for the decision thread")
    items = {}
    with paths[0].open() as stream:
        for line, raw in enumerate(stream, 1):
            record = json.loads(raw)
            data = record.get("payload", {})
            item = data.get("item", {})
            if (record["type"] == "event_msg" and data.get("type") == "item_completed" and
                    data.get("thread_id") == thread and data.get("turn_id") == turn and
                    item.get("type") == "DynamicToolCall"):
                items[item["id"]] = (line, item)
    return items


def inspect(work, rid):
    state = work/"state"
    folder = state/"research"/rid
    result = json.loads((folder/"result.json").read_bytes())
    close = result["closure"]
    require(result["goal_status"] == "CLOSED", "Run has not closed")
    with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro",uri=True)) as c:
        c.row_factory = sqlite3.Row
        d = require(c.execute("SELECT * FROM v6_decisions WHERE run_id=? AND decision_ref=?",
                              (rid,close["decision_ref"])).fetchone(),"Missing adopted closure")
        root = c.execute("SELECT * FROM p0_roots WHERE thread_id=? AND task_id=? AND lease_id=?",
                         (d["thread_id"],d["task_id"],d["lease_id"])).fetchone()
        span, = [s for s in _turns(c,rid,state,root) if s["turn_id"]==d["turn_id"]]
        require(span["classification"] == "NATIVE_MODEL","Missing official model response")
        terminal = c.execute("SELECT reason FROM p0_root_terminals WHERE thread_id=? AND turn_id=? AND call_id=?",
                             (d["thread_id"],d["turn_id"],d["call_id"])).fetchone()
        require(terminal and terminal[0]=="GX_ACCEPTED","Missing committed terminal decision")
        request = json.loads(d["request_json"])
        decision = request.get("original_decision",request["decision"])
        items = native_items(work,d["thread_id"],d["turn_id"])
        decision_line, official = items[d["call_id"]]
        require(official["tool"]=="gx_decide_revision" and official["arguments"]["decision"]==decision,
                "Archived model submission differs from the adopted original")
        calls = list(c.execute("""SELECT n.call_id,n.request_json,c.result_json FROM p0_v6_native_calls n
            JOIN p0_root_calls c USING(thread_id,turn_id,call_id) WHERE n.thread_id=? AND n.turn_id=?
            ORDER BY n.rowid""",(d["thread_id"],d["turn_id"])))
        read, extraction = {}, None
        for call in calls:
            req, answer = (json.loads(call[field]) for field in ("request_json","result_json"))
            if req["tool"]=="gx_read_source" and "receipt_id" in answer:
                read[answer["receipt_id"]] = (call["call_id"],answer)
            if req["tool"]=="gx_extract_evidence" and answer.get("evidence_ref") in decision["evidence_refs"]:
                extraction = call["call_id"], req["arguments"], answer
                break
        extract_call, args, answer = require(extraction,"Decision did not cite an extraction from its own turn")
        read_call, passage = read[args["receipt_id"]]
        read_line, read_item = items[read_call]
        extract_line, extract_item = items[extract_call]
        for item, tool, value in ((read_item,"gx_read_source",passage),(extract_item,"gx_extract_evidence",answer)):
            require(item["tool"]==tool and item["success"] and any(
                json.loads(part["text"]) == value for part in item["content_items"] if part.get("text")),
                    "Official tool delivery disagrees with the stored GX result")
        require(read_line < extract_line < decision_line and passage["lease_id"]==d["lease_id"] and
                passage["task_id"]==d["task_id"] and passage["principal"]==d["thread_id"] and
                args["source_ref"]==passage["source_ref"] and args["quote"] in passage["text"] and
                answer["accepted"] and answer["anchor_status"]=="VERBATIM","Broken original-source chain")
        event = c.execute("SELECT event_id FROM events WHERE run_id=? AND kind='v62.source.extracted' AND subject_id=?",
                          (rid,answer["evidence_ref"])).fetchone()[0]
        require(event < close["committed_event"] <= close["closed_event"],"Events are out of order")
        turns = c.execute("SELECT count(DISTINCT turn_id) FROM p0_root_deliveries WHERE run_id=? AND "
                          "method='turn/start' AND state='acknowledged'",(rid,)).fetchone()[0]
    original = ROOT/"examples/plugins/v62_public_materials/deposition-excerpt.txt"
    require(sha256(original.read_bytes()).hexdigest()==passage["source_sha256"], "Different source material")
    return {"run_id":rid,"source":{"file":"deposition-excerpt.txt","artifact_ref":args["source_ref"],
        "sha256":passage["source_sha256"],"segment":passage["segment_id"],"locator":passage["label"],
        "verbatim_quote":args["quote"]},"extraction":{"event":event,"artifact_ref":answer["evidence_ref"],
        "quote_sha256":answer["quote_sha256"],"status":answer["status"]},"native":{"classification":span["classification"],
        "model":root["model"],"effort":root["effort"],"acknowledged_turns":turns,"terminal_reason":terminal[0],
        "start_utc":span["start"],"end_utc":span["end"],"receipt_order":[read_line,extract_line,decision_line],
        "same_task_lease_thread_turn":True,"official_result_matches_GX":True},
        "adopted":{"decision_ref":close["decision_ref"],"event":close["committed_event"],
        "closed_event":close["closed_event"],"ops":[op["op"] for op in decision["ops"]],
        "evidence_refs":decision["evidence_refs"],"rationale":decision["rationale"]},
        "rejected_decisions":[{k:r[k] for k in ("event_id","code","reason","corrected_decision_event")}
                              for r in result["rejected_decisions"]],"jobs":len(result["jobs"]),
        "claim_levels":[c["level"] for c in result["exploratory_claims"]],
        "report_sha256":sha256((folder/"REPORT.md").read_bytes()).hexdigest()}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work",type=Path)
    parser.add_argument("run_id")
    parser.add_argument("--output",required=True,type=Path)
    args=parser.parse_args()
    work=args.work.resolve()
    before=inspect(work,args.run_id)
    command=[sys.executable,"-B",str(ROOT/"acceptance/science_capabilities/gx_oauth.py"),str(work),
             "research","--state",str(work/"state"),"--run-id",args.run_id]
    result=json.loads(subprocess.check_output(command,cwd=ROOT))
    added=result["invocation"]["added"]
    require(result["invocation"]["action"]=="export_closed" and not any(added.values()),"Reexport added work")
    require(inspect(work,args.run_id)==before,"Reexport changed the report or underlying observations")
    release = (work/"launch-release.txt").read_text().strip()
    args.output.write_text(json.dumps({"launch_release":release, **before,"reexport":{"action":"export_closed","added":added,
                                 "same_report_and_decision":True}},ensure_ascii=False,indent=2)+"\n")
