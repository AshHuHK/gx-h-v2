"""Replay the archived PM2.5 Lead's closing request against a read-only SQLite snapshot."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from gx_core.p0 import research_closeout


def replay(state, archived_report):
    report_bytes = archived_report.read_bytes()
    report = json.loads(report_bytes)
    run_id = report["run_id"]
    coverage = report["publication_coverage"]["unique_publications"]
    assert (coverage["count"], coverage["marked_open_access"], coverage["located_full_text"]) == (160, 41, 9)
    with sqlite3.connect(state.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        goal = conn.execute("SELECT status,stop_decision_ref FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
        assert goal["status"] == "CLOSED"
        close = conn.execute("SELECT task_id,request_json FROM v6_decisions WHERE decision_ref=? AND run_id=?",
                             (goal["stop_decision_ref"], run_id)).fetchone()
        task = conn.execute("SELECT * FROM v6_decision_tasks WHERE task_id=?", (close["task_id"],)).fetchone()
        decision = json.loads(close["request_json"])["decision"]
        assert task["scope"] == "run" and [op["op"] for op in decision["ops"]] == ["close_run"]
        assert research_closeout.maybe_prompt(conn, task, decision) is None
        assert research_closeout.maybe_prompt(conn, task, {**decision, "stop_reason": "INSUFFICIENT_DATA"}) is None
        prompt_count = conn.execute("SELECT count(*) FROM events WHERE run_id=? AND kind=?",
                                    (run_id, research_closeout.PROMPT)).fetchone()[0]
        assert not prompt_count and not conn.total_changes
    return {"level": "UNIT/ARCHIVED_LIVE_REPLAY", "run_id": run_id, "archived_decision": goal["stop_decision_ref"],
            "report_sha256": hashlib.sha256(report_bytes).hexdigest(), "unique_publications": coverage,
            "original_stop_reason": decision.get("stop_reason"), "original_prompted": False,
            "same_decision_with_explicit_insufficient_data_prompted": False, "prompt_events": prompt_count,
            "new_jobs": 0, "new_model_turns": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("archived_report", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.state, args.archived_report), indent=2))
