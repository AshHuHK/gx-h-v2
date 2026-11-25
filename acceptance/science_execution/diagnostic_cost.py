"""Describe original per-role activity without counting a response twice or inferring unknown charges."""
from collections import Counter, defaultdict
from contextlib import closing
import json

from acceptance.v62c import rehearse


def summarize(slot, rid, account, *, fixture):
    with closing(rehearse.db_open(slot)) as db:
        scopes={r[0]:r[1] for r in db.execute("""SELECT r.thread_id,t.scope FROM p0_roots r
            JOIN v6_decision_tasks t USING(run_id,task_id) WHERE r.run_id=? AND r.thread_id IS NOT NULL""",(rid,))}
        calls=defaultdict(Counter)
        for r in db.execute("""SELECT r.thread_id,json_extract(n.request_json,'$.tool') AS tool
                FROM p0_v6_native_calls n JOIN p0_roots r USING(thread_id)
                JOIN p0_root_calls c USING(thread_id,turn_id,call_id)
                WHERE r.run_id=? AND json_type(c.result_json,'$.error') IS NULL""",(rid,)):
            calls[scopes[r["thread_id"]]][r["tool"]]+=1
        jobs=defaultdict(dict)
        for r in db.execute("""SELECT j.job_id,j.status,j.measured_json,t.scope FROM jobs j
                LEFT JOIN v6_nodes n ON j.run_id=n.run_id AND j.job_id=n.job_id
                LEFT JOIN v6_decisions d ON d.run_id=n.run_id AND d.decision_ref=n.decision_ref
                LEFT JOIN v6_decision_tasks t ON t.run_id=d.run_id AND t.task_id=d.task_id
                WHERE j.run_id=? ORDER BY j.rowid,n.rowid""",(rid,)):
            # Execution reuse may reference one physical Job from multiple graph nodes; attribute its first owner.
            if any(r["job_id"] in group for group in jobs.values()):
                continue
            measured=json.loads(r["measured_json"] or "{}")
            jobs[r["scope"] or "unattributed"][r["job_id"]]={"job_id":r["job_id"],"status":r["status"],
                         "cpu_seconds":measured.get("cpu_seconds"),"wall_seconds":measured.get("wall_seconds")}
    def empty():
        return {"native_turns":[],"physical_requests":0 if fixture else None,"identified_responses":0,
                "confirmed_tokens":None,"identified_response_tokens":None,"data_tool_calls":{},"jobs":[]}
    rows=defaultdict(empty)
    def add(row,key,amount):
        if amount is not None:
            row[key]=(row[key] or 0)+amount
    if fixture:
        for event in account["requests"]:
            row=rows[event["scope"]]
            turn=[event["thread_id"],event["turn_id"]]
            if turn not in row["native_turns"]:
                row["native_turns"].append(turn)
            row["physical_requests"]+=1
            if event["state"]=="SETTLED":
                row["identified_responses"]+=1
                add(row,"confirmed_tokens",event["actual"])
                add(row,"identified_response_tokens",event["actual"])
    else:
        for record in account["turns"]:
            group=rows[scopes.get(record["thread"],"unattributed")]
            # A reconciled root charge already includes verified descendants.
            add(group,"confirmed_tokens",record["tokens"])
            for turn in (record,*record["descendants"]):
                group["native_turns"].append([turn["thread"],turn["turn"]])
                group["identified_responses"]+=turn["covered_response_count"]
                add(group,"identified_response_tokens",turn["covered_response_tokens"])
    for scope in set(scopes.values()) | set(jobs):
        row=rows[scope]
        row["data_tool_calls"]={k:v for k,v in sorted(calls[scope].items()) if k in
               {"gx_read_artifact","gx_query_catalog","gx_read_brief","gx_query_evidence","gx_read_measurements"}}
        row["completed_tool_calls"]=dict(sorted(calls[scope].items()))
        row["jobs"]=list(jobs[scope].values())
    return {"fixture_only":fixture,"accounting_status":account["status"],"total_tokens":account["total_tokens"],
            "provider_usd":None,"scope":"original native receipts including descendants, each CPU Job counted once",
            "additional_currency_authorized":0,"by_scope":dict(sorted(rows.items()))}
