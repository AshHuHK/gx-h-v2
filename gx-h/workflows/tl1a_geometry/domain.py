"""Scientific inputs, prospective evaluation and evidence for deposited TL1A coordinates."""
import json
import math
from pathlib import Path

from adapters.scientific.tl1a_geometry import worker
from gx_core.artifacts import ArtifactService
from gx_core.domain import clock, ids
from gx_core.errors import SchemaInvalid
from gx_core.p0 import intent, manifest, science
from gx_core.persistence import store

DATA = Path(worker.__file__).parent / "data"
PROVENANCE = json.loads((DATA / "provenance.json").read_text())
TEMPLATE = json.loads((DATA / "criteria.json").read_text())
SECTIONS = ("coverage", "comparison", "reproduction")
PROTOCOLS = tuple(TEMPLATE["protocols"])
LIMITS = TEMPLATE["interpretation"]["limits"]
require = science.require
JSON = ids.canonical_json


def coordinate_input():
    return {"role": "structure", "path": str(DATA / PROVENANCE["filename"]), "sha256": PROVENANCE["sha256"]}


def source_value():
    return {k: PROVENANCE[k] for k in ("pdb_id", "source_revision", "sha256", "deposition")}


def user_items(spec):
    require(spec.get("source") == PROVENANCE["pdb_id"], "this coordinate workflow requires the deposited 2RE9 source")
    require(type(spec.get("max_jobs")) is int and spec["max_jobs"] >= 3,
            "the quality inventory and two distinct geometry measurements require at least three Jobs")
    require("proposal" not in spec, "the scientific planner supplies the prospective proposal on its GX task")
    items = [{"item_id": "coordinates", "item_type": "data_source", "expected_version": 0, "value": source_value()},
            {"item_id": "job-limit", "item_type": "resource_limit", "expected_version": 0,
             "value": {"resource": "job_submissions", "max": spec["max_jobs"]}}]
    if "criteria" in spec:
        doc = controls(spec["criteria"])
        items.append({"item_id": "evaluation", "item_type": "evaluation_spec", "expected_version": 0,
                      "value": {"method": doc["method"], **{k: doc[k] for k in SECTIONS}}})
    return items


def source_artifact(conn, run):
    row = conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run,)).fetchone()
    files = [f for f in store.loads(row, "inputs_json")["files"] if f["role"] == "structure"]
    require(len(files) == 1 and files[0]["sha256"] == PROVENANCE["sha256"] and files[0]["artifact_ref"],
            "use one immutable deposited 2RE9 coordinate file")
    return files[0]["artifact_ref"]


def publish(conn, run, kind, body, **kwargs):
    return science._publish(conn, run, "tl1a/" + kind, kind, body, **kwargs)


def pin(conn, run, name):
    item = intent.read_item(conn, run, name)
    return {"item_id": name, "version": item["version"], "sha256": item["sha256"], "status": item["status"]}


def intent_criteria(conn, run, value):
    if "eval_spec_id" in value:
        row = conn.execute("SELECT spec_json FROM eval_specs WHERE namespace=? AND eval_spec_id=? "
                           "AND version=? AND status='FROZEN'", (run, value["eval_spec_id"], str(value["version"]))).fetchone()
        require(row, "select a frozen evaluation from this coordinate project")
        value = store.loads(row, "spec_json")
    require(value.get("method") == TEMPLATE["method"], "specify the exact tl1a-geometry@1 criteria")
    sections = {k: value.get(k) for k in SECTIONS}
    controls(sections)
    return sections


def criterion_item(conn, run):
    active = intent.active_items(conn, run, "evaluation_spec")
    require(len(active) <= 1, "the study accepts one active geometry evaluation specification")
    return next(iter(active.values()), None)


def criterion_pin(conn, run):
    item = criterion_item(conn, run)
    return pin(conn, run, item["item_id"]) if item else None


def matches_intent(conn, run, old):
    return (old.get("objective_item") == pin(conn, run, "objective") and
            old.get("source_item") == pin(conn, run, "coordinates") and old.get("evaluation_item") == criterion_pin(conn, run))


def active_inputs(conn, run):
    objective = intent.read_item(conn, run, "objective")
    source = intent.read_item(conn, run, "coordinates")
    require(objective["status"] == "active", "the scientific planner needs an active objective")
    require(source["status"] == "active" and source["value"] == {**source_value(), "artifact_ref": source_artifact(conn, run)},
            "the selected method needs the original 2RE9 coordinate source")
    item = criterion_item(conn, run)
    return {"objective": objective["value"], "objective_item": pin(conn, run, "objective"),
            "source_item": pin(conn, run, "coordinates"), "evaluation_item": criterion_pin(conn, run),
            "formal_criteria": intent_criteria(conn, run, item["value"]) if item else None}


