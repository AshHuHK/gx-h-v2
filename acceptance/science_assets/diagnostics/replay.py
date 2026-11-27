"""Read preserved native calls from the field store; replay only public contract validation."""
import argparse
import json
from pathlib import Path
import sqlite3

from gx_core.domain import ids
from gx_core.errors import SchemaInvalid
from gx_core.p0 import v621_policy, v62_native, v6_graph


def replay(path, run_id):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        policy = v621_policy.pinned(conn, run_id)
        spec = v62_native.evidence_tool("gx_decide_revision", policy)["inputSchema"]["properties"]["decision"]
        events = {json.loads(row["payload_json"])["submission"]: row for row in conn.execute(
            "SELECT event_id,payload_json FROM events WHERE run_id=? AND kind='v624.decision.rejected'", (run_id,))}
        rows = conn.execute("""SELECT n.thread_id,n.turn_id,n.call_id,n.request_json,c.result_json
            FROM p0_root_calls c JOIN p0_v6_native_calls n USING(call_id,thread_id,turn_id)
            JOIN p0_roots r USING(thread_id) WHERE r.run_id=?
            AND json_extract(n.request_json,'$.tool')='gx_decide_revision' ORDER BY c.rowid""", (run_id,))
        refusals = []
        for row in rows:
            previous = json.loads(row["result_json"]).get("decision_refusal", {})
            if "stop_condition" not in previous.get("message", "") and previous.get("message") != "Unknown branch budget unit":
                continue
            identity = ids.sha256_of_obj([row["thread_id"], row["turn_id"], row["call_id"]])
            event = events[identity]
            decision = json.loads(row["request_json"])["arguments"]["decision"]
            try:
                v6_graph._shape(decision, spec)
            except SchemaInvalid as error:
                current = error.to_payload()
            else:
                raise AssertionError(f"Original refused submission {identity} is now allowed")
            assert previous["code"] == current["code"] and previous["side_effect_status"] == current["side_effect_status"]
            assert current["details"]["field"].startswith("ops[")
            refusals.append(dict(event=event["event_id"], submission=identity,
                                 decision_sha256=ids.sha256_of_obj(decision),
                                 original_event=json.loads(event["payload_json"]),
                                 original_refusal=previous, replay_refusal=current))
        assert refusals
        return {"run_id": run_id, "policy": policy, "source": path.name,
                "role": "read-only contract replay; no new model or Job",
                "cases": refusals}
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=Path)
    parser.add_argument("run_id")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with args.output.open("x", encoding="utf-8") as target:
        json.dump(replay(args.store, args.run_id), target, ensure_ascii=False, indent=2)
        target.write("\n")
