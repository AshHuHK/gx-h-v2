"""Export public workflow evidence from an archived-structure successor without host credentials."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "gx-h"))
from gx_core.artifacts import ArtifactService
from gx_core.p0 import manifest, v62_outputs


def collect(conn, run_id):
    inputs = json.loads(conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
    trace = {"run_id": run_id, "goal": conn.execute("SELECT status FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()[0],
             "materials":[{k:f[k] for k in ("role","artifact_ref","sha256")} for f in inputs["files"]],
             "jobs":[], "release_events":[], "recipe_expansions":[], "lead_points":[], "decisions":[]}
    score_ref = None
    score_document = None
    for row in conn.execute("SELECT node_id,job_id,spec_json,status FROM v6_nodes WHERE run_id=? ORDER BY rowid", (run_id,)):
        node = json.loads(row["spec_json"])
        job = {"node_id":row["node_id"],"job_id":row["job_id"],"status":row["status"],
               "method":node["capability"],"depends_on":node["depends_on"],"recipe_decision":node.get("recipe_decision")}
        trace["jobs"].append(job)
        if row["status"] not in ("SUCCEEDED","REUSED"):
            continue
        record = manifest.read_manifest(conn,row["job_id"])
        job.update(inputs=record["admission"]["inputs"],manifest_hash=record["manifest_hash"],
                   outputs=v62_outputs.published_outputs(conn,run_id,row["job_id"]))
        for out in job["outputs"]:
            if out["output_name"] == "agreement.json":
                score_ref = out["artifact_ref"]
                score_document = ArtifactService(conn).read_json(score_ref)
                job["readout"] = {k:score_document[k] for k in ("matched_positions","mapped_positions","calpha_rmsd_A","units",
                    "input_sha256","reference_sha256","mapping_sha256")}
    trace["release_events"] = [{"event_id":n[0],"node":n[1],"type":n[2]} for n in conn.execute(
        "SELECT event_id,subject_id,kind FROM events WHERE run_id=? AND kind='v6.node.settled' ORDER BY event_id", (run_id,))]
    trace["lead_turns"] = [{"thread_id":r[0],"turn_id":r[1],"state":r[2]} for r in conn.execute(
        "SELECT DISTINCT t.thread_id,t.turn_id,t.state FROM p0_meter_native_turns t JOIN p0_roots p USING(run_id,thread_id)"
        " JOIN v6_decision_tasks d ON d.task_id=p.task_id WHERE t.run_id=? AND d.scope='run' ORDER BY t.turn_id", (run_id,))]
    for row in conn.execute("""SELECT p0.thread_id,p0.turn_id,p0.call_id,p0.result_json FROM p0_root_calls p0 JOIN p0_roots r
        ON p0.thread_id=r.thread_id JOIN v6_decision_tasks d ON d.task_id=r.task_id WHERE r.run_id=? AND d.scope='run'
        ORDER BY p0.rowid""", (run_id,)):
        reply=json.loads(row["result_json"])
        if not isinstance(reply, dict):
            continue
        call={k:row[k] for k in ("thread_id","turn_id","call_id")}
        if reply.get("recipe") and isinstance(reply.get("ops"),list):
            trace["recipe_expansions"].append(dict(call,recipe=reply["recipe"],digest=reply["digest"],ops=reply["ops"]))
        if score_ref:
            if reply == score_document:
                kind = "full_named_readout"
            elif reply.get("source_ref") == score_ref and "endpoint_rows" in reply:
                kind = "endpoint_page"
            elif reply.get("metadata", {}).get("artifact_ref") == score_ref and "content" in reply:
                kind = "artifact_content"
            else:
                continue
            trace.setdefault("readout_calls", []).append(dict(call, artifact_ref=score_ref, kind=kind))
    for row in conn.execute("""SELECT d.decision_ref,d.request_json,d.receipt_json,d.turn_id FROM v6_decisions d
        JOIN v6_decision_tasks t USING(task_id) WHERE t.scope='run' AND d.run_id=? ORDER BY d.rowid""", (run_id,)):
        dec=json.loads(row["request_json"])["decision"]
        trace["decisions"].append({"decision_ref":row["decision_ref"],"turn_id":row["turn_id"],
             "rationale":dec["rationale"],"evidence_refs":dec["evidence_refs"],"ops":[op["op"] for op in dec["ops"]],
             "outcome":json.loads(row["receipt_json"])["outcome"]})
    for t in conn.execute("""SELECT d.task_id,c.snapshot_json FROM v6_decision_tasks d JOIN research_contexts c
        USING(context_ref) WHERE d.run_id=? AND d.scope='run' ORDER BY d.rowid""",(run_id,)):
        if points:=json.loads(t[1]).get("recipe_decisions"):
            trace["lead_points"].append({"task_id":t[0], "points":points})
    return trace


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("state",type=Path)
    p.add_argument("parent")
    args=p.parse_args()
    with closing(sqlite3.connect((args.state/"gx.db").resolve().as_uri()+"?mode=ro",uri=True)) as conn:
        conn.row_factory=sqlite3.Row
        successor=conn.execute("""SELECT run_id FROM runs WHERE json_extract(inputs_json,'$.inputs.p0_spec.predecessor.parent_run_id')=?
            AND json_extract(inputs_json,'$.inputs.p0_spec.predecessor.request_message_id')='archived-partial-reference-v1'""",
            (args.parent,)).fetchone()
        out={"parent":collect(conn,args.parent), "successor":collect(conn,successor[0]) if successor else None}
        print(json.dumps(out,indent=2,ensure_ascii=False))


if __name__ == "__main__":
    main()
