"""Compatibility bindings for the published coordinate-study versions."""
import json

from gx_core import config
from gx_core.errors import SchemaInvalid
from gx_core.orchestration.hooks import ResearchStart, WorkerTools, WorkflowHooks
from gx_core.p0 import science_inputs
from workflows.tl1a_geometry import domain, nodes, replan, review_tools
from .grid import task_context


def example_spec():
    return json.loads((config.HARNESS_ROOT.parent / "examples/tl1a_geometry/project.json").read_text())


def research_start(spec):
    return ResearchStart({}, (domain.coordinate_input(),), tuple(domain.user_items(spec)),
                         {"objective": "context", "coordinates": "execution", "job-limit": "context"},
                         ("coordinates", "job-limit", "evaluation"))


def bootstrap(conn, run_id, items, roles):
    source = next(item for item in items if item["item_id"] == "coordinates")
    source["value"] = {**domain.source_value(), "artifact_ref": domain.source_artifact(conn, run_id)}
    roles.update({item["item_id"]: "evaluation" for item in items if item["item_type"] == "evaluation_spec"})


def validate_intent(conn, run_id, edits):
    for edit in edits:
        if edit["item_type"] == "evaluation_spec" and edit["value"] is not None:
            if edit["item_id"] != "evaluation":
                raise SchemaInvalid("Use the evaluation item for this comparison's shared geometric criteria")
            domain.intent_criteria(conn, run_id, edit["value"])
    return science_inputs.unsupported_edit(conn, run_id, edits)


def historical_review_tools(conn, run_id):
    def prepare(row, tool, args, envelope):
        return review_tools.recalculate_now(conn, row, args)

    def call(row, tool, args, envelope, *, prepared):
        return review_tools.worker_call(conn, tool, args, row, envelope, prepared=prepared)

    return WorkerTools(tuple(review_tools.WORKER_TOOLS), prepare, call)


hooks = WorkflowHooks(validate_intent=validate_intent, build_worker_tools=historical_review_tools,
    prepare_dispatch=nodes.prepare_dispatch, build_task_context=task_context, reevaluate=domain.reevaluate_intent,
    replan=replan.prepare, research_start=research_start, bootstrap_intent=bootstrap,
    register_supplement=science_inputs.register_supplement, reconcile_supplement=science_inputs.reconcile_supplement,
    auto_advance=True)

quality_hooks = WorkflowHooks(auto_advance=True)
