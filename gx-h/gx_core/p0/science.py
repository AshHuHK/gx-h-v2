"""Frozen scoring and reviewed scientific outcomes for the GX science_grid workflow."""
import inspect
import math
import subprocess

from ..artifacts import ArtifactService
from ..domain import clock, ids
from ..errors import GxError, SchemaInvalid
from ..persistence import store

JSON = ids.canonical_json


def require(condition, message):
    if not condition:
        raise SchemaInvalid(message)


def _id(prefix, *parts):
    return prefix + "_" + ids.sha256_of_obj(parts)


def _publish(conn, run, folder, kind, body, **metadata):
    return ArtifactService(conn).publish_bytes(
        run_id=run, namespace=f"{run}/science/{folder}", rel_path=f"{ids.sha256_of_obj(body)}.json",
        kind=kind, media_type="application/json", data=JSON(body), **metadata).artifact_ref


def register_hypothesis(conn, run, path, actor):
    hypothesis = _id("gxh", run, path.get("hypothesis_id", path["id"]))
    expected = path.get("expected_reaches_threshold", True)
    require(type(expected) is bool, "expected_reaches_threshold must be boolean")
    document = {"prediction": path["prediction"], "expected_reaches_threshold": expected}
    row = conn.execute("SELECT document_json FROM hypothesis_revisions WHERE hypothesis_id=? AND revision=1",
                       (hypothesis,)).fetchone()
    if row:
        require(store.loads(row, "document_json") == document, "a changed prediction needs a new hypothesis")
    else:
        now = clock.now_iso()
        conn.execute("INSERT INTO hypotheses VALUES(?,?,?,?,?)", (hypothesis, run, 1, actor, now))
        conn.execute("INSERT INTO hypothesis_revisions(hypothesis_id,revision,document_json,sha256,"
                     "author_principal,created_at) VALUES(?,1,?,?,?,?)",
                     (hypothesis, JSON(document), ids.sha256_of_obj(document), actor, now))
    return hypothesis


def score(observations, threshold):
    if not isinstance(observations, list) or not observations:
        raise ValueError("missing observations")
    values = [point.get("score") if isinstance(point, dict) else None for point in observations]
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in values):
        raise ValueError("each observation needs a finite score")
    return {"max_score": max(values), "threshold": threshold, "count_at_or_above": sum(v >= threshold for v in values),
            "observations": len(values)}


def _score_artifact(conn, run, source, attempt, spec):
    threshold = spec["threshold"]
    if plugin := spec.get("evaluator"):
        from ..plugins.evaluator import evaluate_scores
        return evaluate_scores(conn, run_id=run, attempt_id=attempt, plugin_ref=plugin,
                               observations_ref=source, threshold=threshold)
    service = ArtifactService(conn)
    require(spec["implementation_sha"] == ids.sha256_of_obj(inspect.getsource(score)),
            "the pinned builtin score implementation is unavailable")
    result = {**score(service.read_json(source)["observations"], threshold), "input_ref": source,
              "input_sha256": service.describe(source)["sha256"], "attempt_id": attempt}
    result["artifact_ref"] = _publish(conn, run, "evaluations", "score_evaluation", result)
    return result


def _insert_evaluation(conn, *, eval_id, run, path, frozen, purpose, refs, result, provenance, case=None):
    metrics = {k: result[k] for k in ("threshold", "count_at_or_above", "observations", "max_score")}
    conn.execute("INSERT INTO evaluations(eval_id,run_id,path_id,eval_spec_id,eval_spec_version,frozen_sha,"
                 "purpose,case_id,artifact_refs_json,layer,verdict,metrics_json,provenance_json,created_at)"
                 " VALUES(?,?,?,?,?,?,?,?,?,3,?,?,?,?) ON CONFLICT(eval_id) DO NOTHING",
                 (eval_id, run, path, frozen["id"], frozen["version"], frozen["spec_hash"], purpose, case,
                  JSON(refs), "PASS" if metrics["count_at_or_above"] else "FAIL", JSON(metrics),
                  JSON(provenance), clock.now_iso()))


