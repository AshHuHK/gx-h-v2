"""Read field-acceptance metrics from one real GX store, including their denominators."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote


def elapsed(start, end):
    return round((datetime.fromisoformat(end.replace("Z", "+00:00")) -
                  datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds(), 3)


def metrics(db, run_id, qualifying_jobs, expected_method=None):
    with closing(sqlite3.connect("file:" + quote(str(Path(db).resolve())) + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if not run_id:
            rows = conn.execute("SELECT run_id FROM v6_goals").fetchall()
            if len(rows) != 1:
                raise ValueError("Select one Run with --run")
            run_id = rows[0][0]
        run = conn.execute("SELECT created_at,status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not run:
            raise ValueError("Unknown Run")
        events = conn.execute("SELECT event_id,ts,kind,subject_id,payload_json FROM events WHERE run_id=? AND "
                              "kind IN ('research.catalog.surveyed','v624.decision.rejected','v6.node.settled') "
                              "ORDER BY event_id", (run_id,)).fetchall()
        jobs = conn.execute("SELECT job_id,capability_id,capability_version,status,idempotency_key,runner_started "
                            "FROM jobs WHERE run_id=? ORDER BY created_at,job_id", (run_id,)).fetchall()
        started = {r[0] for r in conn.execute("SELECT DISTINCT subject_id FROM events WHERE run_id=? AND "
                                              "kind='job.transition' AND new_state='RUNNING'", (run_id,))}
        keys_per_job = conn.execute("SELECT a.exec_key,COUNT(DISTINCT n.job_id) AS actual FROM v6_job_attempts a "
                                    "JOIN v6_nodes n USING(node_id,run_id) WHERE a.run_id=? AND n.job_id IS NOT NULL "
                                    "AND a.reused_from IS NULL GROUP BY a.exec_key HAVING actual > 1", (run_id,)).fetchall()
        surveyed, rejected, settled = [], [], []
        for e in events:
            payload = json.loads(e["payload_json"])
            if e["kind"] == "research.catalog.surveyed":
                shortlist = payload.get("first_candidates")
                if shortlist is None:
                    original = conn.execute("SELECT result_json FROM p0_root_calls WHERE thread_id=? AND call_id=?",
                                            (payload["thread_id"], payload["call_id"])).fetchone()
                    shortlist = [{k: card[k] for k in ("reference","kind","provider","status") if k in card}
                                 for card in json.loads(original[0])["items"][:5]] if original else []
                surveyed.append({"event":e["event_id"],"through_event":payload["through_event"],
                                 "total":payload["total"],"top":shortlist})
            if e["kind"] == "v624.decision.rejected":
                rejected.append({"event":e["event_id"],"reason":payload.get("reason"), "field":payload.get("field")})
            if e["kind"] == "v6.node.settled" and payload.get("status") in ("SUCCEEDED", "REUSED"):
                if payload.get("result_ref"):
                    settled.append({"event":e["event_id"],"job_id":payload["job_id"],"result_ref":payload["result_ref"],
                                    "ts":e["ts"],"seconds_from_open":elapsed(run["created_at"],e["ts"])})
        if set(qualifying_jobs) - {x["job_id"] for x in settled}:
            raise ValueError("Each qualifying Job must have delivered a named result on this Run")
        effective = next((s for s in settled if s["job_id"] in qualifying_jobs), None)
        keys = Counter(j["idempotency_key"] for j in jobs if j["idempotency_key"])
        first = surveyed[0]["top"] if surveyed else []
        target_jobs = [j for j in jobs if f'{j["capability_id"]}@{j["capability_version"]}' == expected_method]
        return {"run_id":run_id,"status":run["status"],"opened":run["created_at"],"surveys":surveyed,
                "first_effective_delivery":effective,"adjudicated_qualifying_jobs":qualifying_jobs,
                "rejections":rejected,"rejection_count":len(rejected),
                "surplus_jobs_with_identical_idempotency_key":sum(count-1 for count in keys.values()),
                "repeated_graph_execution_keys":[dict(r) for r in keys_per_job],
                "jobs":[{k:j[k] for k in ("job_id","capability_id","capability_version","status")} for j in jobs],
                "shortlist_expectation":expected_method,
                "target_first_top_five_rank":next((i for i,r in enumerate(first,1)
                                                   if r["reference"] == expected_method), None),
                "expected_method_admitted":bool(target_jobs) if expected_method else None,
                "expected_method_executed":any(j["job_id"] in started or j["runner_started"] or
                                               j["status"] == "SUCCEEDED" for j in target_jobs) if expected_method else None,
                "expected_method_succeeded":any(j["status"] == "SUCCEEDED" for j in target_jobs) if expected_method else None,
                "definitions":"Delivery time is GX node settlement of a study-adjudicated result. "
                      "Repeated graph execution keys count distinct actual Job IDs with the same frozen "
                      "execution identity. Independent repeats have a distinct key. Whether a repeat or a "
                      "rejection was avoidable requires inspection of its cause."}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("database")
    cli.add_argument("--run")
    cli.add_argument("--qualifying-job", action="append", default=[])
    cli.add_argument("--expected-method")
    args = cli.parse_args()
    print(json.dumps(metrics(args.database, args.run, args.qualifying_job, args.expected_method),
                     indent=2, ensure_ascii=False))
