"""Source-aware settlement for science_grid@2 on the original score and measurement protocol."""
from ..artifacts import ArtifactService
from ..domain import clock
from ..errors import GxError
from ..persistence import store
from .science import JSON, _current, _id, _reviewed_submission, require


def settle_assessment(ctx, result):
    assets = ArtifactService(ctx.conn)
    proposal = assets.read_json(result.get("proposal_ref") or ctx.outputs["proposal"]["proposal_ref"])
    outcomes = assets.read_json(result.get("evidence_ref") or ctx.outputs["experiment"]["evidence_ref"])["routes"]
    review = assets.read_json(result["artifact_ref"])
    findings = {f["path_id"]: f for f in review["findings"]}
    hypotheses = {p["id"]: p["hypothesis_id"] for p in proposal["paths"]}
    with store.transaction(ctx.conn) as conn:
        submitted = _reviewed_submission(conn, result["task_id"])
        require(result["submission_id"] == submitted["submission_id"], "review submission mismatch")
        for route in outcomes:
            path = route["path_id"]
            settlement = _id("settlement", ctx.run_id, submitted["submission_id"], path)
            if conn.execute("SELECT 1 FROM science_settlements WHERE settlement_id=?", (settlement,)).fetchone():
                continue
            finding = findings[path]
            reason, now = finding["reason"], clock.now_iso()
            kind, manifest_hash, evidence, direction = route["outcome_kind"], None, None, None
            if kind == "historical_only":
                continue
            if kind == "valid_scientific_outcome":
                if finding["supported"] is None:
                    # The measurement is genuine, but the review found an unresolved
                    # qualification. The cited assessment retains it without minting a
                    # supporting or refuting claim from a numerical threshold alone.
                    continue
                try:
                    proof = _current(conn, ctx.run_id, route, proposal["evaluation_item"], proposal.get("objective_item"))
                except GxError as exc:
                    if exc.details.get("reason") == "current_intent":
                        continue
                    kind, reason = "measurement_failure", f"Unusable measurement: {exc}"
                else:
                    eval_row = conn.execute("SELECT * FROM evaluations WHERE eval_id=? AND run_id=?",
                                            (route["eval_id"], ctx.run_id)).fetchone()
                    expected = proposal["evaluator"]
                    require(eval_row and eval_row["frozen_sha"] == expected["spec_hash"] and
                            store.loads(eval_row, "artifact_refs_json") == [route["observations_ref"], route["evaluation_ref"]],
                            "evidence does not match its frozen evaluation and original measurement")
                    manifest_hash = proof["manifest_hash"]
                    evidence = _id("gxv", settlement)
                    direction = "supports" if finding["supported"] else "refutes"
                    conn.execute("INSERT INTO evidence(evidence_id,namespace,run_id,status,claim,claim_grain,"
                                 "evidence_type,source_locator_json,method,limitations,review_status,confidence_basis,"
                                 "evidence_level,origin,created_at,updated_at) VALUES(?,?,?,'REVIEWED',?,'FINDING',"
                                 "'COMPUTATIONAL',?,'fixture-grid@1','Measured synthetic grid only.','REVIEWED',"
                                 "'Frozen evaluation and independent review','EXECUTION_OBSERVED','JOB_OUTPUT',?,?)",
                                 (evidence, ctx.run_id, ctx.run_id, reason, JSON({"job_id": route["job_id"],
                                  "observations_ref": route["observations_ref"], "evaluation_ref": route["evaluation_ref"],
                                  "assessment_ref": result["artifact_ref"], "evidence_refs": review["evidence_refs"],
                                  "source_reviews": review.get("source_reviews", [])}), now, now))
                    conn.execute("INSERT INTO evidence_reviews(review_id,evidence_id,task_id,lease_id,principal,"
                                 "worker_observation_id,independence_level,verdict,note,created_at)"
                                 " VALUES(?,?,?,?,?,?,'FRESH_CONTEXT_SAME_HOST','SUPPORTED',?,?)",
                                 (_id("gxrv", settlement), evidence, result["task_id"], submitted["lease_id"],
                                  submitted["principal"], submitted["worker_observation_id"], reason, now))
            conn.execute("INSERT INTO science_settlements(settlement_id,run_id,round_index,path_id,hypothesis_id,"
                         "task_id,attempt,submission_id,job_id,kind,reason,eval_id,artifact_ref,manifest_hash,"
                         "evidence_id,direction,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (settlement, ctx.run_id, ctx.round, path, hypotheses[path], result["task_id"],
                          submitted["attempt"], submitted["submission_id"], route["job_id"], kind, reason,
                          route.get("eval_id"), route.get("observations_ref"), manifest_hash, evidence, direction, now))


def current_evidence(conn, run_id):
    """Read the accepted outcomes still admissible under the active scientific requirements."""
    from . import science_inputs
    rows = conn.execute("SELECT s.*,e.provenance_json,t.input_json,u.artifact_refs_json FROM science_settlements s JOIN evaluations e USING(eval_id)"
                        " JOIN tasks t ON t.task_id=s.task_id"
                        " JOIN submissions u ON u.submission_id=s.submission_id"
                        " WHERE s.run_id=? AND s.kind='valid_scientific_outcome' ORDER BY s.round_index,s.path_id",
                        (run_id,)).fetchall()
    valid = []
    for row in rows:
        assets = ArtifactService(conn)
        review = next((assets.read_json(ref) for ref in store.loads(row, "artifact_refs_json", [])
                       if assets.describe(ref)["kind"] == "science_assessment"), None)
        if review is None or not science_inputs.review_covers(conn, run_id, row["round_index"], review):
            continue
        origin = store.loads(row, "provenance_json")
        proposal = assets.read_json(store.loads(row, "input_json")["proposal_ref"])
        try:
            _current(conn, run_id, {"job_id": row["job_id"], "observations_ref": origin["input_ref"]},
                     origin["intent_item"], proposal.get("objective_item"))
        except GxError:
            continue
        valid.append(dict(row))
    return valid
