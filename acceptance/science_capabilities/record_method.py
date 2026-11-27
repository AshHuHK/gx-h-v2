"""Read-only evidence report and ordinary successor configuration for the substrate coverage studies."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gx-h"))
from gx_core.artifacts import ArtifactService
from gx_core.p0 import manifest, v62_outputs
from gx_core.plugins import goal

EXAMPLE = ROOT / "examples/record-method-authoring"


def report(conn, run_id):
    assets = ArtifactService(conn)
    jobs = []
    for node in conn.execute("SELECT node_id,spec_json,status,job_id FROM v6_nodes WHERE run_id=? ORDER BY rowid",
                             (run_id,)):
        method = json.loads(node["spec_json"])["capability"]
        if method != "derive-record-coverage@1" and not method.startswith("record-coverage-"):
            continue
        job = {"node_id": node["node_id"], "method": method, "status": node["status"], "job_id": node["job_id"]}
        if node["job_id"]:
            proof = manifest.read_manifest(conn, node["job_id"], missing_ok=True)
            if proof:
                job.update(method_pin=proof["admission"]["capability"], inputs=proof["admission"]["inputs"],
                           outputs=v62_outputs.published_outputs(conn, run_id, node["job_id"]))
                for output in job["outputs"]:
                    if output["output_name"] in ("coverage.json", "method-spec.json"):
                        output["document"] = json.loads(assets.read_bytes(output["artifact_ref"],
                                                                         expected_sha256=output["sha256"]))
        jobs.append(job)
    output_refs = {row["artifact_ref"] for job in jobs if job["method"].startswith("record-coverage-")
                   for row in job.get("outputs", [])}
    decisions = []
    for row in conn.execute("""SELECT d.decision_ref,d.thread_id,d.turn_id,d.request_json,d.receipt_json
        FROM v6_decisions d JOIN v6_decision_tasks t USING(task_id)
        WHERE d.run_id=? AND t.scope='run' ORDER BY d.rowid""", (run_id,)):
        decision = json.loads(row["request_json"])["decision"]
        cited = sorted(output_refs.intersection(decision["evidence_refs"]))
        if cited:
            decisions.append({"decision_ref": row["decision_ref"], "turn_id": row["turn_id"], "cites": cited,
                              "thread_id": row["thread_id"], "rationale": decision["rationale"],
                              "received_metric_refs": json.loads(row["receipt_json"]).get("endpoint_use", {}).get("received_metric_refs", [])})
    recipes = [dict(row) for row in conn.execute(
        "SELECT reference,digest FROM v6_saved_recipes WHERE source_run=?", (run_id,))]
    return {"run_id": run_id, "jobs": jobs, "saved_recipes": recipes, "lead_decisions_citing_coverage": decisions}


def followup(conn, run_id):
    trace = report(conn, run_id)
    active = {row["capability_version"] for row in goal.catalog(conn, run_id)}
    choices = sorted({job["method"] for job in trace["jobs"] if job.get("outputs") and job["method"] in active
                      and job["method"].startswith("record-coverage-")})
    if not choices:
        raise ValueError("The parent has no successful derived coverage method in its active view")
    folder = EXAMPLE / "sucrose"
    grant = json.loads((folder / "goal.json").read_bytes())
    files = [{**item, "path": str((folder / item["path"]).resolve())} for item in grant["input_files"]]
    return {"parent_method_options": choices, "research_effort": "deep", "input_files": files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["report", "followup"])
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    with closing(sqlite3.connect((args.state / "gx.db").resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        result = (report if args.action == "report" else followup)(conn, args.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
