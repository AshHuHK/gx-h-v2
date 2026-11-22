"""Versioned coordinate-quality workflow producing reusable masks from a completed GX inventory Job."""
from pathlib import Path

from gx_core.artifacts import ArtifactService
from gx_core.orchestration import build, load_definition
from gx_core.orchestration.engine import NodeResult
from gx_core.p0 import manifest, science
from gx_core.persistence import store


def inventory(ctx):
    assets = ArtifactService(ctx.conn)
    inputs = ctx.inputs
    parent = ctx.conn.execute("SELECT project_ref FROM runs WHERE run_id=?", (inputs["parent_run_id"],)).fetchone()
    own = ctx.conn.execute("SELECT project_ref,workflow_sha256 FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()
    science.require(parent and parent["project_ref"] == own["project_ref"], "coordinate inventory requires its parent project")
    proof = manifest.validate_evidence(ctx.conn, run_id=inputs["parent_run_id"], job_id=inputs["quality_job_id"],
                                       artifact_ref=inputs["inventory_ref"])
    raw = assets.read_json(inputs["inventory_ref"])
    origin = assets.describe(inputs["structure_ref"])
    science.require(raw["input_sha256"] == origin["sha256"] and origin["run_id"] == inputs["parent_run_id"] and
                    raw["schema"] == "gx.tl1a.coordinate_quality/1", "inventory and coordinates must agree")
    denominator = raw["mapping"]["observed_union_residues"]
    coverage = {}
    for protocol, mask in raw["protocol_masks"].items():
        keys = [tuple(key) for key in mask["selected_keys"]]
        positions = {key[0] for key in keys}
        science.require(len(keys) == len(set(keys)) == mask["selected_atoms"] and
                        len(positions) == mask["selected_residues"] <= denominator,
                        "the atom mask disagrees with its reported residue or atom coverage")
        coverage[protocol] = {"selected_atoms": len(keys), "selected_residues": len(positions),
                              "fraction_of_observed_union_residues": len(positions) / denominator if denominator else 0,
                              "example_mapped_atoms": keys[:3]}
    workflow = {"id": ctx.workflow_id, "version": ctx.workflow_version, "sha256": own["workflow_sha256"],
                "run_id": ctx.run_id}
    mask = {**raw, "quality_workflow": workflow, "inventory_ref": inputs["inventory_ref"]}
    mask_ref = science._publish(ctx.conn, ctx.run_id, "quality", "geometry_masks", mask)
    summary = {"workflow": workflow, "structure_ref": inputs["structure_ref"], "input_sha256": origin["sha256"],
               "job_id": inputs["quality_job_id"], "manifest_hash": proof["manifest_hash"],
               "inventory_ref": inputs["inventory_ref"], "mask_ref": mask_ref, "source": raw["source"],
               "coverage_denominator": "observed_union_residues", "mapping": raw["mapping"],
               "protocol_coverage": coverage}
    return NodeResult(outputs={"mask_ref": mask_ref, "summary": summary})


def completed(conn, run):
    state = store.loads(store.latest_checkpoint(conn, run["run_id"]), "state_json")
    item = state["outputs"]["inventory"]
    mask = ArtifactService(conn).read_json(item["mask_ref"])
    return (mask["quality_workflow"]["run_id"] == run["run_id"] and
            mask["input_sha256"] == item["summary"]["input_sha256"]), []


def register_all(registry):
    graph = load_definition(Path(__file__).with_name("v1.json"))
    return [registry.register(build(graph, {"inventory": inventory}, completion_evidence=completed))]
