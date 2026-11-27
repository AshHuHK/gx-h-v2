"""Match ordinary research, immutable CPU outputs and acknowledged official model turns."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

PRODUCT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(PRODUCT), str(PRODUCT / "gx-h")]
from acceptance.science_plugin_blueprint.run_metrics import metrics
from gx_core.artifacts import ArtifactService
from gx_core.p0 import manifest, v62_outputs


def official_turns(work):
    turns = {}
    for path in (work / "host/codex/sessions").rglob("*.jsonl"):
        thread = None
        for line in path.open():
            record = json.loads(line)
            payload = record.get("payload", {})
            if record["type"] == "session_meta":
                thread = payload["id"]
                provider = payload.get("model_provider")
            elif thread and record["type"] == "turn_context":
                key = (thread, payload["turn_id"])
                turns[key] = {"model": payload["model"], "effort": payload.get("effort"), "provider": provider}
    return turns


def coverage(document):
    return {key: value for key, value in document.items() if key not in ("examples", "scope")}


def visible_records(document):
    return {key: document[key] for key in ("query", "releases", "source_digests", "counts")} | {
        "assay_coverage": coverage(document["assay_coverage"]),
        "parents": [{k: row[k] for k in ("accession", "sequence_version", "sequence_sha256")}
                    for row in document["parents"]]
    }


def readout(document):
    if document["schema"] == "gx.yeast-archive-parents/1":
        return visible_records(document)
    if document["schema"] == "gx.yeast-archive-comparison/1":
        return {key: document[key] for key in ("prior_query", "new_query", "prior_counts", "prior_parents",
                                              "shared_accessions", "new_only_accessions")} | {
            "prior_assays": coverage(document["prior_assays"]), "current": visible_records(document["current"])}
    raise ValueError("Unexpected archive publication type")


def one_run(conn, work, run, host_turns):
    rid = run["run_id"]
    store = ArtifactService(conn)
    turns = []
    for row in conn.execute("""SELECT DISTINCT thread_id,turn_id FROM p0_root_deliveries WHERE run_id=?
        AND state='acknowledged' AND method='turn/start' AND turn_id IS NOT NULL ORDER BY rowid""", (rid,)):
        turns.append(dict(row) | {"official_session": host_turns.get(tuple(row))})
    spec = json.loads(run["inputs_json"])
    files = [{k: f[k] for k in ("artifact_ref", "role", "sha256")} for f in spec["files"]]
    evidence = {"run_id": rid, "goal_status": run["goal_status"], "engine_status": run["status"],
                "created_at":run["created_at"], "native_contract_sha256":run["contract_sha256"],
                "archive_provider_options":[item for item in spec["inputs"].get("method_scope", {}).get("capability_options", ())
                                            if item.get("provider")=="org.gx.yeast-enzyme-archive@1.0.0"],
                "inputs": files, "turns": turns, "jobs": [], "native_actions": [], "decisions": [],
                "release_events": [], "surveys": [], "recipe_publications": [], "transfers": [],
                "decision_refusals": []}
    jobs = conn.execute("SELECT job_id,capability_id,capability_version,status,created_at,finished_at FROM jobs WHERE run_id=? ORDER BY created_at,job_id", (rid,))
    qualified = []
    for job in jobs:
        rec = dict(job)
        if job["status"] == "SUCCEEDED":
            m = manifest.read_manifest(conn, job["job_id"])
            rec["inputs"] = m["admission"]["inputs"]
            rec["manifest_hash"] = m["manifest_hash"]
            outs = v62_outputs.published_outputs(conn, rid, job["job_id"])
            rec["outputs"] = outs
            for f in outs:
                if f["output_name"] in ("parent-records.json", "comparison.json"):
                    if job["job_id"] not in qualified:
                        qualified.append(job["job_id"])
                    document = json.loads(store.read_bytes(f["artifact_ref"], expected_sha256=f["sha256"]))
                    rec.setdefault("readouts", {})[f["output_name"]] = readout(document)
        evidence["jobs"].append(rec)
    visible_pins = {item["artifact_ref"]: item["sha256"] for item in files}
    for job in evidence["jobs"]:
        visible_pins.update({item["artifact_ref"]: item["sha256"] for item in job.get("outputs", ())})
    read_documents = {}
    tool_counts = Counter()
    calls = conn.execute("""SELECT n.rowid AS native_row,n.call_id,n.turn_id,n.thread_id,n.request_json,c.result_json,t.scope
        FROM p0_v6_native_calls n JOIN p0_root_calls c USING(thread_id,turn_id,call_id)
        JOIN p0_roots r USING(thread_id) JOIN v6_decision_tasks t ON t.task_id=r.task_id
        WHERE r.run_id=? ORDER BY n.rowid""", (rid,))
    for call in calls:
        req = json.loads(call["request_json"])
        result = json.loads(call["result_json"])
        tool = req["tool"]
        tool_counts[tool] += 1
        if tool == "gx_decide_revision" and isinstance(result, dict) and "decision_refusal" in result:
            refusal = result["decision_refusal"]
            evidence["decision_refusals"].append({
                "call_id": call["call_id"], "turn_id": call["turn_id"], "native_row": call["native_row"],
                **{key: refusal[key] for key in ("code", "message", "side_effect_status") if key in refusal}})
        if tool not in {"gx_read_artifact", "gx_query_catalog", "gx_save_recipe", "gx_instantiate_recipe", "gx_select_capability"}:
            continue
        detail = {"arguments": req["arguments"]}
        if isinstance(result, dict):
            detail.update({k:result[k] for k in ("kind", "reference", "recipe", "digest", "total", "error", "code", "source_ref") if k in result})
            if tool == "gx_instantiate_recipe" and "ops" in result:
                detail["expanded_steps"] = result["ops"]
                detail["decision_point"] = result["decision_point"]
            if tool == "gx_query_catalog" and "items" in result:
                detail["candidates"] = [i["reference"] for i in result["items"] if i.get("reference")]
            if tool == "gx_read_artifact":
                detail["receipt"] = {k:result["metadata"][k] for k in ("artifact_ref", "sha256","kind") if k in result.get("metadata", {})}
                ref = req["arguments"].get("ref")
                if ref in visible_pins and ref not in read_documents:
                    raw = store.read_bytes(ref, expected_sha256=visible_pins[ref])
                    try:
                        read_documents[ref] = json.loads(raw)
                    except (ValueError, UnicodeError):
                        read_documents[ref] = None
                full_document = result == read_documents.get(ref)
                detail["content_returned"] = full_document or "content" in result
                if full_document:
                    detail["verified_json_sha256"] = visible_pins[ref]
                if "endpoint_rows" in result:
                    detail["endpoint_count"] = len(result["endpoint_rows"])
        evidence["native_actions"].append({k:call[k] for k in ("native_row", "call_id", "turn_id", "thread_id", "scope")} | {"tool":tool} | detail)
    evidence["tool_counts"] = dict(tool_counts)
    for row in conn.execute("SELECT event_id,kind,subject_id,payload_json FROM events WHERE run_id=? ORDER BY event_id",(rid,)):
        k = row["kind"]
        category = {"v6.node.settled":"release_events", "research.catalog.surveyed":"surveys","v6.research.successor_opened":"transfers"}.get(k)
        if category:
            evidence[category].append({"id":row["event_id"], "subject":row["subject_id"], "payload":json.loads(row["payload_json"])})
        if k == "v6.run.closed":
            evidence["close_event"] = row["event_id"]
    for row in conn.execute("""SELECT d.decision_ref,d.request_json,d.receipt_json,d.turn_id,n.rowid AS native_row
        FROM v6_decisions d LEFT JOIN p0_v6_native_calls n USING(call_id,turn_id,thread_id)
        WHERE d.run_id=? ORDER BY d.rowid""",(rid,)):
        decision = json.loads(row["request_json"])["decision"]
        receipt = json.loads(row["receipt_json"])
        evidence["decisions"].append({"event_id":receipt["event_id"], "ref":row["decision_ref"], "native_row":row["native_row"], "turn_id":row["turn_id"],"rationale":decision["rationale"],
                                        "ops":[i["op"] for i in decision["ops"]], "evidence_refs":decision["evidence_refs"],
                                        "outcome":receipt["outcome"]})
    evidence["nodes"] = [
        {"node_id": n["node_id"], "decision_ref": n["decision_ref"], "job_id": n["job_id"],
         "status": n["status"], "spec": json.loads(n["spec_json"])}
        for n in conn.execute("SELECT * FROM v6_nodes WHERE run_id=? ORDER BY node_id", (rid,))]
    evidence["recipe_publications"] = [
        {"reference":r["reference"], "digest":r["digest"], "source_run":rid,
         "definition":json.loads(r["definition_json"]), "sources":json.loads(r["sources_json"])}
        for r in conn.execute("SELECT * FROM v6_saved_recipes WHERE source_run=? ORDER BY reference",(rid,))]
    evidence["metrics"] = metrics(work / "state/gx.db", rid, qualified)
    return evidence


def collect(work):
    db = work / "state/gx.db"
    public_turns = official_turns(work)
    with closing(sqlite3.connect(db.resolve().as_uri()+"?mode=ro",uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        return [one_run(conn, work, r, public_turns) for r in conn.execute(
            "SELECT r.*,g.status AS goal_status FROM runs r JOIN v6_goals g USING(run_id) ORDER BY r.created_at")]


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("workspace", type=Path)
    args = cli.parse_args()
    print(json.dumps(collect(args.workspace.resolve()),ensure_ascii=False,indent=2))
