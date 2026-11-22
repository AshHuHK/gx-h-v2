"""Reviewer-owned detached reproduction of all observed TL1A routes under one Job reservation."""
from dataclasses import replace

from gx_core.capabilities import shipped
from gx_core.domain import ids
from gx_core.orchestration import build
from gx_core.orchestration.engine import module_sources, pause
from gx_core.persistence import store
from workflows.compat_v20 import geometry
from workflows.tl1a_geometry import domain, nodes
from workflows import tl1a_geometry_v2 as v2

from . import review_jobs


def research_start(spec):
    result = geometry.research_start(spec)
    domain.require(spec["max_jobs"] >= 4, "this study needs one inventory, two measurements and one independent review Job")
    return result


def proposal(ctx):
    if not ctx.state.get("next_plan"):
        reused = nodes.observed_jobs(ctx.conn, ctx.run_id, ctx.outputs["quality"])
        minimum = 1 + max(0, 2 - len(reused))
        if domain.budget_left(ctx.conn, ctx.run_id) < minimum:
            return pause([{"kind": "budget", "key": "job_submissions"}],
                         reason="Fund two distinct measurements and one independent review Job before planning.")
    return v2.proposal(ctx)


def prepare_dispatch(conn, run_id, task):
    if not geometry.hooks.prepare_dispatch(conn, run_id, task):
        return False
    current = conn.execute("SELECT input_json FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()
    inputs = store.loads(current, "input_json")
    kind = inputs.get("output_kind")
    if kind == "geometry_assessment":
        # Replans and supplemental reviews can replace the evidence at the same round number.
        if inputs.get("review_inputs_evidence") != inputs["evidence_ref"]:
            inputs["review_inputs_ref"] = review_jobs.prepare_inputs(conn, run_id, inputs)
            inputs["review_inputs_evidence"] = inputs["evidence_ref"]
        guidance = ("One reviewer-owned CPU Job covers this whole evidence set. Request any measured protocol first. "
                    "If pending, call gx_wait_job once and end the turn. After completion request each measured protocol's "
                    "check_ref and interpret its actual independent fits and coverage. The job_output_ref retains all raw checks.")
    elif kind in ("geometry_plan", "geometry_decision"):
        inputs["available_jobs"] = max(0, domain.budget_left(conn, run_id) - 1)
        if kind == "geometry_decision" and not inputs["available_jobs"]:
            inputs["available_protocols"] = []
        guidance = "The available measurement Job count leaves one additional real CPU Job for independent review."
    else:
        return True
    if guidance not in inputs["instruction"]:
        inputs["instruction"] += " " + guidance
    if inputs != store.loads(current, "input_json"):
        conn.execute("UPDATE tasks SET input_json=?,brief_ref=NULL,brief_sha256=NULL WHERE task_id=? AND status='READY'",
                     (ids.canonical_json(inputs), task["task_id"]))
    return True


def register_all(registry):
    baseline = registry.get("tl1a_geometry", "2")
    doc = {**baseline.document, "version": "3", "review_capability": {"id": review_jobs.CAPABILITY,
           "sha256": shipped.pin(review_jobs.CAPABILITY)}}
    bindings = {stage: node.fn for stage, node in baseline.nodes.items()}
    bindings["proposal"] = proposal
    flow = build(doc, bindings, stage_rules=baseline.stage_rules, completion_evidence=baseline.completion_evidence,
                 task_product_validator=v2.validate)
    flow.sources.update(baseline.sources)
    flow.sources.update(module_sources([review_jobs.build_worker_tools, register_all]))
    hooks = replace(geometry.hooks, research_start=research_start, prepare_dispatch=prepare_dispatch,
                    build_worker_tools=review_jobs.build_worker_tools)
    return [registry.register(flow, hooks=hooks)]
