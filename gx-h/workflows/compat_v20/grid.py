"""Operator behavior attached to the existing numerical-grid graph versions."""
import json
from dataclasses import replace
from pathlib import Path

from gx_core import config
from gx_core.orchestration.hooks import ResearchStart, WorkflowHooks, validate_standard_intent
from gx_core.p0 import science, science_inputs, science_refresh, science_refresh_v2
from gx_core.research.model_activity import validated_profiles
from .grid_replan import prepare_replan


def demo_spec():
    directory = config.HARNESS_ROOT / "examples" / "p01_data"
    source = json.loads((directory / "project.json").read_text())
    predictions = {h["id"]: h["prediction"] for h in source["hypotheses"]}
    paths = [{"id": route["id"], "prediction": predictions[route["hypothesis_id"]],
              "grid": json.loads((directory / Path(route["input"]).name).read_text())}
             for route in source["paths"]]
    return {"project": "p0-grid", "objective": source["objective"]["description"], "max_rounds": 3,
            "proposal": {"threshold": -0.25, "paths": paths}}


def research_start(spec):
    items = ({"item_id": "evaluation", "item_type": "evaluation_spec", "expected_version": 0,
              "value": {"threshold": spec["proposal"]["threshold"]}},
             {"item_id": "methods", "item_type": "method_ban", "expected_version": 0,
              "value": {"forbidden_capabilities": []}})
    return ResearchStart({"proposal": spec["proposal"]}, (), items, {"objective": "context", "evaluation": "evaluation"})


def run_inputs(inputs):
    if "execution_profiles" in inputs:
        return {**inputs, "execution_profiles": validated_profiles(inputs["execution_profiles"])}
    return inputs


def validate_intent(conn, run_id, edits):
    validate_standard_intent(conn, run_id, edits)
    return science_inputs.unsupported_edit(conn, run_id, edits)


def task_context(conn, run_id, task_id, attempt):
    return science_inputs.task_context(conn, run_id, task_id)


hooks = WorkflowHooks(validate_intent=validate_intent, prepare_dispatch=science_refresh.prepare_dispatch,
    build_task_context=task_context, reevaluate=science.reevaluate_intent, replan=prepare_replan,
    research_start=research_start, run_inputs=run_inputs, register_supplement=science_inputs.register_supplement,
    reconcile_supplement=science_inputs.reconcile_supplement, auto_advance=True, mechanical_continuation=True)
v2_hooks = replace(hooks, prepare_dispatch=science_refresh_v2.prepare_dispatch)
