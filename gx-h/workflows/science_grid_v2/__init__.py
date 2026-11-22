"""Version two of science_grid shares the original experiment and evaluator."""
from pathlib import Path

from gx_core.orchestration import build, load_definition
from gx_core.orchestration.engine import module_sources
from gx_core.p0 import science, science_inputs, science_refresh, science_refresh_v2, science_v2
from workflows.science_grid import nodes as original
from . import nodes


def load_workflow():
    graph = load_definition(Path(__file__).with_name("v2.json"))
    bindings = {"proposal": original.proposal, "experiment": original.experiment,
                "assessment": nodes.assessment, "decision": nodes.decision, "report": original.report}
    workflow = build(graph, bindings, stage_rules={"experiment": original.experiment_rule,
        "assessment": nodes.assessment_rule, "decision": nodes.decision_rule},
        task_product_validator=nodes.validate, completion_evidence=original.completion_evidence)
    workflow.sources.update(module_sources([load_workflow, science.score, science_refresh.review_for,
        science_v2.settle_assessment, science_refresh_v2.prepare_dispatch, science_inputs.validate_product]))
    return workflow


def register_all(registry):
    return [registry.register(load_workflow())]
