"""Frozen two-entry adaptation of the released three-mask recipe in GX's static Engine.

The benchmark stages enter the same JobService as the two Goal arms. Only the
temporary installation registers this version; the product's shipped pins are untouched.
"""
import json
import os
from pathlib import Path
import sys
from functools import partial, update_wrapper

from common import ENTRIES, MASKS, METHOD, ROOT, pair_results

sys.path.insert(0, str(ROOT / "gx-h"))
from gx_core import config
from gx_core.artifacts import ArtifactService
from gx_core.jobs.service import JobService
from gx_core.jobs.workflow import observe_workflow_job, submit_workflow_job
from gx_core.orchestration import service
from gx_core.orchestration.engine import Node, NodeResult, Workflow
from gx_core.orchestration.hooks import ResearchStart, WorkflowHooks
from gx_core.persistence import store

STATIC_ID = "v61_benchmark_fixed_geometry"


def research_start(spec):
    return ResearchStart({}, tuple(spec["input_files"]), (), {"objective": "context"})


def phase(ctx):
    run = ctx.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (ctx.run_id,)).fetchone()
    inputs = json.loads(run["inputs_json"])["files"]
    outputs = {}
    jobs = {}
    for entry in ENTRIES:
        source, = [f for f in inputs if f["role"] == "structure_" + entry]
        jobs[entry] = submit_workflow_job(ctx.conn, run_id=ctx.run_id, operation_id=f"{entry}-{ctx.node}",
                    family=STATIC_ID, capability=METHOD, inputs={"structure": source["artifact_ref"]},
                    parameters={"mask": ctx.node}, binding={"workflow_id": STATIC_ID, "node_id":ctx.node})["job_id"]
    for entry, job in jobs.items():
        out = JobService(ctx.conn).wait(job, 40)
        if out["status"] != "SUCCEEDED":
            raise ValueError(f"Static fit {job} ended {out['status']}")
        outputs[entry] = observe_workflow_job(ctx.conn, job, "geometry.json")["observations_ref"]
    return NodeResult(outputs=outputs)


def settled(mask, conn, run_id, round_index):
    n = conn.execute("SELECT count(*) FROM jobs WHERE run_id=? AND idempotency_key LIKE ? AND status='SUCCEEDED'",
                     (run_id, f"job:{run_id}:{STATIC_ID}:%-{mask}")).fetchone()[0]
    return {"complete": n == len(ENTRIES), "needs":[]}


def conclude(ctx):
    refs = [ctx.outputs[mask][entry] for mask in MASKS for entry in ENTRIES]
    assets = ArtifactService(ctx.conn)
    statements = pair_results([assets.read_json(ref) for ref in refs])
    result = assets.publish_bytes(run_id=ctx.run_id, namespace=ctx.run_id + "/benchmark",
        rel_path="conclusion.json", kind="comparison_conclusion", media_type="application/json",
        data=json.dumps({"statements":statements, "evidence_refs":refs}).encode())
    return NodeResult(outputs={"claim_ref":result.artifact_ref})


def done(conn, run):
    row = conn.execute("SELECT artifact_ref FROM artifacts WHERE run_id=? AND kind='comparison_conclusion'",
                       (run["run_id"],)).fetchone()
    return bool(row), []


def make_workflow():
    stages = [Node(mask, phase, deps=(MASKS[idx-1],) if idx else (), work="stage")
              for idx, mask in enumerate(MASKS)]
    return Workflow(STATIC_ID, "1", [*stages, Node("conclude", conclude, (MASKS[-1],), work="mechanical")],
                    entry_transitions=("AUDITING","PLANNING","ACTIVE"),
                    description="Frozen sequential mask fits of the two enrolled deposits",
                    stage_rules={mask: update_wrapper(partial(settled, mask), settled) for mask in MASKS}, completion_evidence=done)


def install(config_dir):
    from shutil import copytree
    registry = service.default_registry().staged_copy({})
    flow = registry.register(make_workflow(), hooks=WorkflowHooks(research_start=research_start))
    # Model the operator installation of this benchmark workflow, in its own config.
    fresh = not config_dir.exists()
    if fresh:
        copytree(config.CONFIG_DIR, config_dir)
    config.CONFIG_DIR = config_dir
    path = config_dir / "shipped.json"
    doc = json.loads(path.read_text())
    if fresh:
        doc["workflows"][STATIC_ID + "@1"] = flow.digest()
        doc["capabilities"][METHOD] = "91840fdb7cac9024e0e8b9374e36afc94b7149e100da3b0bfd4278c08a754249"
        path.write_text(json.dumps(doc, indent=2) + "\n")
    elif doc['workflows'][STATIC_ID + "@1"] != flow.digest():
        raise ValueError("Static benchmark workflow changed after installation")
    service.set_registry(registry)
    return flow


if __name__ == "__main__":
    # Stable module name on both install and cold re-entry.
    import static_recipe
    static_recipe.install(Path(os.environ["GX_COMPARISON_CONFIG"]))
    from gx_core.p0.cli import main
    raise SystemExit(main(sys.argv[1:]))