def budget_left(conn, run):
    from gx_core.p0.intent_budget import current_caps
    project = conn.execute("SELECT budget_ref FROM runs WHERE run_id=?", (run,)).fetchone()
    ledger = store.budget_state(conn, project["budget_ref"])
    remaining = ledger["limits"]["job_submissions"] - ledger["spent"].get("job_submissions", 0) - ledger["reserved"].get("job_submissions", 0)
    caps = [row["available_for_new"] for row in current_caps(conn, run) if row["resource"] == "job_submissions"]
    return max(0, int(min([remaining, *caps])))


def controls(product):
    require(isinstance(product, dict) and set(product) == set(SECTIONS),
            "criteria must supply coverage, comparison and reproduction")
    for name in SECTIONS:
        require(isinstance(product[name], dict) and set(product[name]) == set(TEMPLATE[name]),
                f"supply exactly the documented {name} criteria")
    doc = {**TEMPLATE, **product}
    try:
        worker.validate_criteria(doc)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise SchemaInvalid(str(exc)) from exc
    return doc


def text(value):
    return isinstance(value, str) and bool(value.strip())


def validate_plan(inputs, body):
    require(body.get("source_ref") == inputs["structure_ref"] and text(body.get("rationale")),
            "cite the frozen coordinate source and justify the plan")
    require("intent" not in body and "intent_items" not in body and "action" not in body,
            "a scientific proposal must stay within the current user Intent")
    protocols = body.get("protocols")
    require(isinstance(protocols, list) and all(isinstance(p, str) and p in PROTOCOLS for p in protocols)
            and 2 <= len(protocols) == len(set(protocols)),
            "choose at least two distinct atom-selection protocols")
    criteria = controls(body.get("criteria"))
    supplied = inputs.get("formal_criteria")
    require(supplied is None or all(criteria[k] == supplied[k] for k in SECTIONS),
            "the plan must use the user's exact active geometric criteria, including coverage and numerical tolerances")
    reusable = inputs.get("reusable_protocols", [])
    require(len(set(protocols) - set(reusable)) <= inputs["available_jobs"], "the new selections exceed available Jobs")
    hypotheses = body.get("hypotheses")
    require(isinstance(hypotheses, list) and len(hypotheses) >= 2 and all(
        isinstance(h, dict) and text(h.get("id")) and text(h.get("prediction")) for h in hypotheses)
        and len({h["id"] for h in hypotheses}) == len({h["prediction"] for h in hypotheses}) == len(hypotheses)
        and {h.get("expected") for h in hypotheses} == {"stable", "sensitive"},
        "write distinguishable stable and sensitive predictions with different IDs")
    return criteria


def qualification(criteria):
    """Run the published cross-protocol classifier on positive, boundary, adverse and missing-coverage controls."""
    from adapters.scientific.tl1a_geometry.review import compare_protocols
    member = {"schema": "gx.tl1a.geometry/1", "method": "tl1a-geometry@1", "input_sha256": criteria["source_sha256"],
              "status": "measured", "mapping": {"selected_residues": criteria["coverage"]["min_residues"],
              "selected_fraction_of_observed_union_residues": criteria["coverage"]["min_fraction"]}}
    def observation(protocol, rmsd, status="measured"):
        return {**member, "protocol": protocol, "status": status, "pairs": [
            {"fixed_chain": a, "moving_chain": b, "rotation_angle_deg": 0.0, "fitted_rmsd_angstrom": rmsd}
            for a, b in criteria["mapping"]["pairs_fixed_moving"]]}
    boundary = criteria["comparison"]["max_rmsd_delta_angstrom"]
    adverse = math.nextafter(boundary, math.inf)
    require(math.isfinite(adverse), "choose a distinguishable finite RMSD threshold")
    controls = []
    for name, change, coverage, expected in (("identical", 0, "measured", "stable"),
            ("boundary", boundary, "measured", "stable"), ("outside", adverse, "measured", "sensitive"),
            ("insufficient", 0, "insufficient", "insufficient")):
        observations = [observation("common_backbone", 0), observation("common_ca", change, coverage)]
        verdict = compare_protocols(observations, criteria)["status"]
        require(verdict == expected, f"evaluation control {name} returned {verdict}, expected {expected}")
        controls.append({"name": name, "observations": observations, "expected": expected, "observed": verdict})
    return controls


