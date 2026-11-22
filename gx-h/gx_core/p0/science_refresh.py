"""Rebind a durable scientific task's inputs at an Intent change and preserve each prior brief."""
from types import SimpleNamespace

from ..artifacts import ArtifactService
from ..domain import ids
from ..errors import GxError
from ..orchestration import expansion
from ..persistence import store
from ..teams.service import backend_ctx
from . import intent, science


def objective_item(conn, run, selected=None):
    items = {k: v for k, v in intent.active_items(conn, run).items() if v["item_type"] == "objective"}
    if selected is None and items:
        science.require(len(items) == 1, "select the objective_item_id for this research workflow")
        selected = next(iter(items))
    return items.get(selected)


def removed_requirements(conn, run):
    deps = intent.snapshot(conn, run, "workflow", run)
    return [key for key, item in deps.items() if item["item_type"] in ("objective", "evaluation_spec")
            and item["status"] != "active"]


def bind_task(conn, run, task_id, proposal, stage):
    """Only the selected objective and evaluation actually enter these two task briefs."""
    roles = {}
    if pin := proposal.get("objective_item"):
        # A reviewer judges an already specified prediction. A planner chooses new
        # research against the objective, so replacement invalidates an active plan.
        roles[pin["item_id"]] = "execution" if stage == "decision" else "context"
    if pin := proposal.get("evaluation_item"):
        roles[pin["item_id"]] = "evaluation"
    return intent.bind_dependencies(conn, run, "task", task_id, roles)


def _new_review_inputs(conn, run, inputs, original):
    pin = original["evaluation_item"]
    item = intent.read_item(conn, run, pin["item_id"])
    science.require(item["status"] == "active", "the selected evaluation criterion has been removed")
    current = {"item_id": item["item_id"], "version": item["version"]}
    if current == pin:
        return inputs
    frozen = science.freeze_spec(conn, run, item["value"])
    threshold = science._spec(conn, frozen)["threshold"]
    revision = dict(original, evaluation_item=current, evaluator=frozen, threshold=threshold,
                    previous_proposal_ref=inputs["proposal_ref"], evaluation_timing="retrospective")
    paths = []
    for base in original["paths"]:
        path = dict(base)
        path["previous_hypothesis_id"] = path["hypothesis_id"]
        path["hypothesis_key"] = science._id("criterion", path["hypothesis_id"], current)
        predicate = "at least one sampled score reaches" if path["expected_reaches_threshold"] else "no sampled score reaches"
        path["prediction"] = f"For route {path['id']}, {predicate} the revised cutoff {threshold:g}."
        path["hypothesis_id"] = science.register_hypothesis(conn, run, {**path, "hypothesis_id":path["hypothesis_key"]},
                                                           "gx:changed_evaluation_criterion")
        paths.append(path)
    revision["paths"] = paths
    proposal_ref = science._publish(conn, run, "reassessments", "research_proposal", revision)
    artifacts = ArtifactService(conn)
    routes = []
    for ref in inputs["evidence_refs"]:
        for prior in artifacts.read_json(ref)["routes"]:
            row = dict(prior)
            if row.get("eval_id"):
                previous = conn.execute("SELECT provenance_json FROM evaluations WHERE eval_id=?",
                                        (row["eval_id"],)).fetchone()
                origin = store.loads(previous, "provenance_json").get("original_evaluation_id") or row["eval_id"]
                try:
                    result = science.reevaluate_intent(conn, {"run_id":run, "object_type":"evaluation",
                        "object_id":origin, "item_id":item["item_id"], "item_version":item["version"]})
                except GxError as exc:
                    kind = "historical_only" if exc.details.get("reason") == "current_intent" else "measurement_failure"
                    row.update(eval_id=None, evaluation_ref=None, outcome_kind=kind, evaluation_error=str(exc))
                else:
                    row.update(result, outcome_kind="valid_scientific_outcome")
            routes.append(row)
    evidence = science._publish(conn, run, "reassessments", "research_evidence",
                                {"proposal_ref":proposal_ref, "routes":routes})
    return {**inputs, "proposal_ref":proposal_ref, "evidence_refs":[evidence]}


