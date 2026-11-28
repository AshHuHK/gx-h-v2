"""Read-only causal replay of published B papers, graph decision and actual scientific Job."""
import argparse
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
ROLES = ("prediction", "reference", "mapping")
SCORER = "gx-h/plugins/installed/org.gx.archival-fold-calibration/1.0.0/job-scripts/score.py"
EXPECTED_PROGRAM = "1f69a4b3a13c319e25df7baff7afeebf772da41eecbf2921e1492d5242f52e4f"


def load(path):
    return json.loads(path.read_bytes())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scorer():
    path = ROOT / SCORER
    if digest(path) != EXPECTED_PROGRAM:
        raise ValueError("Replay requires the original CPU assay program.")
    spec = importlib.util.spec_from_file_location("original_mapped_assay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.score


def bind(claim, node_candidates, inputs, files, method, scope, observed_jobs, registration_cursor):
    """Reconstruct this assay's ordered contract from facts available before its Job."""
    reg = claim["registration"]
    spec = reg["method_pin"]["measurement_spec"]
    if (spec["input_role"] != "reference" or len(reg["data"]) != 1
            or reg["claim_scope"] != scope or spec["applicability"] != scope):
        return "unbound_primary_material"
    if (spec["metric_id"], spec["unit"], spec["output_name"]) != ("calpha_rmsd_A","angstrom","agreement.json"):
        return "different_registered_endpoint"
    if reg["method_pin"]["manifest_sha256"] != method["sha256"]:
        return "different_registered_method"
    if len(node_candidates) != 1 or node_candidates[0]["parameters"] != spec["parameters"]:
        return "ambiguous_or_changed_node"
    refs = node_candidates[0]["inputs"]
    if len(refs) != len(ROLES) or len(set(refs)) != len(ROLES):
        return "ambiguous_committed_inputs"
    expected_refs = dict(zip(ROLES, refs, strict=True))
    if expected_refs["reference"] != reg["data"][0]["artifact_ref"]:
        return "changed_reference_role"
    pinned_sources = {o["ref"]:o["sha256"] for o in claim["source_records"]}
    if not all(ref in claim["evidence_refs"] and ref in pinned_sources for ref in refs):
        return "unfrozen_companion"
    if len(inputs) != len(ROLES) or {o["role"] for o in inputs} != set(ROLES):
        return "ambiguous_admitted_roles"
    for item in inputs:
        role, ref = item["role"], item["artifact_ref"]
        if expected_refs[role] != ref or item["sha256"] != pinned_sources[ref] or digest(files[role]) != item["sha256"]:
            return "different_" + role
    signature = {(item["role"],item["sha256"]) for item in inputs}
    for observed_at, job in observed_jobs:
        if (observed_at <= registration_cursor and job["method"] == method and job["parameters"] == spec["parameters"]
                and signature == {(i["role"],i["sha256"]) for i in job["inputs"]}
                and any(o["output_name"] == spec["output_name"] for o in job["outputs"])):
            return "retrospective_tuple"
    return "matches_original_tuple"


def run(release):
    records, originals = release / "first-run", release / "released-originals"
    result, history = load(records / "result.json"), load(records / "timeline.json")
    actual = next(j for j in result["jobs"] if j["method"]["id"] == "mapped-structure-agreement")
    ref = next(m for m in result["materials"] if m["role"] == "reference")
    scope = ref["provenance"][0]["acquisition"]["applicability"]
    selected = [c for c in result["predictions"] if c["registration"]["method_pin"]["measurement_spec"]["input_role"] == "reference"]
    if len(selected) != 1:
        raise ValueError("Ambiguous registered prediction for this material role.")
    claim = selected[0]
    reg = claim["registration"]
    decision = next(d for d in history["decisions"] if d["decision_ref"] == reg["decision_ref"])
    nodes = [n for n in decision["operations"] if n["op"] == "add_node" and
             n["capability"] == reg["method_pin"]["capability"]]
    created = next(e["event_id"] for e in history["events"] if e["kind"] == "job.created" and e["subject_id"] == actual["job_id"])
    if not reg["event_id"] < decision["event_id"] < created or actual["owner"]["decision_ref"] != reg["decision_ref"]:
        raise ValueError("Actual Job has no matching pre-output commitment.")
    definition = reg["method_pin"]["measurement_spec"]
    if definition["metric_definition"]["pointer"] != "/calpha_rmsd_A" or reg["prediction"]["operator"] != "le":
        raise ValueError("Endpoint selector or direction changed.")
    outputs = [o for o in actual["outputs"] if o["output_name"] == definition["output_name"]]
    if len(outputs) != 1 or outputs[0]["sha256"] != digest(records / "agreement.json"):
        raise ValueError("The released CPU output differs from the accepted named publication.")
    original = load(records / "agreement.json")
    original_coverage = load(release / "preregistration.json")["quality"]["decision"]
    original_seq = load(release / "materials/P00711.json")["sequence"]
    included = load(records / "inference.json")
    first_publications = load(HERE/"score-publication.json")["events"]
    observed = [(e["event_id"], j) for j in result["jobs"] for e in first_publications if j["job_id"] == e["job_id"]
                and any(o["artifact_ref"] == e["artifact_ref"] and o["sha256"] == e["sha256"] for o in j["outputs"])]
    if not any(j["job_id"] == actual["job_id"] and event > created for event,j in observed):
        raise ValueError("Missing immutable publication of the observed measure.")
    case = {}
    with TemporaryDirectory() as temp:
        scratch = Path(temp)
        crystal = scratch / "1F6S.cif"
        crystal.write_bytes(gzip.decompress((originals / "1F6S.cif.gz").read_bytes()))
        real = {"prediction":records / "model.cif", "reference":crystal, "mapping":originals / "mapping.json"}
        score = scorer()
        args = (claim,nodes,actual["inputs"])
        def assign(c, n, i, files, cursor=None):
            return bind(c,n,i,files,actual["method"],scope,observed,c["registration"]["event_id"] if cursor is None else cursor)
        def measure(key, files):
            answer = score(files["prediction"], files["reference"], files["mapping"])
            case[key] = {"binding":assign(*args,files), "matched":answer["matched_positions"], "mapped":answer["mapped_positions"],
                         "full_mature_length":len(original_seq), "whole_chain_coverage":answer["matched_positions"]/len(original_seq),
                         "rmsd_A":answer["calpha_rmsd_A"], "map_sha256":answer["mapping_sha256"], "prediction_coordinates_sha256":answer["input_sha256"]}
            return answer
        if json.loads(json.dumps(measure("committed", real))) != original:
            raise ValueError("Fresh CPU output differs from the published report.")
        if included["model_sha256"] != digest(real["prediction"]) or included["input_sha256"] != digest(release / "materials/P00711.json"):
            raise ValueError("The coordinate source is not the frozen model for this natural specimen.")
        for mode in ("prefix_three", "cyclic_offset"):
            mapping = load(real["mapping"])
            pairs = mapping["pairs"]
            mapping["pairs"] = pairs[:3] if mode == "prefix_three" else [[p[0],pairs[(i+1)%len(pairs)][1]] for i,p in enumerate(pairs)]
            changed = scratch / (mode + ".json")
            changed.write_text(json.dumps(mapping))
            measure(mode, {**real,"mapping":changed})
        measure("substitute_crystal_for_prediction", {**real, "prediction":real["reference"]})
        # Same basename offered under two roles or different content cannot identify a sample.
        duplicate_name = scratch / "other"
        duplicate_name.mkdir()
        alias = duplicate_name/"1F6S.cif"
        alias.write_bytes(real["prediction"].read_bytes())
        case["same_name_different_reference"] = {"binding":assign(*args, {**real,"reference":alias})}
        extra = [*actual["inputs"],dict(actual["inputs"][1])]
        case["duplicate_reference"] = {"binding":assign(claim,nodes,extra,real)}
        case["two_scorer_nodes"] = {"binding":assign(claim,[*nodes,*nodes],actual["inputs"],real)}
        case["identical_reading_already_known"] = {"binding":assign(*args,real,cursor=max(e for e,_ in observed)+1)}
        first, = [c for c in result["predictions"] if c is not claim]
        case["first_registration"] = {"binding":assign(first,nodes,actual["inputs"],real)}
    expected = {"committed":"matches_original_tuple","prefix_three":"different_mapping","cyclic_offset":"different_mapping",
                "substitute_crystal_for_prediction":"different_prediction","same_name_different_reference":"different_reference",
                "duplicate_reference":"ambiguous_admitted_roles","two_scorer_nodes":"ambiguous_or_changed_node",
                "identical_reading_already_known":"retrospective_tuple","first_registration":"unbound_primary_material"}
    if {key:value["binding"] for key,value in case.items()} != expected:
        raise ValueError("The scoped causal controls did not reproduce their registered order.")
    return dict(evidence_class="POST_CLOSE_READ_ONLY_CPU_CONTROLS", run_id=result["run_id"], original_job=actual["job_id"],
                registration_event=reg["event_id"], decision_event=decision["event_id"], job_created=created,
                registered_metric=definition["metric_id"], registered_unit=definition["unit"],
                threshold=reg["prediction"]["threshold"], coverage_criterion_at_field_freeze=original_coverage,
                coverage_in_machine_metric_contract=False, scope_roles=reg["method_pin"]["measurement_spec"]["source_roles"],
                original_formal_classifications=[c["classification"] for c in result["predictions"]],
                cases=case, new_gpu_jobs=0, new_model_turns=0, original_run_writes=0)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--release", type=Path, default=HERE.parent, help="Published B first-run and released-originals directory.")
    print(json.dumps(run(p.parse_args().release.resolve()), ensure_ascii=False, indent=2))
