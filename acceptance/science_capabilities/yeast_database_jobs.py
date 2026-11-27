"""Real detached database/BLAST/MCP Jobs driven by scripted native callback decisions."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from acceptance.v622.test_close import CloseStudy
from acceptance.v62a.interleavings_fixture import PRODUCT, draft, save
from gx_core.artifacts import ArtifactService
from gx_core.p0 import manifest
from gx_core.plugins.catalog import package_digest

PACKAGE = "org.gx.yeast-enzyme-evidence@1.0.1"
SEARCH = "yeast-reaction-index@2"
RECORD = "yeast-uniprot-record@2"
MCP = "yeast-enzyme-mcp@2"


def start(work, substrate):
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    scope = work / "scope.json"
    save(scope, {"schema": "gx.yeast-enzyme-scope/1", "taxid": 559292})
    limits = {"job_submissions": 8, "cpu_seconds": 1500, "worker_tasks": 40, "api_calls": 40,
              "tokens": 60000, "gpu_jobs": 0, "gpu_minutes": 0, "wall_hours": 1}
    grant = {"project": "yeast-database-control", "research_effort": "deep", "research_modules": ["scholarly_investigation@1"],
             "plugins": [PACKAGE, "org.gx.scholarly-sources@1.0.0"], "capabilities": [SEARCH, RECORD, MCP],
             "input_files": [{"path": str(scope), "role": "scope"}], "run_budget": limits,
             "workspace_budget": limits, "turn_token_budget": 1000}
    save(work / "grant.json", grant)
    cmd = [sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "start", "--state", str(work / "state"),
           "--run-key", "yeast-" + substrate, "--goal-config", str(work / "grant.json"), "--goal",
           f"What database evidence supports {substrate} hydrolysis in baker's yeast?"]
    result = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "GX_CODEX_OFFLINE": "1"})
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)


def verify(study, substrate):
    lead = study.next_actor("run")
    scope, = study.materials
    branch = {"op": "open_branch", "branch": "local:annotations", "hypothesis": "The exact reaction has annotated yeast enzymes.",
              "alternative": "Only broader enzyme classes are annotated.", "method_family": "database lookup",
              "discriminating_observation": "Compare the exact reaction and broader enzyme-class association results.",
              "budget_limits": {"job_submissions": 3, "cpu_seconds": 600}, "stop_condition": "Two sources checked."}
    index = study.node(lead, "search", scope, capability=SEARCH, parameters={"substrate": substrate}, branch_id=branch["branch"])
    submit = study.call(lead, "gx_decide_revision", {"decision": draft(branch, index, watches=[
        {"event": "job_settled", "subject_ref": index["node"]}])}, "index")
    job = study.settle(submit["aliases"][index["node"]])
    assets = ArtifactService(study.conn)
    lead = study.next_actor("run")
    annotations = study.call(lead, "gx_read_artifact", {"ref": job["result_ref"]}, "read-index")
    counts = annotations["counts"]
    if substrate == "trehalose":
        assert counts["rhea_rows"] == counts["enzyme_rows"] == counts["distinct_accessions"] == 3
        assert all(r["direction"] == "unknown" for c in annotations["candidates"] for r in c["associations"])
    else:
        assert counts["rhea_rows"] == 0 and counts["distinct_accessions"] == 1
        assert annotations["candidates"][0]["specificity"] == "enzyme_class_only"
    accession = annotations["candidates"][0]["accession"]
    nodes = [study.node(lead, name, job["result_ref"], capability=cap, parameters={"accession": accession},
                        branch_id=submit["aliases"][branch["branch"]]) for name, cap in (("original", RECORD), ("mcp", MCP))]
    submit = study.call(lead, "gx_decide_revision", {"decision": draft(*nodes, evidence=[job["result_ref"]],
            watches=[{"event": "job_settled", "subject_ref": op["node"]} for op in nodes])}, "read-database")
    original, mcp = [study.settle(submit["aliases"][op["node"]]) for op in nodes]
    outputs = {}
    jobs = []
    for j in (job, original, mcp):
        receipt = manifest.read_manifest(study.conn, j["job_id"])
        files = {item["output_name"]: item["artifact_ref"] for item in receipt["completion"]["outputs"]}
        outputs.update(files)
        jobs.append({"job_id": j["job_id"], "status": j["status"], "capability": receipt["admission"]["capability"],
                     "outputs": files})
    lead = study.next_actor("run")
    ref = outputs["uniprot.json"]
    assignment = study.open_args("record-reader", inputs=[ref], caps=[], round=2)
    assignment.update(module_id="scholarly_investigation", focus="literature",
                      expected_delivery="Independently read the accession identity from the acquired public record.")
    peer = study.call(lead, "gx_open_module", assignment, "open-record-reader")
    reader = study.bind(peer["task_id"])
    page = study.call(reader, "gx_read_source", {"source_ref": ref}, "record-segments")
    selected = next(row for row in page["segments"] if row["label"] == "/primaryAccession")
    passage = study.call(reader, "gx_read_source", {"source_ref": ref, "segment_id": selected["segment_id"]}, "read-accession")
    assert passage["job_id"] == original["job_id"] and passage["receipt_id"]
    official = assets.read_json(outputs["record-summary.json"])
    external = assets.read_json(outputs["mcp-readback.json"])
    assert external["record"]["primaryAccession"] == official["accession"] == accession
    assert external["record_version"] == official["entry_version"] and external["tool"] == "get_protein_info"
    assert official["matches_index_sequence"] and official["source_sha256"] == assets.describe(ref)["sha256"]
    hits = assets.read_json(outputs["homology.json"])
    assert hits["pool"]["taxid"] == 559292 and hits["pool"]["release"] == "2026_03"
    return {"control": "scripted native callbacks and actual detached CPU Jobs, including remote sources via Clash",
            "run_id": study.run_id, "substrate": substrate, "package": PACKAGE,
            "package_digest": package_digest(PRODUCT / "gx-h/plugins/installed/org.gx.yeast-enzyme-evidence/1.0.1"),
            "counts": counts, "pool": hits["pool"], "raw_hsps": hits["raw_hsps"], "non_seed_subjects": hits["non_seed_subjects"],
            "sequence_groups": hits["seed_groups"], "record": official, "mcp": {k:v for k,v in external.items() if k != "record"},
            "source_read": {key: passage[key] for key in ("source_ref","source_sha256","segment_id","label","receipt_id")},
            "jobs": jobs}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="New private state directory")
    parser.add_argument("--substrate", choices=["trehalose", "sucrose"], required=True)
    args = parser.parse_args()
    work = args.directory.resolve()
    start(work, args.substrate)
    with CloseStudy(work, reopen=True) as scenario:
        receipt = verify(scenario, args.substrate)
    save(work / "receipt.json", receipt)
    print(json.dumps(receipt))
