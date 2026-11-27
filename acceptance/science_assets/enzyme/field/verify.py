"""Independently check the A → C handoff and compare both readouts to the SQL audit."""
import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def field_checks(studies):
    reference = json.loads((HERE.parent/"sources/reference.json").read_text())["cases"]
    a, c = studies
    outputs = [(job, output) for job in a["jobs"] for output in job.get("outputs", ())
               if output["output_name"] == "parent-records.json"]
    compare = [(job, output) for job in c["jobs"] for output in job.get("outputs", ())
               if output["output_name"] == "comparison.json"]
    findings = {}
    for name, case in reference.items():
        if name == "maltose" and outputs:
            evidence = outputs[0][0]["readouts"]["parent-records.json"]
        elif name == "isomaltose" and compare:
            evidence = compare[0][0]["readouts"]["comparison.json"]["current"]
        else:
            findings[name] = {"readout": "unavailable"}
            continue
        parents = {p["accession"]: (p["sequence_version"], p["sequence_sha256"]) for p in evidence["parents"]}
        expected = {k: (v["sequence_version"], v["sequence_sha256"]) for k,v in case["ec_taxon_members"].items()}
        assays = case["assays"]
        counts = evidence["counts"]
        covered = evidence["assay_coverage"]
        findings[name] = {
            "source_and_version_matched_parents": parents == expected,
            "scope": evidence["query"]["taxid"],
            "exact_and_ec_counts_match": (
                counts["exact_accessions"] == len(case["raw_rhea"]["pool_hits"]) and
                counts["class_accessions"] == len(expected) and
                counts["distinct_accessions"] == len(expected)),
            "unique_assay_count_matches": sum(covered["linked_unique_rows_by_source"].values()) == assays["distinct_rows_any_member"],
            "catalytic_substrate_count_matches": covered["target_substrate_rows"] == sum(assays["brenda_catalytic_rows_with_exact_substrate"].values())
        }
    transfers = [t for e in c["transfers"] for t in e["payload"].get("material_transfers", ())]
    received = None
    if len(outputs)==1 and len(compare)==1:
        parent, pub = outputs[0]
        consumer, _ = compare[0]
        for t in transfers:
            source = t.get("source_output", {})
            if source.get("run_id") != a["run_id"] or source.get("job_id") != parent["job_id"]:
                continue
            if not all(source.get(k)==pub[k] for k in ("output_name", "artifact_ref", "sha256")):
                continue
            received = {"source_run": a["run_id"], "source_job": parent["job_id"],
                        "source_ref": pub["artifact_ref"], "target_run": c["run_id"],
                        "target_ref": t["artifact_ref"], "sha256":pub["sha256"],
                        "consuming_job":consumer["job_id"],
                        "consumed_under_new_ref": any(x["artifact_ref"] == t["artifact_ref"] and
                            x["sha256"] == pub["sha256"] and x["role"] == "prior_records" for x in consumer["inputs"]),
                        "separate_local_identity": t["artifact_ref"] != pub["artifact_ref"]}
    first_query = {i["sha256"] for i in a["inputs"] if i["role"]=="query"}
    second_query = {i["sha256"] for i in c["inputs"] if i["role"]=="query"}
    citations = []
    for study, output_pairs in ((a, outputs), (c, compare)):
        final = next((d for d in study["decisions"] if "close_run" in d["ops"] and d["outcome"]=="adopted"), None)
        refs = {out["artifact_ref"] for _,out in output_pairs}
        checked = [r["arguments"].get("ref") for r in study["native_actions"]
                   if r["tool"]=="gx_read_artifact" and r["scope"]=="run" and r.get("verified_json_sha256")
                   and final and final["native_row"] and r["native_row"] < final["native_row"]]
        citations.append(sorted(set(checked).intersection(refs, final["evidence_refs"] if final else [])))
    expansions = [r for r in c["native_actions"] if r["tool"] == "gx_instantiate_recipe" and r.get("recipe")]
    replayed = []
    for node in c["nodes"]:
        spec = node["spec"]
        pin = spec.get("recipe_decision")
        if not pin or node["status"] != "SUCCEEDED":
            continue
        published = next((p for p in a["recipe_publications"]
                          if p["reference"] == pin["recipe"] and p["digest"] == pin["digest"]), None)
        matches = [exp for exp in expansions if any(all(
            step.get(k) == spec.get(k) for k in ("capability", "inputs", "parameters", "recipe_decision"))
            for step in exp["expanded_steps"])]
        job = next((j for j in c["jobs"] if j["job_id"] == node["job_id"]), None)
        if not (published and matches and job):
            continue
        decision_point = matches[-1]["decision_point"]
        output = next((out for out in job["outputs"] if out["output_name"] == decision_point["output"]), None)
        if output:
            replayed.append({"source_run": a["run_id"], "recipe_decision":pin,
                             "executed_node":node["node_id"], "executed_job":node["job_id"],
                             "inputs":spec["inputs"], "output":output["artifact_ref"],
                             "expansion_calls":[m["call_id"] for m in matches],
                             "decision_point_event":decision_point["event"],
                             "evidence_cited_on_close":any(output["artifact_ref"] in d["evidence_refs"]
                                  for d in c["decisions"] if "close_run" in d["ops"] and d["outcome"]=="adopted")})
    closure = json.loads((HERE / "preregistration.json").read_text())
    terms_unchanged = all(hashlib.sha256((HERE / name).read_bytes()).hexdigest() == expected
                          for name, expected in closure["input_and_criteria"].items())
    return {"independent_counts": findings, "handoff":received, "frozen_inputs_match": terms_unchanged,
            "verified_full_read_before_closing_citation":citations,
            "both_closed":all(s["goal_status"]=="CLOSED" for s in studies),
            "distinct_queries": bool(first_query and second_query and first_query.isdisjoint(second_query)),
            "authenticated_turns": [sum(t["official_session"] is not None for t in s["turns"]) for s in studies],
            "actual_job_counts": [len(s["jobs"]) for s in studies],
            "parent_saved_recipes": [r["reference"] for r in a["recipe_publications"]],
            "successful_expansions": len(expansions), "executed_recipe_nodes":replayed,
            "decision_refusals": [len(s["decision_refusals"]) for s in studies]}


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts",type=Path)
    args=parser.parse_args()
    checks = field_checks(json.loads(args.receipts.read_text()))
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    sciences = checks["independent_counts"].values()
    complete = (
        checks["both_closed"] and checks["distinct_queries"] and checks["frozen_inputs_match"] and
        all(v.get("scope") == 559292 and all(value for key, value in v.items() if key != "scope") for v in sciences) and
        checks["handoff"] and checks["handoff"]["consumed_under_new_ref"] and checks["handoff"]["separate_local_identity"] and
        all(checks["verified_full_read_before_closing_citation"]) and all(checks["authenticated_turns"]) and
        any(node["evidence_cited_on_close"] for node in checks["executed_recipe_nodes"])
    )
    raise SystemExit(0 if complete else 1)
