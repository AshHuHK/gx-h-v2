"""Report the first field Goal's native catalog choices, received text and adopted decisions."""
import argparse
import collections
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def summarize(database):
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        goal, = conn.execute("SELECT run_id,status,goal FROM v6_goals")
        receipts = []
        catalog = []
        kinds = collections.Counter()
        turns = [dict(row) for row in conn.execute(
            """SELECT DISTINCT thread_id,turn_id FROM p0_root_deliveries WHERE run_id=?
               AND state='acknowledged' AND method='turn/start' AND turn_id IS NOT NULL ORDER BY rowid""",
            (goal["run_id"],))]
        for row in conn.execute("""SELECT n.call_id,n.turn_id,n.thread_id,n.request_json,c.result_json,t.scope
                FROM p0_v6_native_calls n JOIN p0_root_calls c USING(thread_id,turn_id,call_id)
                JOIN p0_roots r USING(thread_id) JOIN v6_decision_tasks t ON t.task_id=r.task_id
                WHERE r.run_id=? ORDER BY n.rowid""", (goal["run_id"],)):
            request = json.loads(row["request_json"])
            answer = json.loads(row["result_json"])
            name = request["tool"]
            scope = row["scope"]
            kinds[scope + "/" + name] += 1
            common = {key:row[key] for key in ("call_id","turn_id","thread_id","scope")}
            if name == "gx_query_catalog":
                items = answer.get("items", [])
                entry = dict(common, arguments=request["arguments"],
                             total=answer.get("total"), returned=[i["reference"] for i in items])
                if "spec" in answer:
                    spec = answer["spec"]
                    entry["opened"] = {key:spec[key] for key in
                                       ("reference","kind","purpose","status","digest","reading_guide","limitations")
                                       if key in spec}
                catalog.append(entry)
            elif name == "gx_read_source" and answer.get("receipt_id"):
                receipts.append(dict(common, **{key:answer.get(key) for key in
                                     ("source_ref","segment_id","receipt_id","text_sha256","label")}))
        included = ("v6.claim.staged","v6.claim.adopted","v6.decision.adopted","v6.goal.closed",
                    "v624.decision.rejected","v62.module.opened","v62.module.reported","v62.module.released",
                    "v62.module.wake","v6.decision.committed","v6.claim.revised","v6.run.closed")
        events = [{"event_id":r["event_id"],"kind":r["kind"],"subject_id":r["subject_id"]}
                  for r in conn.execute("SELECT * FROM events WHERE run_id=? ORDER BY event_id", (goal["run_id"],))
                  if r["kind"] in included]
        decisions = [dict(request=json.loads(row["request_json"]),receipt=json.loads(row["receipt_json"]),
                          decision_ref=row["decision_ref"],thread=row["thread_id"],turn=row["turn_id"])
                     for row in conn.execute("SELECT * FROM v6_decisions WHERE run_id=? ORDER BY rowid", (goal["run_id"],))]
        module_reports = []
        for row in conn.execute("SELECT event_id,payload_json FROM events WHERE run_id=? AND kind='v62.module.reported' ORDER BY event_id", (goal["run_id"],)):
            payload = json.loads(row["payload_json"])
            module_reports.append(dict(event_id=row["event_id"], **{key:payload[key] for key in
                                       ("module_id","author","report_ref","report_sha256","status")}))
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?", (goal["run_id"],)).fetchone()[0]
        return dict(goal=dict(goal), official_native_starts=turns, tools=dict(kinds),
                    catalog=catalog, received_original_segments=receipts, events=events,
                    job_count=count, module_reports=module_reports, effective_decisions=decisions)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.database),ensure_ascii=False,indent=2))