def freeze_spec(conn, run_id, value):
    """Select an immutable definition or qualify a new one on positive, negative and boundary controls."""
    if not isinstance(value, dict):
        raise SchemaInvalid("an evaluation specification must be a JSON object")
    if "eval_spec_id" in value:
        row = conn.execute("SELECT * FROM eval_specs WHERE eval_spec_id=? AND version=? AND status='FROZEN'"
                           " AND namespace=?", (value["eval_spec_id"], str(value.get("version")), run_id)).fetchone()
        require(row is not None, "unknown frozen evaluation in this project")
        return {"id": row["eval_spec_id"], "version": row["version"], "spec_hash": row["frozen_sha"]}
    threshold = value.get("threshold")
    require(type(threshold) in (int, float) and math.isfinite(threshold), "a finite threshold is required")
    evaluator = value.get("evaluator")
    if evaluator is not None:
        require(isinstance(evaluator, dict) and isinstance(evaluator.get("package_digest"), str),
                "a plugin-backed evaluator needs its complete installed package reference")
    spec = {"domain": "synthetic fixture-grid@1", "rule": "at least one observation reaches threshold",
            "threshold": float(threshold), "evaluator": evaluator,
            "implementation_sha": evaluator["package_digest"] if evaluator else ids.sha256_of_obj(inspect.getsource(score))}
    digest = ids.sha256_of_obj(spec)
    frozen = {"id": _id("grid_score", run_id), "version": digest, "spec_hash": digest}
    with store.transaction(conn):
        if conn.execute("SELECT 1 FROM eval_specs WHERE eval_spec_id=? AND version=?",
                        (frozen["id"], digest)).fetchone():
            return frozen
        now = clock.now_iso()
        examples = [("POSITIVE", math.nextafter(threshold, math.inf), True),
                    ("NEGATIVE", math.nextafter(threshold, -math.inf), False), ("BOUNDARY", threshold, True)]
        require(all(math.isfinite(point) for _, point, _ in examples),
                "the cutoff must allow finite controls on both sides")
        cases = []
        for name, point, expected in examples:
            ref = _publish(conn, run_id, "controls", "score_control", {"observations": [{"score": point}]})
            case_id = _id("case", run_id, digest, name)
            result = _score_artifact(conn, run_id, ref, case_id, spec)
            require(bool(result["count_at_or_above"]) == expected, f"failed {name} evaluation control")
            refs = [ref, result["artifact_ref"]]
            conn.execute("INSERT INTO eval_cases(case_id,eval_spec_id,version,case_kind,description,"
                         "artifact_refs_json,expected_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (case_id, frozen["id"], digest, name, f"{name} cutoff control", JSON(refs),
                          JSON({"reaches_threshold": expected}), now))
            _insert_evaluation(conn, eval_id=case_id, run=run_id, path=None, purpose="CASE", frozen=frozen,
                               case=case_id, refs=refs, result=result, provenance={"synthetic_control": True})
            cases.append(case_id)
        conn.execute("INSERT INTO eval_specs(eval_spec_id,version,status,frozen_sha,objective,applicable_domain,"
                     "spec_json,implementation_ref,controls_json,thresholds_json,limitations,namespace,"
                     "created_at,frozen_at) VALUES(?,?,'FROZEN',?,?,?,?,?,?,?,?,?,?,?)",
                     (frozen["id"], digest, digest, spec["rule"], spec["domain"], JSON(spec),
                      spec["implementation_sha"], JSON(cases), JSON({"threshold": threshold}),
                      "Measured grid points only.", run_id, now, now))
    return frozen


def prospective_spec(conn, run, inputs, threshold):
    from . import intent
    items = {key: item for key, item in intent.active_items(conn, run).items()
             if item["item_type"] == "evaluation_spec"}
    item_id = inputs.get("evaluation_item_id")
    if item_id is None and items:
        require(len(items) == 1, "select an evaluation_item_id for this workflow")
        item_id = next(iter(items))
    require(item_id is None or item_id in items, "the selected evaluation item is inactive or missing")
    item = items.get(item_id)
    value = item["value"] if item else {"threshold": threshold, "evaluator": inputs.get("evaluator")}
    frozen = freeze_spec(conn, run, value)
    pin = {"item_id": item_id, "version": item["version"]} if item else None
    return frozen, pin, _spec(conn, frozen)["threshold"]


def _spec(conn, frozen):
    row = conn.execute("SELECT spec_json,frozen_sha FROM eval_specs WHERE eval_spec_id=? AND version=?"
                       " AND status='FROZEN'", (frozen["id"], frozen["version"])).fetchone()
    require(row and row["frozen_sha"] == frozen["spec_hash"], "unrecognized frozen specification")
    spec = store.loads(row, "spec_json")
    require(ids.sha256_of_obj(spec) == frozen["spec_hash"], "the stored evaluation definition changed")
    return spec


