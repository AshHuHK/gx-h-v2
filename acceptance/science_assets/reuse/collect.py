"""Export the ordinary GX results and compare two native archive-QC runs to independent PDB readings."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.science_assets.reuse.independent import measure
from acceptance.science_plugin_blueprint.recipes.trace import collect as recipe_trace
from gx_core.artifacts import ArtifactService
from gx_core.p0 import research_routes
from gx_core.p0 import manifest


def case(conn, state, rid, archive):
    record = recipe_trace(conn, rid)
    assets = ArtifactService(conn)
    deployment=conn.execute("SELECT contract_sha256,workflow_sha256,agent_policy_sha256,created_at,host_json,"
                            "inputs_json FROM runs WHERE run_id=?", (rid,)).fetchone()
    record["deployment"]={k:deployment[k] for k in ("created_at","contract_sha256","workflow_sha256","agent_policy_sha256")}
    configured=json.loads(deployment["inputs_json"])
    record["initially_active_qc_provider"]=[item for item in configured["inputs"].get("capability_catalog", [])
                                    if item.get("capability_version")=="deposited-backbone-qc@1"]
    record["goal_pins"]=[v for v in configured["inputs"].get("method_scope",{}).get("capability_options",[])
                         if v.get("method")=="deposited-backbone-qc@1"]
    outputs = {}
    pdb = HERE / "materials" / (archive + ".pdb")
    independent = measure(pdb, "A")
    for job in record["jobs"]:
        if job.get("status") not in ("SUCCEEDED", "REUSED"):
            continue
        job["provider"] = manifest.read_manifest(conn, job["job_id"])["admission"]["capability"]
        for named in job["outputs"]:
            if named["output_name"] != "qc.json":
                continue
            ref = named["artifact_ref"]
            reading = assets.read_json(ref)
            difference = abs(reading["ca_radius_of_gyration_A"] - independent["radius_of_gyration_A"])
            counts = {"denominator":"declared_residues", "observed":"observed_residues",
                      "c_alpha":"ca_count", "complete_n_ca_c_o":"complete_backbones"}
            job["independent_pdb"] = independent
            job["comparison"] = {"counts_identical": all(independent[k] == reading[v] for k,v in counts.items()),
                                 "abs_difference_A": difference, "tolerance_A": 0.0001}
            job["readout"] = reading
            outputs[ref] = reading
    record["catalog"], record["save_calls"], record["reads"] = [], [], []
    query = """SELECT t.scope,c.turn_id,c.call_id,n.request_json,c.result_json
        FROM p0_root_calls c JOIN p0_v6_native_calls n USING(thread_id,turn_id,call_id)
        JOIN p0_roots r USING(thread_id) JOIN v6_decision_tasks t ON r.task_id=t.task_id
        WHERE r.run_id=? ORDER BY c.rowid"""
    record["researcher_extracts"]=[]
    for row in conn.execute(query, (rid,)):
        req, result = json.loads(row["request_json"]), json.loads(row["result_json"])
        tool, args = req.get("tool"), req.get("arguments", {})
        if (row["scope"] != "run" and tool == "gx_extract_evidence" and isinstance(result,dict)
                and result.get("accepted") and result.get("evidence_ref")):
            record["researcher_extracts"].append({"scope":row["scope"],"turn":row["turn_id"],
                "call":row["call_id"],"evidence_ref":result["evidence_ref"],"source_ref":args.get("source_ref")})
        if row["scope"] != "run" or not isinstance(result,dict):
            continue
        action = {"turn":row["turn_id"], "call":row["call_id"]}
        if tool == "gx_query_catalog":
            record["catalog"].append(dict(action, query=args, selected=result.get("reference"),
                references=[i.get("reference") for i in result.get("items",[])[:5]],
                total=result.get("total"), survey_event=result.get("survey_event")))
        elif tool == "gx_save_recipe":
            record["save_calls"].append(dict(action, args=args, returned=result))
        elif tool == "gx_read_artifact":
            for ref in outputs:
                if ref in args.values() and (result == outputs[ref] or
                        result.get("metadata",{}).get("artifact_ref") == ref and "content" in result):
                    record["reads"].append(dict(action, artifact_ref=ref, tool=tool))
    for extract in record["researcher_extracts"]:
        ref=extract["evidence_ref"]
        extract["cited_by_run_decisions"]=[d["decision_ref"] for d in record["decisions"] if ref in d["evidence_refs"]]
    record["signed_reports"] = []
    for (ref,) in conn.execute("SELECT report_ref FROM v62_module_reports WHERE run_id=? ORDER BY rowid", (rid,)):
        document=assets.read_json(ref)
        record["signed_reports"].append({"report_ref":ref, **{k:document.get(k) for k in
            ("author","scope","status","findings","qualitative_notes","gaps","next_steps")}})
    route=research_routes.routes(conn,rid,state)
    record["lead_endpoint_receipts"] = []
    for row in conn.execute("""SELECT e.event_id,e.payload_json FROM events e
            JOIN v6_decision_tasks t ON t.task_id=json_extract(e.payload_json,'$.task_id') AND t.run_id=e.run_id
            WHERE e.run_id=? AND t.scope='run' AND e.kind='v623.endpoints.received' ORDER BY e.event_id""", (rid,)):
        delivery = json.loads(row["payload_json"])
        endpoints = [item for item in delivery["endpoints"] if item.get("source_ref") in outputs]
        if endpoints:
            record["lead_endpoint_receipts"].append({"event_id": row["event_id"], "turn": delivery["turn_id"],
                                                     "endpoints": endpoints})
    record["handoffs"]=route["handoffs"]
    record["native_worker_overlaps"]=route["real_turn_overlaps"]
    record["turns"]=[]
    for root in conn.execute("""SELECT r.*,t.scope FROM p0_roots r
                  JOIN v6_decision_tasks t USING(task_id,run_id) WHERE r.run_id=?""", (rid,)):
        for turn in research_routes._turns(conn,rid,state,root):
            record["turns"].append({"scope":root["scope"], **{k:turn.get(k) for k in
                ("thread_id","turn_id","provider","model","classification","start","end","terminal_status")}})
    record["job_running_intervals"]=[]
    record["job_turn_overlap_intervals"]=[]
    for job in conn.execute("SELECT job_id,finished_at FROM jobs WHERE run_id=?", (rid,)):
        start=conn.execute("""SELECT MIN(ts) FROM events WHERE run_id=? AND subject_id=?
                          AND kind='job.transition' AND new_state='RUNNING'""", (rid,job[0])).fetchone()[0]
        if not start or not job["finished_at"]:
            continue
        interval={"start":start,"end":job["finished_at"],"job_id":job["job_id"]}
        record["job_running_intervals"].append(interval)
        for turn in record["turns"]:
            if turn["classification"] != "NATIVE_MODEL":
                continue
            overlap=research_routes._interval(interval,turn)
            if overlap:
                record["job_turn_overlap_intervals"].append({"job_id":job["job_id"],"turn_id":turn["turn_id"],
                                                       "scope":turn["scope"],"seconds":overlap})
    record["closing"]= [dict(row) for row in conn.execute("""SELECT event_id,ts,kind,subject_id FROM events
                WHERE run_id=? AND kind IN ('v6.run.closed','v6.decision.committed') ORDER BY event_id""", (rid,))]
    return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("parent")
    args=parser.parse_args()
    state=args.state.resolve()
    with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro",uri=True)) as conn:
        conn.row_factory=sqlite3.Row
        conn.execute("BEGIN")
        child=conn.execute("""SELECT run_id FROM runs WHERE
            json_extract(inputs_json,'$.inputs.p0_spec.predecessor.parent_run_id')=? AND
            json_extract(inputs_json,'$.inputs.p0_spec.predecessor.request_message_id')='archive-qc-next-independent-deposition-1'""",
            (args.parent,)).fetchone()
        payload={"parent":case(conn,state,args.parent,"1UBQ"),
                 "successor":case(conn,state,child[0],"1MBN") if child else None}
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
