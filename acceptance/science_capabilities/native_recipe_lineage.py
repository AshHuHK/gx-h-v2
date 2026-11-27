"""Read the sealed parent and restored successor, their Codex sessions and original Job publications."""
import argparse
from collections import Counter
from contextlib import closing
import json
import re
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gx-h"))
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.p0 import manifest, research_report, v62_outputs


def calls(conn, rid):
    rows = conn.execute("""SELECT n.rowid AS sequence,n.*,c.result_json FROM p0_v6_native_calls n
        JOIN p0_roots r USING(thread_id) LEFT JOIN p0_root_calls c USING(thread_id,turn_id,call_id)
        WHERE r.run_id=? ORDER BY n.rowid""", (rid,))
    for row in rows:
        request = json.loads(row["request_json"])
        yield dict(sequence=row["sequence"], tool=request["tool"], thread_id=row["thread_id"], turn_id=row["turn_id"],
                   call_id=row["call_id"], arguments=request["arguments"],
                   result=json.loads(row["result_json"]) if row["result_json"] else {})


def sessions(conn, root, rid):
    turns = conn.execute("""SELECT r.thread_id,d.turn_id,d.state FROM p0_roots r JOIN p0_root_deliveries d
        USING(thread_id,run_id) WHERE r.run_id=? AND d.method='turn/start' AND d.state='acknowledged'
        ORDER BY d.rowid""", (rid,))
    verified = []
    for thread, turn, _ in turns:
        files = list((root / "codex/sessions").rglob("*" + thread + ".jsonl"))
        metas, contexts, host_calls = [], [], []
        for path in files:
            with path.open() as stream:
                current_turn = None
                for line in stream:
                    if not line.endswith("\n"):
                        continue
                    row = json.loads(line)
                    data = row.get("payload", {})
                    if row["type"] == "session_meta" and data.get("id") == thread:
                        metas.append({k:data.get(k) for k in ("originator", "cli_version", "model_provider")})
                    if row["type"] == "turn_context":
                        current_turn = data.get("turn_id")
                        if current_turn == turn:
                            contexts.append({k:data.get(k) for k in ("model", "effort")})
                    if current_turn == turn and row["type"] == "response_item" and data.get("type") in (
                            "function_call","custom_tool_call"):
                        script = str(data.get("input","")) if data.get("name") == "exec" else data.get("name","")
                        named = sorted(set(re.findall(r"\bgx_(?:save_recipe|instantiate_recipe|select_capability|install_record_method)\b",
                                                      script)))
                        if named:
                            host_calls.append({"call_id":data.get("call_id"), "requested_tool_names":named})
        verified.append({"thread_id":thread, "turn_id":turn, "session":metas[0] if metas else None,
                         "turn_context":contexts[0] if contexts else None, "host_method_requests":host_calls})
    return verified


