"""Replay the released coordinate method on one checkout and retain its Job and store.

Run the script from the candidate release once per checkout. Pass --product,
--expect-commit and a fresh --work directory to each invocation. No model is used.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


RELEASE = Path(__file__).resolve().parents[2]
CAPABILITY = "deposited-chain-geometry@1"
PLUGIN = "org.gx.protein@0.2.0"
INPUT_SHA = "8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b"
OUTPUT_SHA = "d7d7de37690f14bb67abb37efd9ea1adb8a9448efd4ce3d6553961688e503216"


def replay(product, commit, work):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(product), *args], text=True).strip()

    head = git("rev-parse", "HEAD")
    if head != commit or git("status", "--porcelain", "--", "gx-h"):
        raise ValueError("The tested product must have the requested full SHA and a clean gx-h tree")
    if work == product or product in work.parents:
        raise ValueError("Choose a fresh work directory outside the tested checkout")
    work.mkdir(mode=0o700, parents=True)
    sys.path.insert(0, str(product / "gx-h"))
    from gx_core import budgets
    from gx_core.artifacts import ArtifactService
    from gx_core.p0 import manifest, v6_decisions, v6_goal, v6_jobs, v62_outputs
    from gx_core.persistence import store
    from tests.test_v6_decisions import bind_native_fixture, branch, draft

    original = product / "gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif"
    if hashlib.sha256(original.read_bytes()).hexdigest() != INPUT_SHA:
        raise ValueError("Archived mmCIF differs from the published input digest")
    metadata = json.loads((RELEASE / "examples/v621-coordinate-analysis/2RE9.json").read_bytes())
    spec = {"mode": "goal", "project": "archived-source", "research_effort": "deep", "research_modules": [],
            "objective": "Describe coordinate coverage in the supplied archive.",
            "plugins": [PLUGIN], "capabilities": [CAPABILITY],
            "input_files": [{"role": "structure", "path": str(original), "sha256": INPUT_SHA}]}
    with closing(store.open_store(work / "gx.db")) as db:
        run_id = v6_goal.start(db, spec, run_key="archive-compat", current=None)["run_id"]
        inputs = db.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
        material, = json.loads(inputs)["files"]
        src = {k: material[k] for k in ("artifact_ref", "sha256")}
        observation = v6_goal.record_observation(db, run_id, src["artifact_ref"], source="archive",
                                                 acquisition=metadata)
        node = {"op": "add_node", "branch": "local:sample", "node": "local:archive", "capability": CAPABILITY,
                "inputs": [src["artifact_ref"]], "parameters": {"mask": "ordered_backbone"},
                "depends_on": [], "question": "Count visible positions."}
        plan = draft(dict(branch(), budget_limits={"job_submissions": 1, "cpu_seconds": 60}), node)
        adoption = v6_decisions.adopt(db, **bind_native_fixture(db, run_id, work), decision=plan)
        node_id = adoption["aliases"]["local:archive"]
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            v6_jobs.advance(db, run_id)
            row = db.execute("SELECT status,job_id,result_ref FROM v6_nodes WHERE node_id=?", (node_id,)).fetchone()
            if row["status"] in v6_jobs.NODE_TERMINAL:
                break
            time.sleep(0.05)
        else:
            raise TimeoutError(f"Coordinate Job did not settle for {node_id}")
        if row["status"] != "SUCCEEDED":
            raise ValueError(f"Coordinate node {node_id} ended in {row['status']}")
        job, = db.execute("SELECT job_id FROM jobs WHERE run_id=?", (run_id,)).fetchall()
        if job["job_id"] != row["job_id"]:
            raise ValueError("The admitted Job does not belong to the selected node")
        execution = manifest.read_manifest(db, row["job_id"])
        admitted = execution["admission"]
        pinned_input, = admitted["inputs"]
        created, = db.execute("SELECT event_id FROM events WHERE run_id=? AND kind='job.created' AND subject_id=?",
                              (run_id, row["job_id"])).fetchone()
        if (pinned_input["role"] != "structure" or pinned_input["artifact_ref"] != src["artifact_ref"]
                or pinned_input["sha256"] != INPUT_SHA or src["sha256"] != INPUT_SHA
                or created <= observation["event_id"]):
            raise ValueError("Job admission does not follow the recorded acquisition of this input")
        output_ref = row["result_ref"]
        output, = [item for item in execution["completion"]["outputs"] if item["artifact_ref"] == output_ref]
        classified = v62_outputs.classify(db, run_id, output_ref)
        assets = ArtifactService(db)
        geometry = assets.read_json(output_ref)
        digest = hashlib.sha256(assets.read_bytes(output_ref)).hexdigest()
        selected, denominator = geometry["selected_residues"], geometry["observed_union_residues"]
        if (output["output_name"] != "geometry.json" or digest != OUTPUT_SHA or output["sha256"] != digest
                or classified["sha256"] != digest or classified["nature"] != "unclassified"
                or (selected, denominator) != (49, 164)):
            raise ValueError("The legacy output changed in bytes, classification, name or counts")
        spent = budgets.state(db, budgets.budget_for(db, run_id=run_id))["spent"]
        if spent.get("api_calls", 0) or spent.get("tokens", 0):
            raise ValueError("The acceptance unexpectedly charged model usage")
        return {"schema": "gx.accept.legacy_coordinates/1", "commit": head, "run_id": run_id, "node_id": node_id,
                "job_id": row["job_id"], "plugin": PLUGIN, "capability": CAPABILITY,
                "capability_sha256": admitted["capability"]["sha256"], "nature": classified["nature"],
                "raw_input": src, "output_name": output["output_name"], "output_ref": output_ref,
                "output_sha256": digest, "admission_sha256": execution["admission_hash"],
                "manifest_sha256": execution["manifest_hash"], "acquisition_event": observation["event_id"],
                "job_created_event": created, "pre_admission_cursor": admitted.get("provenance_cursor"),
                "selected": selected, "denominator": denominator, "real_jobs": 1,
                "paid_calls": spent.get("api_calls", 0), "tokens": spent.get("tokens", 0)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", type=Path, default=RELEASE, help="Checkout with the tested GX runtime")
    parser.add_argument("--expect-commit", required=True, help="Exact tested Git SHA")
    parser.add_argument("--work", type=Path, required=True, help="Fresh external directory for this run")
    args = parser.parse_args()
    work = args.work.resolve()
    receipt = replay(args.product.resolve(), args.expect_commit, work)
    content = json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False) + "\n"
    (work / "receipt.json").write_text(content, encoding="utf-8")
    sys.stdout.write(content)


if __name__ == "__main__":
    main()