def register_claims(conn, run, hypotheses, criteria_ref, structure_ref, objective_item, protocols, author):
    claims = []
    now = clock.now_iso()
    for hypothesis in hypotheses:
        claim = {k: hypothesis[k] for k in ("id", "prediction", "expected")}
        document = {**claim, "criteria_ref": criteria_ref, "source_ref": structure_ref,
                    "objective_item": objective_item, "protocols": protocols}
        if previous := hypothesis.get("hypothesis_id"):
            document["prior_hypothesis_id"] = previous
        hid = science._id("geometry_h", run, document)
        conn.execute("INSERT OR IGNORE INTO hypotheses VALUES(?,?,?,?,?)", (hid, run, 1, author, now))
        conn.execute("INSERT OR IGNORE INTO hypothesis_revisions(hypothesis_id,revision,document_json,sha256,"
                     "author_principal,created_at) VALUES(?,1,?,?,?,?)", (hid, JSON(document), ids.sha256_of_obj(document),
                                                                          author, now))
        claims.append({**claim, "hypothesis_id": hid})
    return claims


def freeze_criteria(conn, run, criteria, rationale, author, task_id=None):
    digest = ids.sha256_of_obj(criteria)
    frozen = {"id": science._id("geometry_spec", run), "version": digest, "spec_hash": digest}
    ref = publish(conn, run, "geometry_criteria", criteria)
    if conn.execute("SELECT 1 FROM eval_specs WHERE eval_spec_id=? AND version=?", (frozen["id"], digest)).fetchone():
        return frozen, ref
    now = clock.now_iso()
    control_ref = publish(conn, run, "geometry_evaluator_controls", {"type": "analytic software controls",
                          "cases": qualification(criteria), "criteria_ref": ref})
    conn.execute("INSERT OR IGNORE INTO eval_specs(eval_spec_id,version,status,frozen_sha,objective,applicable_domain,"
                 "spec_json,implementation_ref,controls_json,thresholds_json,threshold_basis,limitations,author_principal,"
                 "author_task_id,namespace,created_at,frozen_at) VALUES(?,?,'FROZEN',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (frozen["id"], digest, digest, criteria["scope"], criteria["domain"], JSON(criteria), ref,
                  JSON([control_ref]), JSON({k: criteria[k] for k in SECTIONS}), rationale, LIMITS, author, task_id, run, now, now))
    return frozen, ref


def freeze(conn, run, result, plan, inputs):
    criteria = validate_plan(inputs, plan)
    with store.transaction(conn):
        author = science._reviewed_submission(conn, result["task_id"])["principal"]
        frozen, criteria_ref = freeze_criteria(conn, run, criteria, plan["rationale"], author, result["task_id"])
        hypotheses = register_claims(conn, run, plan["hypotheses"], criteria_ref, inputs["structure_ref"],
                                     inputs["objective_item"], plan["protocols"], author)
        frozen_plan = {k: inputs[k] for k in ("structure_ref", "quality_summary_ref", "mask_ref", "objective",
                                              "objective_item", "source_item", "evaluation_item", "formal_criteria")}
        frozen_plan.update(author_principal=author, planner_submission_id=result["submission_id"],
                           plan_ref=result["artifact_ref"], hypotheses=hypotheses, protocols=plan["protocols"],
                           evaluator=frozen, criteria_ref=criteria_ref, rationale=plan["rationale"])
        frozen_plan["reused_protocols"] = [p for p in plan["protocols"] if p in inputs.get("reusable_protocols", [])]
        return frozen_plan


