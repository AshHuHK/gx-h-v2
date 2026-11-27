"""Versioned scientific grid with frozen per-path products and conditional independent review."""
from dataclasses import replace
from pathlib import Path

from gx_core.orchestration import build, load_definition
from gx_core.orchestration.engine import module_sources
from gx_core.p0 import science, science_inputs, science_refresh_v2
from workflows.compat_v20 import grid, grid_dispatch
from workflows.science_grid import nodes as original
from workflows.science_grid_v2 import nodes as prior
from . import contract, nodes, products, settlement


def load_workflow():
    definition = load_definition(Path(__file__).with_name("v3.json"))
    workflow = build(definition, {"proposal": nodes.proposal, "experiment": original.experiment,
        "assessment": nodes.assessment, "decision": nodes.decision, "report": original.report},
        stage_rules={"experiment": original.experiment_rule, "assessment": nodes.assessment_rule,
                     "decision": prior.decision_rule}, task_product_validator=nodes.validate,
        completion_evidence=original.completion_evidence)
    workflow.prepare_native_product = products.prepare_native_product
    workflow.sources.update(module_sources([load_workflow, *contract.SEMANTIC_VALIDATORS,
        products.prepare_native_product, settlement.settle, science.score, science_inputs.validate_product,
        science_refresh_v2.bind_task, grid_dispatch._revise]))
    return workflow


def register_all(registry):
    hooks = replace(grid.v2_hooks, prepare_dispatch=nodes.prepare_dispatch, build_task_context=products.context)
    return [registry.register(load_workflow(), hooks=hooks)]
