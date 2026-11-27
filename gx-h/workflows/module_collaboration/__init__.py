"""Project collaboration with lead-selected tasks and coordinator-directed returns."""
from pathlib import Path

from gx_core.orchestration import build, load_definition
from gx_core.orchestration.engine import module_sources

from . import nodes, products


def load_workflow():
    workflow = build(load_definition(Path(__file__).with_name("v1.json")),
        {name: getattr(nodes, name) for name in ("scope", "modules", "handoff", "project_decision", "report")},
        stage_rules={"modules": nodes.modules_rule, "project_decision": nodes.decision_rule},
        task_product_validator=products.validate, completion_evidence=nodes.completion_evidence)
    workflow.sources.update(module_sources((load_workflow,)))
    return workflow


def register_all(registry):
    return [registry.register(load_workflow())]
