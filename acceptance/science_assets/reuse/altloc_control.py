"""Run the documented unsupported-conformer case through an ordinary Goal and a real worker."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
import time

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/"gx-h")]
from acceptance.v62a.interleavings_fixture import Actor, Scenario, branch, draft
from gx_core.p0 import v6_jobs

def receipt(work, conn, rid, job_id, input_sha):
    job=conn.execute("SELECT status,outcome_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    node=conn.execute("SELECT status FROM v6_nodes WHERE run_id=? AND job_id=?",(rid,job_id)).fetchone()
    failure=(work/"state/jobs"/job_id/"stderr.log").read_text().splitlines()[-1]
    outcome=json.loads(job[1])
    if job[0] != "FAILED" or "Ambiguous conformer" not in failure:
        raise AssertionError((job[0],failure))
    return {"transport":"controlled native callback, actual CPU Job", "run":rid, "job":job_id,
            "job_status":job[0],"node_status":node[0],"capability":"deposited-backbone-qc@1",
            "input_sha256":input_sha,"worker_stderr_last_line":failure,"exit":outcome["exit_code"],
            "actual_worker_started":outcome["started"],"wall_seconds":outcome["runner"]["wall_seconds"]}


def control(work):
    raw=HERE/"materials/1CLL.cif"
    data=json.loads((HERE/"materials/sources.json").read_text())["1CLL"]
    database=work/"state/gx.db"
    if database.exists():
        with sqlite3.connect(database.resolve().as_uri()+"?mode=ro",uri=True) as conn:
            old=conn.execute("SELECT run_id,job_id FROM jobs WHERE status='FAILED'").fetchone()
            if old:
                return receipt(work,conn,*old,data["sha256"])
    cap="deposited-backbone-qc@1"
    spec={"project":"archival-alternate-control", "evidence_policy":"gx.evidence/6.2.4",
          "research_modules":[], "research_effort":"deep", "capabilities":[cap],
          "plugins":["org.gx.deposited-structure-qc@1.0.0"], "capability_collections":[],
          "input_files":[{"role":"structure","path":str(raw),"sha256":data["sha256"]}]}
    with Scenario(work,spec_updates=spec, reopen=(work/"state/gx.db").exists()) as s:
        if s.lead is None:
            matched = s.conn.execute("""SELECT r.task_id,r.lease_id,d.scope,r.thread_id,t.turn_id FROM p0_roots r
                JOIN v6_decision_tasks d USING(task_id,run_id) JOIN p0_root_deliveries t USING(thread_id,run_id)
                WHERE d.scope='run' AND t.state='acknowledged' ORDER BY t.rowid LIMIT 1""").fetchone()
            s.lead = Actor(*matched)
            s.roots.active_turns[s.lead.thread] = s.lead.turn
        overview = {"kind":"all"}
        if "pi_method_scope" in s.brief(s.lead):
            overview["pi_method_scope"] = []
        s.call(s.lead,"gx_query_catalog",overview,"inspect")
        node={"op":"add_node", "branch":"local:qa", "node":"local:read", "capability":cap,
              "parameters":{},"inputs":[s.materials[0]],"depends_on":[],
              "question":"Can this provider measure all deposited backbone positions?"}
        opening={**branch("qa"),"budget_limits":{"job_submissions":1,"cpu_seconds":60}}
        adopted=s.call(s.lead,"gx_decide_revision",{"decision":draft(opening,node)},"corrected-measure")
        nid=adopted["aliases"]["local:read"]
        until=time.monotonic()+45
        while time.monotonic()<until:
            v6_jobs.advance(s.conn,s.run_id)
            settled=s.conn.execute("SELECT * FROM v6_nodes WHERE node_id=?", (nid,)).fetchone()
            if settled["status"] in v6_jobs.NODE_TERMINAL:
                break
            time.sleep(0.05)
        else:
            raise TimeoutError(nid)
        return receipt(work,s.conn,s.run_id,settled["job_id"],sha256(raw.read_bytes()).hexdigest())

if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("work",type=Path)
    print(json.dumps(control(cli.parse_args().work.resolve()),indent=2))