def evaluate(conn, run, round_index, proposal_ref, routes, *, origin=None):
    from adapters.scientific.tl1a_geometry.review import compare_protocols
    assets = ArtifactService(conn)
    plan = assets.read_json(proposal_ref)
    criteria = assets.read_json(plan["criteria_ref"])
    original = [assets.read_json(r.get("rescored_ref") or r["observations_ref"]) for r in routes if r.get("observations_ref")]
    comparison = compare_protocols(original, criteria) if len(original) >= 2 else {
        "status": "insufficient", "reason": "Fewer than two measurable protocols.", "comparisons": []}
    if any(r["job_status"] != "SUCCEEDED" or r.get("measurement_error") for r in routes):
        comparison = {**comparison, "measurement_comparison": comparison["status"], "status": "insufficient",
                      "reason": "At least one required Job did not yield usable measurements."}
    raw_refs = [r["observations_ref"] for r in routes if r.get("observations_ref")]
    refs = [r[k] for r in routes for k in ("rescored_ref", "check_ref") if r.get(k)]
    eid = science._id("geometry_e", run, round_index, proposal_ref, raw_refs, refs)
    doc = {"proposal_ref": proposal_ref, "original_evaluation_id": origin, "round": round_index,
           "review_status": "awaiting_independent_review", "structure_ref": plan["structure_ref"], "criteria_ref": plan["criteria_ref"],
           "eval_id": eid, "routes": routes, "comparison": comparison, "scope": LIMITS}
    ref = publish(conn, run, "geometry_evidence", doc)
    frozen = plan["evaluator"]
    conn.execute("INSERT OR IGNORE INTO evaluations(eval_id,run_id,eval_spec_id,eval_spec_version,frozen_sha,"
                 "purpose,artifact_refs_json,layer,verdict,metrics_json,provenance_json,created_at)"
                 " VALUES(?,?,?,?,?,'CANDIDATE',?,3,?,?,?,?)", (eid, run, frozen["id"], frozen["version"],
                 frozen["spec_hash"], JSON([*raw_refs, *refs, ref]), comparison["status"].upper(), JSON(comparison),
                 JSON({"structure_ref": plan["structure_ref"], "criteria_ref": plan["criteria_ref"],
                       "proposal_ref": proposal_ref, "original_evaluation_id": origin, "job_ids": [r["job_id"] for r in routes],
                       "input_ref": raw_refs[0] if raw_refs else None, "intent_item": plan.get("evaluation_item")}), clock.now_iso()))
    if not origin and (pin := plan.get("evaluation_item")):
        intent.bind_evaluation(conn, run, eid, pin["item_id"], pin["version"])
    return ref


def inspect_measurement(conn, run, plan, route):
    """Bind original measurements to their execution. If only criteria changed, reevaluate those same bytes."""
    assets = ArtifactService(conn)
    observed = route["observations_ref"]
    proof = manifest.validate_evidence(conn, run_id=run, job_id=route["job_id"], artifact_ref=observed)
    admission = manifest.read_manifest(conn, route["job_id"])["admission"]
    selected = {i["role"]: i["artifact_ref"] for i in admission["inputs"]}
    raw = assets.read_json(observed)
    require(selected["structure"] == plan["structure_ref"] and selected["selection"] == plan["mask_ref"] and
            raw["protocol"] == route["protocol"] and raw["input_sha256"] == assets.describe(plan["structure_ref"])["sha256"]
            and raw["quality_sha256"] == assets.describe(plan["mask_ref"])["sha256"] and
            raw["criteria_spec_sha256"] == ids.sha256_of_obj(assets.read_json(selected["criteria"])),
            "the original coordinates, masks, protocol and criteria must agree with the Job manifest")
    facts = {"manifest_hash": proof["manifest_hash"], "executed_criteria_ref": selected["criteria"]}
    if selected["criteria"] == plan["criteria_ref"]:
        return facts
    criteria = assets.read_json(plan["criteria_ref"])
    minimum = criteria["coverage"]
    sufficient = (len(raw["pairs"]) == len(criteria["mapping"]["pairs_fixed_moving"]) and
                  raw["mapping"]["selected_residues"] >= minimum["min_residues"] and
                  raw["mapping"]["selected_fraction_of_observed_union_residues"] >= minimum["min_fraction"])
    projection = {**raw, "status": "measured" if sufficient else "insufficient",
                  "issues": [] if sufficient else ["the fitted pairs or coverage fall below the revised criteria"],
                  "original_ref": observed, "original_criteria_ref": selected["criteria"], "evaluation_timing": "retrospective",
                  "criteria_spec_sha256": plan["evaluator"]["spec_hash"]}
    return {**facts, "rescored_ref": publish(conn, run, "geometry_rescored_measurement", projection)}


