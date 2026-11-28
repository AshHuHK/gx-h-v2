"""Inspect immutable event and official conversation receipts without exporting the scientific materials."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from gx_core.p0 import v62_rounds


def strings(value):
    if isinstance(value, str):
        yield value
        if value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except ValueError:
                return
            yield from strings(decoded)
    elif isinstance(value, (dict, list)):
        for child in (value.values() if isinstance(value, dict) else value):
            yield from strings(child)


def trace(arm):
    uri = (arm / "state/gx.db").resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rid, decision = conn.execute("SELECT run_id,stop_decision_ref FROM v6_goals").fetchone()
        row = conn.execute("""SELECT d.thread_id,d.turn_id,d.context_ref,c.event_cursor,c.snapshot_json,
            d.receipt_json,d.request_json,r.brief FROM v6_decisions d JOIN research_contexts c USING(context_ref)
            JOIN p0_roots r ON r.thread_id=d.thread_id WHERE d.run_id=? AND d.decision_ref=?""",(rid,decision)).fetchone()
        packet = json.loads(row["snapshot_json"])
        receipt = json.loads(row["receipt_json"])
        clock = receipt["close_context"]
        assert hashlib.sha256(row["brief"].encode()).hexdigest() == clock["brief_sha256"]
        delivery = conn.execute("""SELECT text_sha256 FROM p0_root_deliveries WHERE run_id=? AND thread_id=?
            AND turn_id=? AND event_id=? AND state='acknowledged' AND method='turn/start'""",
            (rid,row["thread_id"],clock["brief_turn"],clock["brief_delivery"])).fetchone()
        assert delivery and delivery[0] == clock["brief_sha256"]
        mid = json.loads((arm/"late-delivery.json").read_text())["receipt"]["message_id"]
        notices = [(e["event_id"], json.loads(e["payload_json"])) for e in conn.execute(
            """SELECT event_id,payload_json FROM events WHERE run_id=? AND kind='research.pi.feedback'
               AND json_extract(payload_json,'$.source_message_id')=? ORDER BY event_id""",(rid,mid))]
        assert len(notices) == 2
        routed = conn.execute("SELECT event_id FROM events WHERE run_id=? AND kind='pi.message.routed' AND subject_id=?",
                              (rid,mid)).fetchone()[0]
        end = conn.execute("SELECT event_id FROM events WHERE run_id=? AND kind='v6.run.closed'",(rid,)).fetchone()[0]
        # Hide only events created by the successful closing transaction and its successors.
        # The read-only main DB remains untouched. The historical GOAL version is still current in the archive.
        first_write = conn.execute("""SELECT event_id FROM events WHERE run_id=? AND kind='artifact.published'
                                     AND subject_id=?""",(rid,decision)).fetchone()[0]
        conn.execute(f"CREATE TEMP VIEW events AS SELECT * FROM main.events WHERE event_id < {int(first_write)}")
        conn.execute("PRAGMA query_only=ON")
        late = v62_rounds.late_evidence(conn,rid,row["event_cursor"])
        transcript = {}
        for file in (arm/"sessions").rglob("*.jsonl"):
            found = []
            for line in file.open():
                event = json.loads(line)
                if event.get("type") not in ("response_item","event_msg"):
                    continue
                item = event.get("payload",{})
                contents = list(strings(item))
                ids = [eid for eid,n in notices if any(n["feedback_id"] in text for text in contents)]
                words = [eid for eid,n in notices if any(n["text"] in text for text in contents)]
                if ids or words:
                    found.append({"type":item.get("type"),"role":item.get("role"),"clause_ids":ids,"clause_text":words})
            transcript[("deciding_lead" if row["thread_id"] in file.name else file.stem[-36:])] = found
        assert transcript.get("deciding_lead") == []
        assert any(entry["type"] == "message" and entry["role"] == "user" and len(entry["clause_text"]) == 2
                   for matches in transcript.values() for entry in matches)
        assert all(eid>row["event_cursor"] and eid<routed<first_write for eid,_ in notices)
        return {"run_id":rid, "goal_version":clock["goal_version"],"close_decision":decision,
                "closing_thread":row["thread_id"],"closing_turn":row["turn_id"],"brief_context":row["context_ref"],
                "acknowledged_cursor":row["event_cursor"],"acknowledged_brief_sha256":clock["brief_sha256"],
                "feedback_in_deciding_brief":[i["feedback_id"] for i in packet.get("pi_dialogue",[]) if i.get("feedback_id")],
                "pi_feedback":[{"event":eid,"id":n["feedback_id"],"source_kind":n["source_kind"],"scope":n["scope"],
                                "chars":len(n["text"]),"text_sha256":hashlib.sha256(n["text"].encode()).hexdigest()}
                               for eid,n in notices], "routed_event":routed,"official_transcript_matches":transcript,
                "first_closing_write":first_write,"original_decision_event":receipt["event_id"],"closed_event":end,
                "original_stop_reason":clock["stop_reason"],"late_event_detected_on_replay":late, "writes_to_run":0}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm",type=Path)
    print(json.dumps(trace(parser.parse_args().arm),indent=2))
