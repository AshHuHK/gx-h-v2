"""Prepare a composed grid@2 assignment before its Root takes a lease."""
from dataclasses import dataclass
import math

from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import GxError
from gx_core.orchestration.dispatch_preparation import DispatchPreparation
from gx_core.p0 import intent, science, science_inputs, science_refresh, science_refresh_v2
from gx_core.persistence import store


def _basis(conn, run_id, task, round_index):
    # Intent versions are append-only. A review can be accepted after a newer
    # submission, so track the actual accepted set, irrespective of insert order.
    run = conn.execute(
        "SELECT status,phase,(SELECT MAX(rowid) FROM p0_intent_versions i WHERE i.run_id=r.run_id) "
        "FROM runs r WHERE run_id=?", (run_id,)).fetchone()
    sources = science_inputs.sources_for(conn, run_id, round_index)
    accepted = conn.execute(
        "SELECT s.submission_id FROM tasks t JOIN submissions s USING(task_id) "
        "WHERE t.run_id=? AND t.status='ACCEPTED' AND s.result='ACCEPTED' AND s.attempt=t.attempt "
        "AND json_extract(t.input_json,'$.output_kind')='science_assessment' "
        "AND json_extract(t.input_json,'$.round')=? ORDER BY s.submission_id", (run_id, round_index))
    return (task["task_id"], task["attempt"], task["input_json"], tuple(run),
            tuple((item["source_id"], item["artifact_ref"]) for item in sources), tuple(row[0] for row in accepted))


@dataclass(frozen=True)
class _Pause:
    reason: str
    evidence: dict | None = None


def _prepare_controls(conn, run_id, value):
    """Fill the frozen scorer's immutable locks before its owner registers the spec."""
    plugin = value.get("evaluator")
    if not plugin or "eval_spec_id" in value:
        return
    threshold = value["threshold"]
    science.require(type(threshold) in (int, float) and math.isfinite(threshold), "a finite threshold is required")
    science.require(isinstance(plugin, dict) and isinstance(plugin.get("package_digest"), str),
                    "a plugin-backed evaluator needs its complete installed package reference")
    # These are the identities of the pinned grid scorer. Its existing freeze_spec
    # remains responsible for verifying all three controls and their ledger entries.
    spec = {"domain": "synthetic fixture-grid@1", "rule": "at least one observation reaches threshold",
            "threshold": float(threshold), "evaluator": plugin, "implementation_sha": plugin["package_digest"]}
    digest = ids.sha256_of_obj(spec)
    if conn.execute("SELECT 1 FROM eval_specs WHERE eval_spec_id=? AND version=?",
                    (science._id("grid_score", run_id), digest)).fetchone():
        return
    controls = (("POSITIVE", math.nextafter(threshold, math.inf)), ("NEGATIVE", math.nextafter(threshold, -math.inf)),
                ("BOUNDARY", threshold))
    science.require(all(math.isfinite(point) for _, point in controls), "the cutoff must allow finite controls on both sides")
    for name, point in controls:
        ref = science._publish(conn, run_id, "controls", "score_control", {"observations": [{"score": point}]})
        science._score_artifact(conn, run_id, ref, science._id("case", run_id, digest, name), spec)


