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


def audit(root):
    with closing(sqlite3.connect((root/"state/gx.db").resolve().as_uri()+"?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        events = db.execute("""SELECT event_id,run_id,payload_json FROM events
            WHERE kind='v624.decision.rejected' AND json_extract(payload_json,'$.code')='SCOPE_DENIED'""")
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
                            "delivered_own_source_record":ref in owned, "replay":status})
        return {"rows":sorted(results,key=lambda row:row["original_event"]),
                "unmatched_original_submissions":len(rejected)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs",type=Path,nargs="+",help="Deployment directories containing state and codex/sessions")
    parser.add_argument("--output",required=True,type=Path)
    args = parser.parse_args()
    groups = [audit(folder) for folder in args.runs]
    args.output.write_text(json.dumps({"scope":"Read-only original request replay at evidence-scope check",
                                       "runs":groups},indent=2)+"\n")
    raise SystemExit(0 if all(g["rows"] and not g["unmatched_original_submissions"] and
        all(r["replay"]=="accepted_by_evidence_scope_check" for r in g["rows"]) for g in groups) else 1)
