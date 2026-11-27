"""Verify the two deposited coordinate records and the real Goal-to-Goal evidence links offline."""
import hashlib
import json
import sys
from pathlib import Path

from independent import measure

HERE = Path(__file__).resolve().parent


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def verify_case(study, entry):
    require(study["goal"] == "CLOSED", f"{entry}: research is still open")
    job, = [job for job in study["jobs"] if job["method"] == "deposited-backbone-qc@1"]
    require(job["status"] == "SUCCEEDED", f"{entry}: new CPU calculation missing")
    original, = [item for item in study["materials"] if item["role"] == "structure"]
    source, = job["inputs"]
    named, = [item for item in job["outputs"] if item["output_name"] == "qc.json"]
    result = job["readout"]
    file_sha = hashlib.sha256((HERE / "materials" / (entry + ".cif")).read_bytes()).hexdigest()
    require(original["artifact_ref"] == source["artifact_ref"], f"{entry}: wrong Job input")
    require(file_sha == result["input_sha256"] == original["sha256"] == source["sha256"],
            f"{entry}: deposited bytes differ")
    require((result["entry"], result["chain"], result["model"]) == (entry, "A", 1), f"{entry}: wrong scope")
    comparison = measure(HERE / "materials" / (entry + ".pdb"), "A")
    counts = {"denominator": "declared_residues", "observed": "observed_residues",
              "c_alpha": "ca_count", "complete_n_ca_c_o": "complete_backbones"}
    require(all(comparison[a] == result[b] for a, b in counts.items()), f"{entry}: counts disagree")
    require(abs(comparison["radius_of_gyration_A"] - result["ca_radius_of_gyration_A"]) <= 0.0001,
            f"{entry}: radius differs from independent PDB calculation")
    closure, = [d for d in study["decisions"] if "close_run" in d["ops"] and d["outcome"] == "adopted"]
    require(named["artifact_ref"] in closure["evidence_refs"], f"{entry}: closure omitted its own readout")
    received = [event for event in study["lead_endpoint_receipts"] if event["turn"] == closure["turn_id"]
                and any(ep["source_ref"] == named["artifact_ref"] for ep in event["endpoints"])]
    require(received, f"{entry}: missing endpoint delivery to closing Lead")
    require(any(turn["classification"] == "NATIVE_MODEL" and turn["turn_id"] == closure["turn_id"]
                for turn in study["turns"]), f"{entry}: no verified native closing turn")
    return job, named


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "field.json"
    field = json.loads(path.read_bytes())
    a, b = field["parent"], field["successor"]
    old_job, old_output = verify_case(a, "1UBQ")
    new_job, new_output = verify_case(b, "1MBN")
    require(a["run_id"] != b["run_id"] and old_job["job_id"] != new_job["job_id"]
            and old_output["artifact_ref"] != new_output["artifact_ref"], "Repeated Run, Job or result")
    require(old_job["provider"] == new_job["provider"], "Provider changed between studies")
    card = a["save_calls"][0]["returned"]
    reference, digest = card["reference"], card["digest"]
    require(any(reference in search["references"] for search in b["catalog"]), "B search missed A's recipe")
    require(any(exp["digest"] == digest and exp["recipe"] == reference for exp in b["recipe_expansions"]),
            "B did not instantiate the published recipe")
    require(new_job["recipe_decision"]["digest"] == digest, "New Job was not bound to A's recipe")
    require(any(point["digest"] == digest and point["job_id"] == new_job["job_id"]
                and point["evidence"]["artifact_ref"] == new_output["artifact_ref"]
                for brief in b["lead_points"] for point in brief["points"]), "B Lead missed its new decision point")
    require(any(item["seconds"] > 0 and item["left_scope"] != item["right_scope"]
                for item in a["native_worker_overlaps"]), "No measured overlap between A's researchers")
    require(any(item.get("real_model_handoff") and item.get("status") == "ENACTED" for item in a["handoffs"]),
            "No enacted native report handoff")
    print("2 closed native studies; 2 distinct CPU Jobs and coordinate readouts; matched independent PDB calculations; "
          "saved recipe, discovered and acted on new results; overlapping native researchers and a verified handoff.")


if __name__ == "__main__":
    main()