def collect(conn, path, rid):
    close = research_report.completed_close(conn, rid)
    assets = ArtifactService(conn)
    inventory = []
    excluded = []
    for node in conn.execute("SELECT * FROM v6_nodes WHERE run_id=? ORDER BY rowid", (rid,)):
        spec = json.loads(node["spec_json"])
        if not node["job_id"]:
            excluded.append(dict(node_id=node["node_id"], status=node["status"], capability=spec["capability"]))
            continue
        proof = manifest.read_manifest(conn, node["job_id"])
        admission, completion = proof["admission"], proof["completion"]
        outputs = v62_outputs.published_outputs(conn, rid, node["job_id"])
        record = dict(node_id=node["node_id"], decision_ref=node["decision_ref"], job_id=node["job_id"],
                      status=node["status"], method=spec["capability"], manifest_pin=admission["capability"],
                      package_pin=admission.get("goal_capability",{}).get("plugin"), inputs=admission["inputs"],
                      admission_sha256=proof["admission_hash"], completion_sha256=proof["manifest_hash"],
                      started_at=completion["started_at"], finished_at=completion["finished_at"],
                      exit_code=completion["exit_code"], outputs=outputs)
        for out in outputs:
            if out["output_name"] not in ("method-spec.json", "coverage.json"):
                continue
            value = json.loads(assets.read_bytes(out["artifact_ref"], expected_sha256=out["sha256"]))
            keep = (("method","source","fields","calibration") if out["output_name"] == "method-spec.json" else
                    ("method","numerator","denominator","covered_ids","matched_without_value","unmatched_ids",
                     "outside_cohort_ids","cohort_sha256","evidence_sha256"))
            out["verified_document_fields"] = {k:value[k] for k in keep}
        inventory.append(record)
    raw_calls = list(calls(conn,rid))
    specialized = [c for c in raw_calls if c["tool"] in ("gx_save_recipe","gx_instantiate_recipe","gx_install_record_method",
                                                       "gx_select_capability") or c["tool"] == "gx_query_catalog"
                   and c["arguments"].get("kind") == "recipe"]
    procedures = []
    for c in specialized:
        result = c["result"]
        # Do not republish entire catalogs, source text or general tool context.
        subset = {k:result[k] for k in ("reference","digest","recipe","ops","capability_version","plugin","revision")
                  if k in result}
        if "spec" in result:
            subset["selected_card"] = {k:result["spec"].get(k) for k in ("reference","digest","status")}
        if isinstance(result.get("items"), list):
            subset["returned_references"] = [i.get("reference") for i in result["items"]]
        procedures.append(dict(c, result=subset))
    ref_map = {r["artifact_ref"] for job in inventory for r in job["outputs"] if r["output_name"] == "coverage.json"}
    received = []
    for e in conn.execute("SELECT event_id,payload_json FROM events WHERE run_id=? AND kind='v623.endpoints.received' ORDER BY event_id",(rid,)):
        data = json.loads(e["payload_json"])
        rows = [r for r in data["endpoints"] if r["source_ref"] in ref_map]
        if rows:
            received.append(dict(event_id=e["event_id"], delivery=data["delivery_kind"], turn_id=data["turn_id"],
                                 receipt=data["receipt"], endpoints=rows))
    adopted = conn.execute("SELECT request_json,receipt_json,thread_id,turn_id FROM v6_decisions WHERE decision_ref=?",
                           (close["decision_ref"],)).fetchone()
    request, receipt = json.loads(adopted[0])["decision"], json.loads(adopted[1])
    metric_refs = receipt.get("endpoint_use",{}).get("used_metric_refs",[])
    turn = adopted["turn_id"]
    used = {p["metric_ref"] for e in received if e["turn_id"] == turn for p in e["endpoints"] if "metric_ref" in p}
    refused = [c["result"]["decision_refusal"] for c in raw_calls if "decision_refusal" in c["result"]]
    return dict(run_id=rid, official_turns=sessions(conn,path,rid), jobs=inventory, nodes_without_job=excluded,
                research_tool_calls=procedures, received=received, adopted_close=dict(decision_ref=close["decision_ref"],
                event_id=close["closed_event"], thread_id=adopted["thread_id"], turn_id=turn,
                cited_coverage_outputs=sorted(ref_map.intersection(request["evidence_refs"])),
                used_delivered_metrics=sorted(used.intersection(metric_refs)), rationale=request["rationale"],
                operation=close["operation"]), decision_refusals=dict(Counter(r["code"] for r in refused)))


def matching_steps(conn, rid, call):
    actual = conn.execute("""SELECT n.spec_json,n.job_id,n.status,d.receipt_json,c.rowid AS sequence
        FROM v6_nodes n JOIN v6_decisions d USING(decision_ref) JOIN p0_v6_native_calls c
        USING(thread_id,turn_id,call_id) WHERE n.run_id=? ORDER BY n.rowid""", (rid,)).fetchall()
    matched = []
    for expected in call["result"].get("ops",[]):
        if expected["op"] != "add_node":
            continue
        for row in actual:
            spec, aliases = json.loads(row["spec_json"]),json.loads(row["receipt_json"])["aliases"]
            ref = expected["node"]
            if row["sequence"] <= call["sequence"] or aliases.get(ref) != spec["node"]:
                continue
            same = all(spec[k] == expected[k] for k in ("capability","inputs","parameters","question"))
            deps = [aliases.get(d,d) for d in expected["depends_on"]]
            branch = aliases.get(expected["branch"], expected["branch"])
            matched.append(dict(recipe_node=ref, node_id=spec["node"], job_id=row["job_id"], status=row["status"],
                                same_method_inputs_parameters_question_dependencies=same and deps == spec["depends_on"],
                                same_branch_binding=branch == spec["branch"], branch=spec["branch"]))
    return matched


