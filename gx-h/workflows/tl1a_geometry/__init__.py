"""Installed scientific workflow for coordinate selection sensitivity."""
from pathlib import Path

from adapters.scientific.tl1a_geometry import review, worker
from gx_core.orchestration import build, load_definition
from gx_core.orchestration.engine import module_sources
from . import domain, nodes, replan, review_tools


def register_all(registry):
    graph = load_definition(Path(__file__).with_name("v1.json"))
    graph["coordinate_quality"] = {"id": "tl1a_coordinate_quality", "version": "1",
                                   "sha256": registry.get("tl1a_coordinate_quality", "1").digest()}
    workflow = build(graph, {x["name"]: getattr(nodes, x["name"]) for x in graph["nodes"]},
                     stage_rules={x["name"]: getattr(nodes, x["name"] + "_rule") for x in graph["nodes"] if x["work"] == "stage"},
                     completion_evidence=nodes.completed, task_product_validator=nodes.validate)
    workflow.sources.update(module_sources([register_all, domain.freeze, review_tools.worker_call, worker.validate_criteria,
                                           review.independent_check, replan.prepare]))
    return [registry.register(workflow)]
