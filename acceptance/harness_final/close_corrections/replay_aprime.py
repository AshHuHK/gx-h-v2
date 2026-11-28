"""Read-only reconstruction of A′'s original decision refusals. Export only engineering receipts."""
import argparse
import json
import sqlite3
from pathlib import Path

from gx_core.errors import GxError
from gx_core.p0 import v6_decisions

FIRST = "call_a308712646334fd3b7383b04f460b1f6"
SECOND = "call_578af7f80d194ef2846032604c762045"


def trace(arm):
    with sqlite3.connect((arm/"state/gx.db").resolve().as_uri()+"?mode=ro",uri=True) as conn:
        conn.row_factory = sqlite3.Row
        requests = {}
        for row in conn.execute("""SELECT c.thread_id,c.turn_id,c.call_id,n.request_json,c.result_json
            FROM p0_root_calls c JOIN p0_v6_native_calls n USING(thread_id,turn_id,call_id)
            WHERE c.call_id IN (?,?)""", (FIRST,SECOND)):
            requests[row["call_id"]] = dict(row)
        first = requests[FIRST]
        root = dict(conn.execute("SELECT * FROM p0_roots WHERE thread_id=?",(first["thread_id"],)).fetchone())
        task = dict(conn.execute("""SELECT t.*,c.event_cursor FROM v6_decision_tasks t JOIN research_contexts c
                        USING(context_ref) WHERE t.task_id=?""",(root["task_id"],)).fetchone())
        rid = task["run_id"]
        handoff = conn.execute("SELECT event_id,payload_json FROM events WHERE run_id=? AND "
            "subject_id=? AND kind='v6.decision.superseded'",(rid,task["task_id"])).fetchone()
        receipts = {k:json.loads(v["result_json"])["decision_refusal"] for k,v in requests.items()}
        calls = {k:json.loads(v["request_json"])["arguments"]["decision"] for k,v in requests.items()}
        missing = set(receipts[FIRST]["details"]["uncited_review_refs"])
        assert len(missing)==5 and missing <= set(calls[SECOND]["evidence_refs"])
        session, = (arm/"codex/sessions").rglob("*"+first["thread_id"]+"*.jsonl")
        wire = {}
        for line in session.open():
            entry = json.loads(line).get("payload",{})
            if entry.get("call_id") in requests and entry.get("type") in ("custom_tool_call_output","function_call_output"):
                raw = entry["output"]
                wire[entry["call_id"]] = json.loads(raw) if isinstance(raw,str) else raw
        assert set(wire)==set(requests)
        # Use the event prefix observed just before GX first retired this lease.
        stop = conn.execute("""SELECT max(event_id) FROM events WHERE run_id=? AND kind='task.transition'
             AND subject_id=? AND new_state='CANCELLED' AND event_id<?""",(rid,task["task_id"],handoff[0])).fetchone()[0]
        assert stop
        conn.execute(f"CREATE TEMP VIEW events AS SELECT * FROM main.events WHERE event_id < {int(stop)}")
        conn.execute("PRAGMA query_only=ON")
        allowed = v6_decisions._own_close_reviews(conn,task,root,first["turn_id"],calls[FIRST])
        assert missing == allowed.difference(calls[FIRST]["evidence_refs"])
        try:
            v6_decisions._require_close_context(conn,rid,task,root,first["turn_id"],calls[FIRST])
        except GxError as refusal:
            replayed = refusal.to_payload()
        else:
            raise AssertionError("Closing should have required the omitted reviews.")
        corrected_context = v6_decisions._require_close_context(conn,rid,task,root,first["turn_id"],calls[SECOND])
        return {"run_id":rid,"deciding_task":task["task_id"],"thread":first["thread_id"],"turn":first["turn_id"],
                "source_cursor":task["event_cursor"],"before_retirement_event":stop,"retirement_event":handoff[0],
                "original_refusals":receipts,"actual_original_tool_outputs":wire, "own_delivered_reviews":sorted(allowed),
                "first_close_with_this_code":replayed, "corrected_close_at_same_prefix_passes_freshness":bool(corrected_context),
                "external_calls":0,"writes_to_research_run":0}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm",type=Path)
    print(json.dumps(trace(parser.parse_args().arm),indent=2))
