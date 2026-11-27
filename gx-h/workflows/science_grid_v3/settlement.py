"""Record scientific outcomes with the evaluator or independent reviewer that produced them."""
from gx_core.artifacts import ArtifactService
from gx_core.domain import clock, ids
from gx_core.errors import GxError
from gx_core.p0 import intent, manifest, science, science_inputs, science_refresh
from gx_core.persistence import store
from . import products


def assessment_owner(conn, run, ref, round_index):
    asset = ArtifactService(conn).describe(ref)
    if asset["run_id"] != run or asset["kind"] != "science_assessment":
        products.semantic("The assessment must be an artifact of this research run.")
    if asset["task_id"] is not None:
        return science_refresh.review_for(conn, run, ref)
    if asset["namespace"] != f"{run}/science/automatic/r{round_index}":
        products.semantic("The numerical assessment did not come from the workflow evaluator.")
    return {"artifact_ref": ref, "task_id": None, "submission_id": None}


def settle(conn, run, round_index, inputs, result):
    read = ArtifactService(conn).read_json
    review = read(result["artifact_ref"])
    proposal = read(inputs["proposal_ref"])
    hypotheses = {p["id"]: p for p in proposal["paths"]}
    findings = {f["path_id"]: f for f in review["findings"]}
    routes = [route for ref in inputs["evidence_refs"] for route in read(ref)["routes"]]
    submitted = science._reviewed_submission(conn, result["task_id"]) if result["task_id"] else None
    if submitted and submitted["submission_id"] != result["submission_id"]:
        products.semantic("The assessment does not match the accepted review submission.")
    with store.transaction(conn):
        for route in routes:
            path_id = route["path_id"]
            sid = science._id("settlement", run, result["artifact_ref"], path_id)
            if conn.execute("SELECT 1 FROM science_settlements WHERE settlement_id=?", (sid,)).fetchone():
                continue
            finding, path, now = findings[path_id], hypotheses[path_id], clock.now_iso()
            kind, reason = route["outcome_kind"], finding["reason"]
            evidence = direction = manifest_hash = None
            if kind == "historical_only":
                continue
            if kind == "valid_scientific_outcome" and finding["measurement_valid"] is False:
                kind, reason = "measurement_failure", finding["measurement_note"]
            if kind == "valid_scientific_outcome":
                if finding["supported"] is None:
                    continue
                try:
                    proof = science._current(conn, run, route, proposal["evaluation_item"], proposal.get("objective_item"))
                except GxError as exc:
                    if submitted is None:
                        # The enclosing evaluator transaction must also discard its
                        # computed positive/negative assessment if Job authority fails.
                        raise
                    if exc.details.get("reason") == "current_intent":
                        continue
                    kind, reason = "measurement_failure", f"Unusable measurement: {exc}"
                else:
                    manifest_hash = proof["manifest_hash"]
                    evidence = science._id("gxv", sid)
                    direction = "supports" if finding["supported"] else "refutes"
                    claim = (f"At least one sampled score on {path_id} reached the frozen cutoff {proposal['threshold']:g}."
                             if finding["reaches_threshold"] else
                             f"No sampled score on {path_id} reached the frozen cutoff {proposal['threshold']:g}.")
                    locator = {"job_id": route["job_id"], "proposal_ref": inputs["proposal_ref"],
                               "observations_ref": route["observations_ref"], "evaluation_ref": route["evaluation_ref"],
                               "assessment_ref": result["artifact_ref"], "evidence_refs": inputs["evidence_refs"],
                               "contract_digest": review["contract_digest"], "assessment_mode": review["assessment_mode"],
                               "source_reviews": review["source_reviews"], "interpretation": reason}
                    status = "REVIEWED" if submitted else "EXTRACTED"
                    confidence = ("Frozen evaluator and independent interpretation." if submitted else
                                  "Frozen grid evaluator and original observations.")
                    conn.execute("INSERT INTO evidence(evidence_id,namespace,run_id,status,claim,claim_grain,"
                                 "evidence_type,source_locator_json,method,limitations,review_status,confidence_basis,"
                                 "evidence_level,origin,created_at,updated_at) VALUES(?,?,?,?,?,'FINDING','COMPUTATIONAL',"
                                 "?,'fixture-grid@1',?,?,?,'EXECUTION_OBSERVED','JOB_OUTPUT',?,?)",
                                 (evidence, run, run, status, claim, ids.canonical_json(locator),
                                  finding["risk"], status, confidence, now, now))
                    if submitted:
                        conn.execute("INSERT INTO evidence_reviews(review_id,evidence_id,task_id,lease_id,principal,"
                                     "worker_observation_id,independence_level,verdict,note,created_at) "
                                     "VALUES(?,?,?,?,?,?,'FRESH_CONTEXT_SAME_HOST','SUPPORTED',?,?)",
                                     (science._id("gxrv", sid), evidence, result["task_id"], submitted["lease_id"],
                                      submitted["principal"], submitted["worker_observation_id"], reason, now))
            conn.execute("INSERT INTO science_settlements(settlement_id,run_id,round_index,path_id,hypothesis_id,"
                         "task_id,attempt,submission_id,job_id,kind,reason,eval_id,artifact_ref,manifest_hash,evidence_id,"
                         "direction,created_at,assessment_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (sid, run, round_index, path_id, path["hypothesis_id"], result["task_id"],
                          submitted["attempt"] if submitted else None, result["submission_id"], route["job_id"], kind,
                          reason, route.get("eval_id"), route.get("observations_ref"), manifest_hash, evidence,
                          direction, now, result["artifact_ref"]))


def current_evidence(conn, run, *, verify_bytes=True):
    """List results still covered by the current source set, criterion and Job authority."""
    rows = conn.execute("SELECT * FROM science_settlements WHERE run_id=? AND evidence_id IS NOT NULL "
                        "AND assessment_ref IS NOT NULL ORDER BY round_index,path_id,rowid", (run,))
    read = ArtifactService(conn).read_json
    reviews, proposals, out = {}, {}, []
    for row in rows:
        ref = row["assessment_ref"]
        if ref not in reviews:
            review = read(ref)
            reviews[ref] = (review if science_inputs.review_covers(conn, run, row["round_index"], review) else None)
        if not (review := reviews[ref]):
            continue
        pref = review["proposal_ref"]
        if pref not in proposals:
            proposals[pref] = read(pref)
        proposal = proposals[pref]
        try:
            pins = (proposal["evaluation_item"], proposal.get("objective_item"))
            if verify_bytes:
                science._current(conn, run, {"job_id": row["job_id"], "observations_ref": row["artifact_ref"]}, *pins)
            else:
                # Operator polling reads durable proof and the active requirements. The
                # scientific writer verified the bytes when recording or acting on the result.
                manifest.require_current(conn, row["job_id"])
                active = [intent.read_item(conn, run, p["item_id"]) for p in pins if p]
                if any(item["status"] != "active" or item["version"] != pin["version"]
                       for item, pin in zip(active, filter(None, pins))):
                    continue
        except GxError:
            continue
        out.append(dict(row))
    return out