def review_for(conn, run, ref):
    row = conn.execute("SELECT t.task_id,s.submission_id FROM tasks t JOIN submissions s USING(task_id)"
                       " WHERE t.run_id=? AND json_extract(t.input_json,'$.output_kind')='science_assessment' AND s.result='ACCEPTED' "
                       " AND s.attempt=t.attempt AND t.status='ACCEPTED'"
                       " AND EXISTS (SELECT 1 FROM json_each(s.artifact_refs_json) WHERE value=?)", (run, ref)).fetchone()
    science.require(row, "the decision must cite an accepted independent assessment in its run")
    return {**dict(row), "artifact_ref":ref}


def _replacement_review(conn, run, inputs, task):
    """Return an accepted review of these specific rescored outputs, or commission it once."""
    assets = ArtifactService(conn)
    old = review_for(conn, run, inputs["assessment_ref"])
    old_body = assets.read_json(old["artifact_ref"])
    if (old_body["proposal_ref"], old_body["evidence_refs"]) == (inputs["proposal_ref"], inputs["evidence_refs"]):
        return old
    original = conn.execute("SELECT input_json FROM tasks WHERE task_id=?", (old["task_id"],)).fetchone()
    updated = {**store.loads(original, "input_json"), "proposal_ref":inputs["proposal_ref"],
               "evidence_refs":inputs["evidence_refs"]}
    updated["instruction"] += " Review the retrospective criterion on the original measurements. Limit claims to this revised test."
    spec = {"kind":"research_round", "role":"evidence_reviewer", "stage":task["stage"], "bundle_kind":None,
            "input":updated, "required_artifacts":["science_assessment"], "acceptance":{"validate_workflow_product":True}}
    key = expansion.round_key(run, "assessment", inputs["round"], "criterion_change", inputs["proposal_ref"])
    expanded = expansion.expand(backend_ctx(conn), run, rule="review_changed_criterion", key=key,
                                trigger={"proposal_ref":inputs["proposal_ref"]}, stage=task["stage"],
                                round_index=inputs["round"], tasks=[spec])
    reviewer = expanded["task_ids"][0]
    bind_task(conn, run, reviewer, assets.read_json(inputs["proposal_ref"]), "assessment")
    conn.execute("INSERT OR IGNORE INTO task_deps(task_id,depends_on) VALUES(?,?)", (task["task_id"], reviewer))
    accepted = conn.execute("SELECT s.submission_id,s.artifact_refs_json FROM tasks t JOIN submissions s USING(task_id)"
                            " WHERE t.task_id=? AND t.status='ACCEPTED' AND s.result='ACCEPTED' AND s.attempt=t.attempt",
                            (reviewer,)).fetchone()
    if accepted is None:
        return None
    ref = next(r for r in store.loads(accepted,"artifact_refs_json") if assets.describe(r)["kind"] == "science_assessment")
    product = {"task_id":reviewer,"submission_id":accepted["submission_id"],"artifact_ref":ref}
    outputs = {"proposal":{"proposal_ref":inputs["proposal_ref"]}, "experiment":{"evidence_ref":inputs["evidence_refs"][0]}}
    science.settle_assessment(SimpleNamespace(conn=conn,run_id=run,round=inputs["round"],outputs=outputs), product)
    return product


def prepare_dispatch(conn, run, task):
    """Under the claim's write transaction, refresh a READY assignment or wait for its reviewer."""
    inputs = store.loads(task, "input_json")
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
    if inputs["output_kind"] == "science_decision":
        review = _replacement_review(conn, run, updated, task)
        if review is None:
            return False
        updated = {**updated, "assessment_ref":review["artifact_ref"]}
    if updated != inputs:
        conn.execute("UPDATE tasks SET input_json=?,brief_ref=NULL,brief_sha256=NULL WHERE task_id=? AND status='READY'",
                     (ids.canonical_json(updated), task["task_id"]))
    return True