def verify(parent_path, child_path, rid):
    def connect(root):
        db = sqlite3.connect((root / "state/gx.db").resolve().as_uri()+"?mode=ro",uri=True)
        db.row_factory = sqlite3.Row
        return db
    with closing(connect(parent_path)) as parent_db, closing(connect(child_path)) as child_db:
        event = child_db.execute("SELECT event_id,payload_json FROM events WHERE run_id=? AND kind='v6.research.successor_opened'",
                                 (rid,)).fetchone()
        lineage = json.loads(event["payload_json"])
        parent = collect(parent_db,parent_path,lineage["parent_run_id"])
        child = collect(child_db,child_path,rid)
        if lineage["parent_decision_ref"] != parent["adopted_close"]["decision_ref"]:
            raise ValueError("The parent archive does not match the successor origin")
        registered = []
        for r in parent_db.execute("SELECT * FROM v6_saved_recipes WHERE source_run=?",(parent["run_id"],)):
            definition = json.loads(r["definition_json"])
            if ids.sha256_of_obj(definition) != r["digest"]:
                raise ValueError("Parent recipe content digest mismatch")
            copy = child_db.execute("SELECT * FROM v6_saved_recipes WHERE reference=?", (r["reference"],)).fetchone()
            if not copy or tuple(r) != tuple(copy):
                raise ValueError("Restored recipe definition or source Jobs differ")
            sources = json.loads(r["sources_json"])
            jobs = {j["job_id"]:j["completion_sha256"] for j in parent["jobs"]}
            if any(jobs.get(s["job_id"]) != s["completion_sha256"] for s in sources.values()):
                raise ValueError("Recipe source Job digest mismatch")
            registered.append(dict(reference=r["reference"],digest=r["digest"],source_jobs=sources,
                                   step_method_pins={s["slot"]:s["method_digest"] for s in definition["steps"]}))
        expansions = [c for c in child["research_tool_calls"] if c["tool"]=="gx_instantiate_recipe"]
        expansion_proof = [dict(call_id=c["call_id"], exact_parent_recipe=any(
            c["result"].get("recipe")==r["reference"] and c["result"].get("digest")==r["digest"] for r in registered),
            expanded_steps=len(c["result"].get("ops",[])), adopted=matching_steps(child_db,rid,c)) for c in expansions]
        methods = []
        child_by_method = {j["method"]:j for j in child["jobs"]}
        for first in parent["jobs"]:
            if not (second := child_by_method.get(first["method"])):
                continue
            left = {p["role"]:p["sha256"] for p in first["inputs"]}
            right = {p["role"]:p["sha256"] for p in second["inputs"]}
            methods.append({"method":first["method"],"parent_job":first["job_id"],"successor_job":second["job_id"],
                            "distinct_successful_jobs":first["job_id"] != second["job_id"] and
                                 first["status"] == second["status"] == "SUCCEEDED",
                            "same_method_manifest":first["manifest_pin"] == second["manifest_pin"],
                            "same_provider": first["package_pin"] == second["package_pin"] if first["package_pin"] else None,
                            "different_input_roles":[k for k in sorted(set(left)|set(right)) if left.get(k) != right.get(k)]})
        return dict(method_execution=methods,parent=parent,successor=child,successor_origin=dict(event_id=event["event_id"],**lineage),
                    restored_recipes=registered,expansion_to_adoption=expansion_proof)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("parent",type=Path)
    p.add_argument("successor",type=Path)
    p.add_argument("successor_run")
    args = p.parse_args()
    print(json.dumps(verify(args.parent,args.successor,args.successor_run),indent=2,ensure_ascii=False))
