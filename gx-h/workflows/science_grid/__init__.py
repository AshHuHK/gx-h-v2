"""Pinned GX workflow for synthetic numerical investigations."""
from pathlib import Path

from gx_core.orchestration import build, load_definition
from gx_core.orchestration.engine import module_sources

from . import nodes


def load_workflow():
    graph = load_definition(Path(__file__).with_name("v1.json"))
    workflow = build(graph, {n["name"]: getattr(nodes, n["name"]) for n in graph["nodes"]},
        stage_rules={"experiment": nodes.experiment_rule, "assessment": nodes.assessment_rule,
                     "decision": nodes.decision_rule},
        task_product_validator=nodes.validate, completion_evidence=nodes.completion_evidence)
    workflow.sources.update(module_sources([load_workflow]))
    return workflow


def register_all(registry):
    return [registry.register(load_workflow())]