def _revise(conn, run_id, inputs, original, item):
    """Keep the original Job bytes, hypothesis IDs and retrospective criterion timing."""
    current = {"item_id": item["item_id"], "version": item["version"]}
    _prepare_controls(conn, run_id, item["value"])
    frozen = science.freeze_spec(conn, run_id, item["value"])
    threshold = science._spec(conn, frozen)["threshold"]
    paths = []
    with store.transaction(conn):
        for prior in original["paths"]:
            hypothesis_key = science._id("criterion", prior["hypothesis_id"], current)
            predicate = "at least one sampled score reaches" if prior["expected_reaches_threshold"] else "no sampled score reaches"
            path = {**prior, "previous_hypothesis_id": prior["hypothesis_id"], "hypothesis_key": hypothesis_key,
                    "prediction": f"For route {prior['id']}, {predicate} the revised cutoff {threshold:g}."}
            path["hypothesis_id"] = science.register_hypothesis(
                conn, run_id, {**path, "hypothesis_id": hypothesis_key}, "gx:changed_evaluation_criterion")
            paths.append(path)
    revision = {**original, "paths": paths, "evaluation_item": current, "evaluator": frozen, "threshold": threshold,
                "previous_proposal_ref": inputs["proposal_ref"], "evaluation_timing": "retrospective"}
    proposal = science._publish(conn, run_id, "reassessments", "research_proposal", revision)
    assets = ArtifactService(conn)
    routes = []
    for evidence in inputs["evidence_refs"]:
        for route in assets.read_json(evidence)["routes"]:
            row = dict(route)
            if row.get("eval_id"):
                previous = conn.execute("SELECT provenance_json FROM evaluations WHERE eval_id=?", (row["eval_id"],)).fetchone()
                origin = store.loads(previous, "provenance_json").get("original_evaluation_id") or row["eval_id"]
                try:
                    result = science.reevaluate_intent(conn, {"run_id": run_id, "object_type": "evaluation",
                        "object_id": origin, "item_id": item["item_id"], "item_version": item["version"]})
                except GxError as exc:
                    kind = "historical_only" if exc.details.get("reason") == "current_intent" else "measurement_failure"
                    row.update(eval_id=None, evaluation_ref=None, outcome_kind=kind, evaluation_error=str(exc))
                else:
                    row.update(result, outcome_kind="valid_scientific_outcome")
            routes.append(row)
    evidence = science._publish(conn, run_id, "reassessments", "research_evidence", {"proposal_ref": proposal, "routes": routes})
    return {**inputs, "proposal_ref": proposal, "evidence_refs": [evidence]}


def prepare_dispatch(conn, run_id, task):
    inputs = store.loads(task, "input_json")
    if not inputs.get("p0") or inputs.get("output_kind") not in ("science_assessment", "science_decision"):
        return True
    round_index = inputs.get("round", 0)
    basis = _basis(conn, run_id, task, round_index)

    def prepare():
        if conn.in_transaction:
            raise RuntimeError("grid assignment preparation requires a committed claim check")
        if science_refresh.removed_requirements(conn, run_id):
            return _Pause("The selected scientific objective or criterion was removed; supply a new research specification.")
        original = ArtifactService(conn).read_json(inputs["proposal_ref"])
        if pin := original.get("objective_item"):
            objective = intent.read_item(conn, run_id, pin["item_id"])
            if objective["status"] != "active" or objective["version"] != pin["version"]:
                return _Pause("The objective was replaced. A new research proposal is needed for the changed question.",
                              {"historical_proposal_ref": inputs["proposal_ref"], "objective_item": pin})
        updated = inputs
        if pin := original.get("evaluation_item"):
            item = intent.read_item(conn, run_id, pin["item_id"])
            if item["status"] != "active":
                return None
            if item["version"] != pin["version"]:
                updated = _revise(conn, run_id, inputs, original, item)
        updated = science_inputs.with_context(conn, run_id, updated)
        if inputs["output_kind"] == "science_decision":
            # Review expansion, binding its dependency and settling the named
            # independent submission are one owner operation, outside claim.
            with store.transaction(conn):
                fresh = conn.execute("SELECT * FROM tasks WHERE run_id=? AND task_id=? AND status='READY'",
                                     (run_id, task["task_id"])).fetchone()
                if not fresh or _basis(conn, run_id, fresh, round_index) != basis:
                    return None
                review = science_refresh_v2._replacement_review(conn, run_id, updated, fresh)
            if review is None:
                return None
            updated = {**updated, "assessment_ref": review["artifact_ref"]}
        return updated

    def apply(updated):
        if updated is None:
            return False
        if isinstance(updated, _Pause):
            store.transition(conn, machine="run", subject_id=run_id, to_state="PAUSED", actor="backend",
                             reason=updated.reason, payload=updated.evidence)
            return False
        if updated != inputs:
            conn.execute("UPDATE tasks SET input_json=?,brief_ref=NULL,brief_sha256=NULL WHERE task_id=? AND status='READY'",
                         (ids.canonical_json(updated), task["task_id"]))
        return True

    return DispatchPreparation(basis, prepare, apply)
