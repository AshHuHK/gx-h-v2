"""Read deposited-molecule study receipts from the original GX store without starting a Host."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT/"gx-h")]
from acceptance.science_plugin_blueprint.recipes.trace import collect as recipe_trace
from gx_core.artifacts import ArtifactService

PUBLIC_METRICS = (
    "entry", "chain", "model", "model_count", "input_sha256", "numbering_sha256", "mapped_models",
    "declared_closure", "model_1_distance_A", "declared_residues", "observed_residues", "complete_backbones",
    "declared_positions", "observed_positions", "peptide_residue_positions", "backbone_positions",
    "declared_modified_positions", "annotation_status", "non_peptide_components", "unknown_chemistry_positions",
    "mapped_regions", "mapped_positions", "matched_positions", "numbering_method", "numbering_version",
    "scheme", "predicted_confidence",
)


def collect(state, run):
    with closing(sqlite3.connect((state / "gx.db").resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        trace = recipe_trace(db, run)
        assets = ArtifactService(db)
        for job in trace["jobs"]:
            original = db.execute("SELECT decision_ref,spec_json FROM v6_nodes WHERE run_id=? AND node_id=?",
                                  (run,job["node_id"])).fetchone()
            node = json.loads(original["spec_json"])
            job["adopted_by"] = original["decision_ref"]
            job["selected_inputs"] = node["inputs"]
            job["parameters"] = node["parameters"]
            job["measurements"] = []
            for output in job.get("outputs", []):
                if not output["output_name"].endswith(".json"):
                    continue
                ref = output["artifact_ref"]
                document = assets.read_json(ref)
                reading = {key: document[key] for key in PUBLIC_METRICS if key in document}
                if "chains" in document:
                    reading["chains"] = document["chains"]
                job["measurements"].append({"artifact_ref": ref, "summary": reading})
        trace["endpoint_deliveries"] = []
        trace["catalog"] = []
        for eid, kind, actor, encoded in db.execute(
                "SELECT event_id,kind,actor,payload_json FROM events WHERE run_id=? ORDER BY event_id", (run,)):
            payload = json.loads(encoded)
            if kind == "v623.endpoints.received":
                trace["endpoint_deliveries"].append({"event_id":eid, "task_id":payload["task_id"],
                    "thread_id":payload["thread_id"], "delivery_kind":payload["delivery_kind"],
                    "sources":sorted({item["source_ref"] for item in payload["endpoints"]})})
            elif kind == "research.catalog.surveyed":
                trace["catalog"].append({"event_id":eid, "kind":payload["arguments"].get("kind"),
                    "query":payload["arguments"].get("query"), "total":payload["total"],
                    "first_candidates":payload.get("first_candidates",[])})
            elif kind == "v6.run.closed":
                trace["closure_event"] = eid
        trace["adopted_decision_events"] = [dict(r) for r in db.execute(
            "SELECT event_id,subject_id FROM events WHERE run_id=? AND kind='v6.decision.committed' ORDER BY event_id", (run,))]
        trace["host"] = "Existing official Codex OAuth through Clash; admitted native turn IDs, including modules."
        trace["native_turns"] = [dict(r) for r in db.execute("""SELECT DISTINCT p.thread_id,d.turn_id,t.scope
                FROM p0_root_deliveries d JOIN p0_roots p USING(run_id,thread_id)
                JOIN v6_decision_tasks t ON t.task_id=p.task_id WHERE d.run_id=? AND d.method='turn/start'
                AND d.state='acknowledged' AND d.turn_id IS NOT NULL ORDER BY d.turn_id""", (run,))]
        trace["recipes_published_from_run"] = [dict(r) for r in db.execute(
            "SELECT reference,digest FROM v6_saved_recipes WHERE source_run=?", (run,))]
        trace["native_recipe_calls"] = []
        for row in db.execute("""SELECT p.thread_id,p.call_id,p.turn_id,n.request_json,p.result_json
               FROM p0_root_calls p JOIN p0_v6_native_calls n USING(thread_id,call_id,turn_id)
               JOIN p0_roots r USING(thread_id) WHERE r.run_id=?
               AND json_extract(n.request_json,'$.tool') IN ('gx_save_recipe','gx_instantiate_recipe')
               ORDER BY p.rowid""", (run,)):
            trace["native_recipe_calls"].append({
                "thread_id":row["thread_id"],"call_id":row["call_id"],"turn_id":row["turn_id"],
                "request":json.loads(row["request_json"]), "result":json.loads(row["result_json"])
            })
        return trace


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path)
    parser.add_argument("run")
    args = parser.parse_args()
    print(json.dumps(collect(args.state, args.run), ensure_ascii=False, indent=2))
