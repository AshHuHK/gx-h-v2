"""Research assignments for science_grid@2 and dispatch to the run's pinned workflow."""
from types import SimpleNamespace

from ..artifacts import ArtifactService
from ..domain import ids
from ..orchestration import expansion
from ..persistence import store
from ..teams.service import backend_ctx
from . import intent, science_inputs, science_v2
from .science_refresh import _new_review_inputs, removed_requirements, review_for


def bind_task(conn, run, task_id, proposal, stage):
    """Bind the task to its scientific requirements, including later updates to its role."""
    roles = {}
    if pin := proposal.get("objective_item"):
        # A reviewer judges an already specified prediction. A planner chooses new
        # research against the objective, so replacement invalidates an active plan.
        roles[pin["item_id"]] = "execution" if stage == "decision" else "context"
    if pin := proposal.get("evaluation_item"):
        roles[pin["item_id"]] = "evaluation"
    role = "decision" if stage == "decision" else "review"
    inputs = store.loads(conn.execute("SELECT input_json FROM tasks WHERE task_id=?", (task_id,)).fetchone(), "input_json", {})
    if not science_inputs.role_of(inputs):
        conn.execute("UPDATE tasks SET input_json=json_set(input_json,'$.science_role',?) WHERE task_id=?", (role, task_id))
    intent.bind_dependencies(conn, run, "task", task_id, roles)
    return science_inputs.bind_task(conn, run, task_id, role)


def _replacement_review(conn, run, inputs, task):
    """Return an accepted review of these specific rescored outputs, or commission it once."""
    assets = ArtifactService(conn)
    old = review_for(conn, run, inputs["assessment_ref"])
    for candidate in science_inputs.accepted_products(conn, run, "review", inputs["round"]):
        body = candidate["body"]
        if (body.get("proposal_ref") != inputs["proposal_ref"] or body.get("evidence_refs") != inputs["evidence_refs"]
                or not science_inputs.review_covers(conn, run, inputs["round"], body)):
            continue
        review = {k: candidate[k] for k in ("task_id", "submission_id", "artifact_ref")}
        if review != old:
            _settle_replacement(conn, run, inputs, review)
        return review
    original = conn.execute("SELECT input_json FROM tasks WHERE task_id=?", (old["task_id"],)).fetchone()
    updated = science_inputs.with_context(conn, run, {**store.loads(original, "input_json"),
            "proposal_ref": inputs["proposal_ref"], "evidence_refs": inputs["evidence_refs"]})
    updated["instruction"] += (" Reassess the specified observations against the current criterion and supplemental"
                               " sources. State which original findings remain justified and why.")
    updated["previous_assessment_ref"] = old["artifact_ref"]
    spec = {"kind":"research_round", "role":"evidence_reviewer", "stage":task["stage"], "bundle_kind":None,
            "input":updated, "required_artifacts":["science_assessment"], "acceptance":{"validate_workflow_product":True}}
    identity = ids.sha256_of_obj([inputs["proposal_ref"], inputs["evidence_refs"], updated["supplement_sources"]])
    key = expansion.round_key(run, "assessment", inputs["round"], "updated_evidence", identity)
    expanded = expansion.replay(conn, key) or expansion.expand(backend_ctx(conn), run,
                rule="review_updated_evidence", key=key, trigger={"previous_assessment_ref": old["artifact_ref"],
                "evidence_refs": inputs["evidence_refs"], "supplement_sources": updated["supplement_sources"]},
                stage=task["stage"], round_index=inputs["round"], tasks=[spec])
    reviewer = expanded["task_ids"][0]
    bind_task(conn, run, reviewer, assets.read_json(inputs["proposal_ref"]), "assessment")
    conn.execute("INSERT OR IGNORE INTO task_deps(task_id,depends_on) VALUES(?,?)", (task["task_id"], reviewer))
    return None


def _settle_replacement(conn, run, inputs, product):
    outputs = {"proposal": {"proposal_ref": inputs["proposal_ref"]},
               "experiment": {"evidence_ref": inputs["evidence_refs"][0]}}
    science_v2.settle_assessment(SimpleNamespace(conn=conn, run_id=run, round=inputs["round"], outputs=outputs), product)


def prepare_dispatch(conn, run, task):
    """Under the claim's write transaction, refresh a READY assignment or wait for its reviewer."""
    inputs = store.loads(task, "input_json")
    if inputs.get("science_domain") == "tl1a_geometry":
        from workflows.tl1a_geometry.nodes import prepare_dispatch as prepare_geometry
        return prepare_geometry(conn, run, task)
    workflow = conn.execute("SELECT workflow_id,workflow_version FROM runs WHERE run_id=?", (run,)).fetchone()
    if workflow["workflow_id"] == "science_grid" and workflow["workflow_version"] == "1":
        from .science_refresh import prepare_dispatch as prepare_original
        return prepare_original(conn, run, task)
    if not inputs.get("p0") or inputs.get("output_kind") not in ("science_assessment", "science_decision"):
        return True
    assets = ArtifactService(conn)
    original = assets.read_json(inputs["proposal_ref"])
    if removed_requirements(conn, run):
        store.transition(conn, machine="run", subject_id=run, to_state="PAUSED", actor="backend",
                         reason="The selected scientific objective or criterion was removed; supply a new research specification.")
        return False
    if pin := original.get("objective_item"):
        obj = intent.read_item(conn, run, pin["item_id"])
        if obj["status"] != "active" or obj["version"] != pin["version"]:
            store.transition(conn, machine="run", subject_id=run, to_state="PAUSED", actor="backend",
                             reason="The objective was replaced. A new research proposal is needed for the changed question.",
                             payload={"historical_proposal_ref":inputs["proposal_ref"],"objective_item":pin})
            return False
    updated = _new_review_inputs(conn, run, inputs, original) if original.get("evaluation_item") else inputs
    updated = science_inputs.with_context(conn, run, updated)
    if inputs["output_kind"] == "science_decision":
        review = _replacement_review(conn, run, updated, task)
        if review is None:
            return False
        updated = {**updated, "assessment_ref":review["artifact_ref"]}
    if updated != inputs:
        conn.execute("UPDATE tasks SET input_json=?,brief_ref=NULL,brief_sha256=NULL WHERE task_id=? AND status='READY'",
                     (ids.canonical_json(updated), task["task_id"]))
    return True
