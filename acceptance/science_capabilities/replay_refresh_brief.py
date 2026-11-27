"""Reproject an existing frozen Lead brief using only its historical scope and facts."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gx-h"))
from gx_core.domain import ids
from gx_core.p0 import v6_goal, v6_native, v62_lead, v62_catalog


def replay(state, run_id, cursor):
    with tempfile.TemporaryDirectory() as scratch, closing(sqlite3.connect(
            (state / "gx.db").resolve().as_uri() + "?mode=ro", uri=True)) as original, closing(
            sqlite3.connect(str(Path(scratch) / "gx.db"), isolation_level=None)) as conn:
        original.execute("PRAGMA query_only=ON")
        original.backup(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""SELECT r.brief,t.visible_refs_json FROM v6_decision_tasks t
            JOIN research_contexts c USING(context_ref) JOIN p0_roots r USING(task_id)
            WHERE t.run_id=? AND t.scope='run' AND c.event_cursor=? ORDER BY r.attempt LIMIT 1""",
            (run_id, cursor)).fetchone()
        wire = json.loads(row["brief"].removeprefix(v6_native.PREFIX))
        original_ref = wire["full_brief_ref"]
        artifact = conn.execute("SELECT namespace,rel_path,sha256 FROM artifacts WHERE run_id=? AND artifact_ref=?",
                                (run_id, original_ref)).fetchone()
        raw = (state / "artifacts" / artifact["namespace"] / artifact["rel_path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
        full = json.loads(raw)
        changes = {c["event_id"] for c in full["trigger"]["changes"]}
        events = [r for r in v6_goal._events(conn, run_id, full["trigger"]["since_cursor"]) if r["event_id"] in changes]
        assert {r["event_id"] for r in events} == changes
        v62_lead.describe_refresh(conn, full, events)
        limit = v62_catalog.policy(conn, run_id)["brief_bytes"]
        _, revised = v62_lead.deliver(conn, full, limit)
        trigger = revised["trigger"]
        delta = [r for r in trigger["changes"] if r["event_id"] > trigger["refresh"]["after_cursor"]
                 and r["event"] != "v6.decision.superseded"]
        facts = [{k:v for k,v in row.items() if k in {"event", "event_id", "verdict", "before", "after", "review_ref",
                 "source_ref", "segment_id", "extraction_ref", "previous_claim_ref", "claim_revision_id", "target_claim_ref"}}
                 for row in delta]
        granted = set(json.loads(row["visible_refs_json"]))
        assert all(ref in granted for item in facts for k, ref in item.items()
                   if k in ("review_ref", "source_ref", "extraction_ref", "claim_revision_id"))
        size = len((v6_native.PREFIX + ids.canonical_json(revised)).encode())
        assert size <= limit
        return {"verification":"offline reprojection of a frozen brief on a temporary database copy",
                "run_id":run_id, "original_full_brief_sha256":artifact["sha256"], "through_cursor":cursor,
                "original_wire_bytes":len(row["brief"].encode()), "old_wire_claim_count":len(wire["claims"]),
                "old_wire_change_count":len(wire["trigger"].get("changes", ())), "full_change_count":len(changes),
                "wire_bytes":size, "wire_limit":limit, "refresh":trigger["refresh"], "changes":facts,
                "new_oauth_turns":0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("run_id")
    parser.add_argument("cursor", type=int)
    args = parser.parse_args()
    print(json.dumps(replay(args.state, args.run_id, args.cursor), ensure_ascii=False, indent=2))