def _evaluate(conn, run, path, job, source, frozen, origin=None, item=None):
    from .manifest import read_manifest, validate_evidence
    proof = validate_evidence(conn, run_id=run, job_id=job, artifact_ref=source)
    admission = read_manifest(conn, job)["admission"]
    raw = ArtifactService(conn).read_json(source)
    require((raw.get("method_id"), raw.get("method_version")) ==
            (admission["capability"]["id"], admission["capability"]["version"])
            and raw.get("operation_id") == admission["parameters"]["operation"]
            and [entry["sha256"] for entry in admission["inputs"] if entry["role"] == "grid"] == [raw.get("input_sha256")],
            "observations do not originate from the declared grid, operation and method")
    eval_id = _id("gxe", run, job, source, frozen["spec_hash"], item)
    prior = conn.execute("SELECT artifact_refs_json FROM evaluations WHERE eval_id=?", (eval_id,)).fetchone()
    if prior:
        ref = store.loads(prior, "artifact_refs_json")[-1]
        ArtifactService(conn).read_json(ref)
        return {"eval_id": eval_id, "evaluation_ref": ref, "observations_ref": source}
    spec = _spec(conn, frozen)
    result = _score_artifact(conn, run, source, eval_id, spec)
    provenance = {"job_id": job, "input_ref": source, "manifest_hash": proof["manifest_hash"],
                  "original_evaluation_id": origin, "intent_item": item}
    with store.transaction(conn):
        _insert_evaluation(conn, eval_id=eval_id, run=run, path=path, frozen=frozen, purpose="CANDIDATE",
                           refs=[source, result["artifact_ref"]], result=result, provenance=provenance)
        if item and origin is None:
            from . import intent
            intent.bind_evaluation(conn, run, eval_id, item["item_id"], item["version"])
    return {"eval_id": eval_id, "evaluation_ref": result["artifact_ref"], "observations_ref": source}


def reevaluate_intent(conn, payload):
    """Run a changed criterion against its original bytes, never submitting another compute Job."""
    run = payload["run_id"]
    row = conn.execute("SELECT * FROM evaluations WHERE run_id=? AND eval_id=? AND purpose='CANDIDATE'",
                       (run, payload["object_id"])).fetchone()
    require(row is not None and payload["object_type"] == "evaluation", "invalid evaluation work item")
    origin = store.loads(row, "provenance_json")
    item = {"item_id": payload["item_id"], "version": payload["item_version"]}
    require(origin.get("intent_item") and origin["intent_item"]["item_id"] == item["item_id"],
            "the evaluation does not depend on the changed item")
    from . import intent
    value = intent.read_item(conn, run, item["item_id"], item["version"])["value"]
    frozen = freeze_spec(conn, run, value)
    return _evaluate(conn, run, row["path_id"], origin["job_id"], origin["input_ref"], frozen,
                     origin=row["eval_id"], item=item)


def experiment_result(conn, run, row, proposal):
    if row["status"] != "SUCCEEDED":
        failure = row.get("failure") or {}
        invalid = failure.get("failure_class") == "input_or_format" or failure.get("observed") in (
            "output_rejected", "missing_output")
        # The shipped grid adapter returns a generic nonzero exit for malformed numeric coordinates.
        if failure.get("observed") == "nonzero_exit":
            grid = ArtifactService(conn).read_json(row["input_ref"])
            invalid = any(type(p.get("x")) not in (int, float) or not math.isfinite(p["x"])
                          for p in grid["candidates"] if isinstance(p, dict))
        row["outcome_kind"] = "measurement_failure" if invalid else "infrastructure_failure"
        return
    try:
        result = _evaluate(conn, run, row["path_id"], row["job_id"], row["observations_ref"],
                           proposal["evaluator"], item=proposal["evaluation_item"])
    except GxError as exc:
        kind = "historical_only" if exc.details.get("reason") == "current_intent" else "measurement_failure"
        row.update(outcome_kind=kind, evaluation_error=str(exc))
    except (ValueError, KeyError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        row.update(outcome_kind="measurement_failure", evaluation_error=str(exc))
    else:
        row.update(result, outcome_kind="valid_scientific_outcome")


def record_failure(conn, *, run_id, task_id, attempt, kind, reason, operation_id):
    require(kind in ("agent_execution_failure", "infrastructure_failure"), "invalid worker failure class")
    conn.execute("INSERT INTO science_settlements(settlement_id,run_id,task_id,attempt,kind,reason,created_at)"
                 " VALUES(?,?,?,?,?,?,?) ON CONFLICT(settlement_id) DO NOTHING",
                 (_id("failure", run_id, task_id, attempt, operation_id), run_id, task_id, attempt,
                  kind, reason, clock.now_iso()))


def _reviewed_submission(conn, task_id):
    row = conn.execute("SELECT s.*,l.principal FROM tasks t JOIN submissions s USING(task_id)"
                       " JOIN leases l ON l.lease_id=s.lease_id WHERE t.task_id=? AND t.status='ACCEPTED'"
                       " AND s.result='ACCEPTED' AND s.attempt=t.attempt", (task_id,)).fetchone()
    require(row is not None and row["principal"], "no accepted submission under a worker lease")
    return row


def _current(conn, run, row, *pins):
    from .manifest import validate_evidence
    proof = validate_evidence(conn, run_id=run, job_id=row["job_id"], artifact_ref=row["observations_ref"])
    for pin in filter(None, pins):
        from . import intent
        item = intent.read_item(conn, run, pin["item_id"])
        if item["status"] != "active" or item["version"] != pin["version"]:
            raise SchemaInvalid("this result belongs to an earlier scientific requirement",
                                details={"reason": "current_intent"})
    return proof


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
                                  "observations_ref": route["observations_ref"], "evaluation_ref": route["evaluation_ref"]}), now, now))
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