def reevaluate_intent(conn, payload):
    """Recompute a changed human criterion from its original observations with no new coordinate Job."""
    run = payload["run_id"]
    original = conn.execute("SELECT * FROM evaluations WHERE eval_id=? AND run_id=? AND purpose='CANDIDATE'",
                            (payload["object_id"], run)).fetchone()
    require(original and payload["object_type"] == "evaluation", "unknown original coordinate evaluation")
    provenance = store.loads(original, "provenance_json")
    require(provenance.get("intent_item") and provenance["intent_item"]["item_id"] == payload["item_id"],
            "the measurement must depend on the changed criterion")
    item = intent.read_item(conn, run, payload["item_id"], payload["item_version"])
    criteria = controls(intent_criteria(conn, run, item["value"]))
    assets = ArtifactService(conn)
    before = assets.read_json(provenance["proposal_ref"])
    old = assets.read_json(store.loads(original, "artifact_refs_json")[-1])
    with store.transaction(conn):
        frozen, ref = freeze_criteria(conn, run, criteria, "Human-committed revised evaluation", "gx:operator-intent")
    plan = {**before, "evaluator": frozen, "criteria_ref": ref, "formal_criteria": {k: criteria[k] for k in SECTIONS},
            "evaluation_item": {k: item[k] for k in ("item_id", "version", "sha256", "status")},
            "previous_proposal_ref": provenance["proposal_ref"], "evaluation_timing": "retrospective"}
    plan_ref = publish(conn, run, "geometry_retrospective_plan", plan)
    routes = []
    for raw in old["routes"]:
        route = {k: raw[k] for k in ("protocol", "job_id", "job_status", "observations_ref", "failure")}
        if route["observations_ref"]:
            route.update(inspect_measurement(conn, run, plan, route))
        routes.append(route)
    return evaluate(conn, run, old["round"], plan_ref, routes, origin=original["eval_id"])


def settle(conn, run, round_index, result, inputs):
    assets = ArtifactService(conn)
    proposal = assets.read_json(inputs["proposal_ref"])
    evidence = assets.read_json(inputs["evidence_ref"])
    review = assets.read_json(result["artifact_ref"])
    with store.transaction(conn):
        author = science._reviewed_submission(conn, result["task_id"])
        now = clock.now_iso()
        if not matches_intent(conn, run, proposal):
            return
        proofs = [manifest.validate_evidence(conn, run_id=run, job_id=r["job_id"], artifact_ref=r["observations_ref"])
                  for r in evidence["routes"] if r.get("observations_ref") and not r.get("measurement_error")]
        for hypothesis in proposal["hypotheses"]:
            name = hypothesis["id"]
            sid = science._id("geometry_settlement", run, author["submission_id"], name)
            if conn.execute("SELECT 1 FROM science_settlements WHERE settlement_id=?", (sid,)).fetchone():
                continue
            judgment = review["findings"][name]
            evidence_id = direction = None
            reason = judgment["reason"]
            status = review["status"]
            if status == "insufficient":
                kind = ("infrastructure_failure" if any(r["job_status"] != "SUCCEEDED" for r in evidence["routes"])
                        else "measurement_failure")
            else:
                kind = "valid_scientific_outcome"
                evidence_id = science._id("geometry_v", sid)
                direction = "supports" if judgment["supported"] else "refutes"
                locator = {"structure_ref": proposal["structure_ref"], "criteria_ref": proposal["criteria_ref"],
                           "evaluation_ref": inputs["evidence_ref"], "review_ref": result["artifact_ref"],
                           "jobs": proofs, "recalculation_refs": review["checks"]}
                conn.execute("INSERT INTO evidence(evidence_id,namespace,run_id,status,claim,claim_grain,evidence_type,"
                             "source_locator_json,method,limitations,review_status,confidence_basis,evidence_level,origin,"
                             "created_at,updated_at) VALUES(?,?,?,'REVIEWED',?,'FINDING','COMPUTATIONAL',?,?,?,'REVIEWED',"
                             "'Independent coordinates and frozen tolerances','EXECUTION_OBSERVED','JOB_OUTPUT',?,?)",
                             (evidence_id, run, run, reason, JSON(locator), "tl1a-geometry@1", LIMITS, now, now))
                conn.execute("INSERT INTO evidence_reviews(review_id,evidence_id,task_id,lease_id,principal,"
                             "worker_observation_id,independence_level,verdict,note,created_at) VALUES(?,?,?,?,?,?,'FRESH_CONTEXT_SAME_HOST',"
                             "'SUPPORTED',?,?)", (science._id("geometry_rv", sid), evidence_id, result["task_id"], author["lease_id"],
                             author["principal"], author["worker_observation_id"], reason, now))
            anchor = (proofs or [{}])[0]
            conn.execute("INSERT INTO science_settlements(settlement_id,run_id,round_index,path_id,hypothesis_id,task_id,"
                         "attempt,submission_id,job_id,kind,reason,eval_id,artifact_ref,manifest_hash,evidence_id,direction,"
                         "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (sid, run, round_index, name,
                         hypothesis["hypothesis_id"], result["task_id"], author["attempt"], author["submission_id"],
                         anchor.get("job_id"), kind, reason, evidence["eval_id"], anchor.get("artifact_ref"),
                         anchor.get("manifest_hash"), evidence_id, direction, now))
