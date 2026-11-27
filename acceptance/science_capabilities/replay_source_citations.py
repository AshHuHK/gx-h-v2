"""Replay only the evidence-scope check over original native attempts. Never adopt a decision."""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gx-h"))
from gx_core.domain import ids
from gx_core.errors import ScopeDenied
from gx_core.p0 import v6_decisions, v62_sources


def decisions(directory):
    for path in directory.rglob("*.jsonl"):
        thread, turn = None, None
        with path.open() as transcript:
            for line in transcript:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # The official host may still be appending its final line.
                payload = record.get("payload", {})
                if record["type"] == "session_meta":
                    thread = payload["id"]
                elif record["type"] == "turn_context" or payload.get("type") == "task_started":
                    turn = payload.get("turn_id") or turn
                elif (thread and turn and record["type"] == "response_item" and
                      payload.get("name") == "gx_decide_revision" and "arguments" in payload):
                    yield thread, turn, payload["call_id"], json.loads(payload["arguments"])["decision"]


def origins(db, task, thread, turn, call, decision):
    native = db.execute("""SELECT rowid,request_json FROM p0_v6_native_calls
                        WHERE thread_id=? AND turn_id=? AND call_id=?""", (thread,turn,call)).fetchone()
    arguments = {"decision":decision}
    if json.loads(native["request_json"])["arguments"] != arguments:
        raise ValueError("Official function arguments differ from the recorded GX call")
    refs = set(decision["evidence_refs"]) - set(json.loads(task["visible_refs_json"]))
    records = []
    for ref in sorted(refs):
        receipts = db.execute("""SELECT json_extract(n.request_json,'$.tool') AS tool,n.call_id,a.kind,a.sha256
            FROM p0_v6_native_calls n JOIN p0_root_calls c USING(thread_id,call_id,turn_id)
            JOIN artifacts a ON a.artifact_ref=?
            WHERE n.thread_id=? AND n.turn_id=? AND n.rowid<? AND a.run_id=?
            AND ? IN (json_extract(c.result_json,'$.source_ref'), json_extract(c.result_json,'$.artifact_ref'),
                      json_extract(c.result_json,'$.claim_ref')) ORDER BY n.rowid""",
            (ref,thread,turn,native["rowid"],task["run_id"],ref))
        records.append({"ref":ref,"earlier_same_turn_receipts":[dict(row) for row in receipts]})
    return {"gx_call_sequence":native["rowid"], "call_id":call, "thread_id":thread, "turn_id":turn,
            "official_arguments_match_gx":True, "argument_sha256":ids.sha256_of_obj(arguments),
            "nonfrozen_citations":records}


def audit(root, run_filter=None):
    with closing(sqlite3.connect((root/"state/gx.db").resolve().as_uri()+"?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        events = db.execute("""SELECT event_id,run_id,payload_json FROM events
            WHERE kind='v624.decision.rejected' AND json_extract(payload_json,'$.code')='SCOPE_DENIED'
            AND (? IS NULL OR run_id=?)""", (run_filter,run_filter))
        rejected = {}
        for event in events:
            info = json.loads(event["payload_json"])
            field = re.fullmatch(r"evidence_refs\[(\d+)\]", info.get("field", ""))
            if field:
                rejected[info["submission"]] = event["event_id"], event["run_id"], int(field[1])
        results = []
        for thread, turn, call, decision in decisions(root/"codex/sessions"):
            key = ids.sha256_of_obj([thread, turn, call])
            if key not in rejected:
                continue
            number, run, index = rejected.pop(key)
            task = db.execute("""SELECT t.*,r.lease_id FROM p0_roots r JOIN v6_decision_tasks t USING(task_id)
                WHERE r.thread_id=? AND r.run_id=?""", (thread,run)).fetchone()
            ref = decision["evidence_refs"][index]
            owned = v62_sources.decision_citations(db, task, task["lease_id"], thread, turn, {ref})
            try:
                v6_decisions._visible(db, task, task["lease_id"], thread, turn, decision)
                status = "accepted_by_evidence_scope_check"
            except ScopeDenied as err:
                status = err.details
            results.append({"run":run,"original_event":number,"scope":task["scope"],"submission_sha256":key,
                            "field":f"evidence_refs[{index}]","artifact_ref":ref,
                            "in_frozen_grant":ref in json.loads(task["visible_refs_json"]),
                            "delivered_own_source_record":ref in owned, "replay":status,
                            **(origins(db,task,thread,turn,call,decision) if run_filter else {})})
        return {"rows":sorted(results,key=lambda row:row["original_event"]),
                "unmatched_original_submissions":len(rejected)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs",type=Path,nargs="+",help="Deployment directories containing state and codex/sessions")
    parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--run-id", action="append", help="Exact Run ID for each corresponding archive, in order")
    args = parser.parse_args()
    if args.run_id and len(args.run_id) != len(args.runs):
        parser.error("Provide one --run-id per archive")
    groups = [audit(folder, rid) for folder, rid in zip(args.runs, args.run_id or [None] * len(args.runs))]
    args.output.write_text(json.dumps({"scope":"Read-only original request replay at evidence-scope check",
                                       "runs":groups},indent=2)+"\n")
    raise SystemExit(0 if all(g["rows"] and not g["unmatched_original_submissions"] and
        all(r["replay"]=="accepted_by_evidence_scope_check" for r in g["rows"]) for g in groups) else 1)