def decision_routes(proposal, body):
    kept = set(body["continue_paths"])
    pivots = body.get("pivots", [])
    by_source = {p["from_path"]: p for p in pivots}
    routes, actions = [], []
    for old in proposal["paths"]:
        name = old["id"]
        if name in kept:
            routes.append({"id": name, "prediction": old["prediction"], "grid": body["next_grids"][name],
                           "expected_reaches_threshold": old.get("expected_reaches_threshold", True),
                           "hypothesis_id": old.get("hypothesis_key", name)})
            actions.append({"action": "continue", "path_id": name})
        elif pivot := by_source.get(name):
            routes.append({k: v for k, v in pivot.items() if k != "from_path"})
            actions.append({"action": "pivot", "path_id": name, "successor": pivot["id"]})
        elif kept or pivots:
            actions.append({"action": "prune", "path_id": name})
        else:
            actions.append({"action": "stop", "path_id": name})
    return routes, actions


def current_evidence(conn, run_id):
    """Read the accepted outcomes still admissible under the active scientific requirements."""
    rows = conn.execute("SELECT s.*,e.provenance_json,t.input_json FROM science_settlements s JOIN evaluations e USING(eval_id)"
                        " JOIN tasks t ON t.task_id=s.task_id"
                        " WHERE s.run_id=? AND s.kind='valid_scientific_outcome' ORDER BY s.round_index,s.path_id",
                        (run_id,)).fetchall()
    valid = []
    for row in rows:
        origin = store.loads(row, "provenance_json")
        proposal = ArtifactService(conn).read_json(store.loads(row, "input_json")["proposal_ref"])
        try:
            _current(conn, run_id, {"job_id": row["job_id"], "observations_ref": origin["input_ref"]},
                     origin["intent_item"], proposal.get("objective_item"))
        except GxError:
            continue
        valid.append(dict(row))
    return valid


def record_decision(ctx, result, actions, stop, paths):
    with store.transaction(ctx.conn) as conn:
        decision = _reviewed_submission(conn, result["task_id"])
        from .science_refresh import review_for
        review = _reviewed_submission(conn, review_for(conn, ctx.run_id, result["assessment_ref"])["task_id"])
        require(decision["principal"] != review["principal"], "the reviewer and decision worker must differ")
        assets = ArtifactService(conn)
        body = assets.read_json(result["artifact_ref"])
        proposal = assets.read_json(result["proposal_ref"])
        outcomes = assets.read_json(result["evidence_ref"])["routes"]
        by_path = {r["path_id"]: r for r in outcomes}
        # A change after hand-in can make a previously successful observation inadmissible.
        # Preserve the submitted decision and record the effective disposition in the ledger.
        for action in actions:
            row = by_path[action["path_id"]]
            if action["action"] not in ("continue", "pivot") or row["outcome_kind"] in (
                    "infrastructure_failure", "measurement_failure"):
                continue
            try:
                _current(conn, ctx.run_id, row, proposal["evaluation_item"], proposal.get("objective_item"))
            except GxError as exc:
                blocked = action.get("successor", action["path_id"])
                paths = [p for p in paths if p["id"] != blocked]
                action.update(action="prune", reason=f"current eligibility: {exc}")
                if not paths:
                    stop = "no_current_evidence"
        conn.execute("INSERT INTO science_decisions VALUES(?,?,?,?,?,?,?,?,?)"
                     " ON CONFLICT(run_id,round_index) DO NOTHING",
                     (ctx.run_id, ctx.round, decision["submission_id"], review["submission_id"],
                      result["artifact_ref"], JSON(actions), body["reason"], stop, clock.now_iso()))
        return paths, stop
