"""Independent evidence extraction and prospective finite-grid checks. Reads a completed or partial GX store."""
from contextlib import closing
import hashlib
import json
import sqlite3


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def capture(state):
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")

        def rows(sql):
            return [dict(r) for r in db.execute(sql)]

        result = {
            "run": rows("SELECT run_id,status,workflow_id,workflow_version,workflow_sha256 FROM runs")[0],
            "jobs": rows("SELECT job_id,status,idempotency_key,input_manifest_ref,submitted_at,finished_at,measured_json "
                         "FROM jobs ORDER BY rowid"),
            "submissions": rows("SELECT s.submission_id,s.created_at,s.result,s.attempt,s.artifact_refs_json,s.validation_json, "
                                "l.principal,t.role,t.task_id,json_extract(t.input_json,'$.round') AS round "
                                "FROM submissions s JOIN tasks t USING(task_id) JOIN leases l ON l.lease_id=s.lease_id "
                                "ORDER BY s.rowid"),
            "settlements": rows("SELECT round_index,path_id,hypothesis_id,job_id,submission_id,kind,direction,"
                                "evidence_id,eval_id,artifact_ref,manifest_hash FROM science_settlements ORDER BY rowid"),
            "roots": rows("SELECT task_id,thread_id,role,model,effort,provider,state FROM p0_roots"),
            "meter": rows("SELECT thread_id,turn_id,budget_status,confidence,settled_tokens FROM p0_meter_native_turns"),
            "turns": rows("SELECT * FROM p0_root_turn_usage ORDER BY rowid"),
            "deliveries": rows("SELECT thread_id,turn_id,reservation_ref,state FROM p0_root_deliveries"),
            "ledger": rows("SELECT subject_id,status,amounts_json,settled_at FROM budget_reservations "
                           "WHERE subject_kind='native_turn' ORDER BY rowid"),
            "decisions": rows("SELECT * FROM science_decisions ORDER BY round_index"),
            "product_calls": rows("SELECT thread_id,turn_id,call_id,request_sha,"
                                  "json_extract(result_json,'$.submission_id') AS submission_id "
                                  "FROM p0_root_calls WHERE json_extract(result_json,'$.submission_id') IS NOT NULL"),
            "sources": rows("SELECT s.source_id,s.provenance_json,g.text,g.text_sha256 FROM sources s JOIN "
                            "source_segments g USING(source_id) WHERE s.kind='operator_note' ORDER BY s.rowid"),
            "manifests": rows("SELECT job_id,admission_sha256,admission_json,completion_sha256,completion_json "
                              "FROM p0_execution_manifests"),
            "messages": rows("SELECT message_id,created_at,result_json FROM p0_operator_messages "
                             "WHERE json_extract(action_json,'$.kind')='supplement'"),
            "artifacts": {},
        }
        for manifest in result["manifests"]:
            for phase in ("admission", "completion"):
                raw = manifest[phase + "_json"]
                if raw is not None and digest(raw.encode()) != manifest[phase + "_sha256"]:
                    raise ValueError("Execution manifest digest mismatch: " + manifest["job_id"])
        for a in rows("SELECT artifact_ref,kind,bytes,sha256,namespace,rel_path FROM artifacts WHERE published=1"):
            data = (state / "artifacts" / a["namespace"] / a["rel_path"]).read_bytes()
            if digest(data) != a["sha256"] or len(data) != a["bytes"]:
                raise ValueError("Artifact digest/size mismatch: " + a["artifact_ref"])
            if a["kind"] in {"research_report", "research_proposal", "grid_input", "science_assessment",
                             "science_decision", "research_evidence", "synthetic_grid", "score_evaluation",
                             "operator_source"}:
                result["artifacts"][a["artifact_ref"]] = {
                    "kind": a["kind"], "bytes": a["bytes"], "sha256": a["sha256"], "body": json.loads(data)}
    return result


def official_product_calls(session_dir, evidence):
    """Export only public product tool arguments from the official session. Exclude reasoning and account data."""
    calls = []
    known = {(r["thread_id"], r["call_id"]): r for r in evidence["product_calls"]}
    for session in sorted(session_dir.rglob("*.jsonl")):
        thread = provider = None
        with session.open() as stream:
            for line in stream:
                event = json.loads(line)
                item = event.get("payload", {})
                if event["type"] == "session_meta":
                    thread, provider = item.get("id"), item.get("model_provider")
                if (event["type"] != "response_item" or item.get("type") != "function_call"
                        or item.get("name") != "gx_submit_product"):
                    continue
                args = json.loads(item["arguments"])
                identity = json.dumps(["gx_submit_product", args], ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()
                receipt = known.get((thread, item["call_id"]), {})
                calls.append({"provider": provider, "thread_id": thread, "call_id": item["call_id"],
                              "namespace": item.get("namespace"),
                              "at": event["timestamp"], "product": args["product"], "session_file": session.name,
                              "request_sha256": digest(identity), "receipt": receipt})
    return calls


# Independently tabulated from the three high-interval observations at (1.5,-1/4),
# (1.75,-1/16), (2.5,-1/4). The right linear segment has slope -1/4.
# Grid scores and these interpolation predictions are separate quantities.
PREDICTIONS = {
    1.625: {"quadratic": -9/64, "linear": -5/32},
    1.8125: {"quadratic": -9/256, "linear": -5/64},
    2.125: {"quadratic": -1/64, "linear": -5/32},
    2.375: {"quadratic": -9/64, "linear": -7/32},
}


def score(data, case):
    artifacts = data["artifacts"]
    jobs = {row["job_id"]: row for row in data["jobs"]}
    submissions = {s["submission_id"]: s for s in data["submissions"] if s["result"] == "ACCEPTED"}
    roots = {r["thread_id"]: r for r in data["roots"]}
    manifests = {m["job_id"]: m for m in data["manifests"]}
    acknowledged = {r["thread_id"] for r in data["deliveries"] if r["state"] == "acknowledged" and r["turn_id"]}
    expected = {method: max(PREDICTIONS, key=lambda x: PREDICTIONS[x][method]) for method in ("linear", "quadratic")}
    dirs = {(s["round_index"], s["path_id"]): s["direction"] for s in data["settlements"]
            if s["kind"] == "valid_scientific_outcome" and s["evidence_id"]}
    reports = [v["body"] for v in artifacts.values() if v["kind"] == "research_report"]
    history = reports[0]["rounds"] if len(reports) == 1 else []
    if not reports:
        # Preserve and score any complete accepted round even if the later budget or provider stopped.
        for d in data["decisions"]:
            dec = artifacts[d["decision_ref"]]["body"]
            review = artifacts[dec["assessment_ref"]]["body"]
            history.append({"round": d["round_index"], "artifact_ref": d["decision_ref"],
                            "proposal_ref": dec["proposal_ref"], "assessment_ref": dec["assessment_ref"],
                            "evidence_ref": review["evidence_refs"][0]})
    outcomes, reviews, choices = {}, [], {}
    checks = {"completed_two_rounds": data["run"]["status"] == "COMPLETED" and
                                         [r["round"] for r in history] == [0, 1],
              "initial_hypothesis_settlements": dirs.get((0, "low")) == "refutes" and dirs.get((0, "high")) == "supports"}
    native = data.get("official_products", [])
    accepted_ids = {s["submission_id"] for s in data["submissions"] if s["result"] == "ACCEPTED"}
    matched_ids = {r["receipt"]["submission_id"] for r in native if r.get("receipt")
                   and r["request_sha256"] == r["receipt"]["request_sha"]}
    checks["accepted_products_match_native_calls"] = bool(accepted_ids and accepted_ids <= matched_ids)
    checks["bounded_native_work"] = 0 < len(data["meter"]) <= case["project"]["run_budget"]["api_calls"]
    checks["all_turns_within_budget_and_settled"] = bool(data["meter"]) and (
        all(x["budget_status"] == "SETTLED" and x["settled_tokens"] is not None for x in data["meter"])
        and sum(x["settled_tokens"] for x in data["meter"]) <= case["project"]["run_budget"]["tokens"])
    for item in history:
        i = item["round"]
        proposal, review, decision, evidence = [artifacts[item[k]]["body"] for k in
                                                ("proposal_ref", "assessment_ref", "artifact_ref", "evidence_ref")]
        choices[i] = decision
        index = {d["round_index"]: d for d in data["decisions"]}[i]
        reviewer = submissions.get(index["reviewer_submission_id"], {})
        planner = submissions.get(index["submission_id"], {})
        reviews.append({"round": i, "reviewer": reviewer, "planner": planner, "author": proposal["author_principal"]})
        checks[f"independent_submission_r{i}"] = bool(
            planner and reviewer and index["decision_ref"] == item["artifact_ref"]
            and reviewer["role"] == "evidence_reviewer" and planner["role"] == "planner"
            and reviewer["principal"] not in (planner["principal"], proposal["author_principal"])
            and item["assessment_ref"] in json.loads(reviewer["artifact_refs_json"])
            and item["artifact_ref"] in json.loads(planner["artifact_refs_json"])
            and all(roots.get(s["principal"], {}).get("task_id") == s["task_id"] and s["principal"] in acknowledged
                    for s in (reviewer, planner))
            and review["evidence_refs"] == [item["evidence_ref"]]
            and decision["assessment_ref"] == item["assessment_ref"] and decision["proposal_ref"] == item["proposal_ref"])
        for route in evidence["routes"]:
            path = route["path_id"]
            original = artifacts[route["observations_ref"]]["body"]
            measured = original["observations"]
            evaluation = artifacts[route["evaluation_ref"]]["body"]
            grid_ref = next(p["input_ref"] for p in proposal["paths"] if p["id"] == path)
            measured_coords = [{"candidate_id": p["candidate_id"], "x": p["x"]} for p in measured]
            job = jobs[route["job_id"]]
            checks[f"observation_chain_r{i}_{path}"] = bool(
                job["status"] == "SUCCEEDED" and route["outcome_kind"] == "valid_scientific_outcome"
                and manifests[route["job_id"]]["completion_sha256"]
                and original["input_sha256"] == artifacts[grid_ref]["sha256"]
                and artifacts[grid_ref]["body"]["candidates"] == measured_coords
                and evaluation["input_ref"] == route["observations_ref"]
                and evaluation["threshold"] == proposal["threshold"] == -0.25
                and all(p["score"] == -(p["x"] - 2)**2 for p in measured)
                and evaluation["max_score"] == max(p["score"] for p in measured)
                and evaluation["count_at_or_above"] == sum(p["score"] >= -0.25 for p in measured))
            settlement = next((s for s in data["settlements"] if (s["round_index"], s["path_id"], s["job_id"]) ==
                               (i, path, route["job_id"]) and s.get("evidence_id")), {})
            prediction = next(p for p in proposal["paths"] if p["id"] == path)
            expected_crossing = prediction.get("expected_reaches_threshold", True)
            reaches = any(p["score"] >= proposal["threshold"] for p in measured)
            checks[f"reviewed_finding_chain_r{i}_{path}"] = bool(
                settlement and settlement["submission_id"] == index["reviewer_submission_id"]
                and settlement["direction"] == ("supports" if expected_crossing == reaches else "refutes")
                and settlement["kind"] == "valid_scientific_outcome" and settlement["eval_id"] == route["eval_id"]
                and settlement["artifact_ref"] == route["observations_ref"]
                and settlement["hypothesis_id"] == prediction["hypothesis_id"]
                and settlement["manifest_hash"] == manifests[route["job_id"]]["completion_sha256"])
            outcomes[(i, path)] = (measured, job, route)
        if i != 0:
            continue
        sources = {row["text"]: row for row in data["sources"]}
        for src in case["supplements"]:
            source = sources.get(src["text"], {})
            msg = next((m for m in data["messages"] if m["message_id"] == src["message_id"]), {})
            sid = source.get("source_id")
            is_catalog = src["message_id"] == "follow-up-controller-catalog"
            permitted = {"accepted"} if is_catalog else {"pending", "rejected"}
            path_refs = {r[k] for r in evidence["routes"] if is_catalog or r["path_id"] == "low"
                         for k in ("observations_ref", "evaluation_ref")}
            # A GX evidence bundle also resolves both paths and their raw observations.
            path_refs.add(item["evidence_ref"])
            checks[f"source_{src['message_id']}"] = bool(sid and msg and
                json.loads(msg["result_json"])["source_id"] == sid and digest(src["text"].encode()) == source["text_sha256"]
                and all(j["submitted_at"] <= msg["created_at"] for j in (outcomes[(0, p)][1] for p in ("low", "high")))
                and msg["created_at"] <= reviewer.get("created_at", "") and all(any(
                    j["source_id"] == sid and artifacts[j["artifact_ref"]]["body"]["text"] == src["text"]
                    and j["disposition"] in permitted and set(j["evidence_refs"]) & path_refs
                    for j in row.get(field, []))
                    for row, field in ((review, "source_reviews"), (decision, "source_decisions"))))
    first = choices.get(0, {})
    continuation = [(p, p, first["next_grids"][p]) for p in first.get("continue_paths", [])]
    continuation.extend((p["from_path"], p["id"], p["grid"]) for p in first.get("pivots", []))
    origin, next_path, grid = continuation[0] if len(continuation) == 1 else (None, None, {})
    selected = grid.get("candidates", [])
    chosen = {p["candidate_id"]: p["x"] for p in selected}
    checks["alternative_method_choices"] = origin == "high" and len(selected) == 2 and chosen == expected
    measured, job, _ = outcomes.get((1, next_path), ([], {}, {}))
    followup = {p["candidate_id"]: p["x"] for p in measured}
    r0 = next((d for d in data["decisions"] if d["round_index"] == 0), {})
    checks["decision_executed"] = bool(chosen and chosen == followup and job and r0["created_at"] <= job["submitted_at"])
    scores = {p["candidate_id"]: p["score"] for p in measured}
    checks["held_out_measured"] = (len(jobs) == 3 and scores == {"quadratic": -1/64, "linear": -9/256}
                                   and choices.get(1, {}).get("continue_paths") == [] and bool(reports and reports[0]["stop_reason"]))
    checks["all_required_stages"] = len(history) == len(reviews) == 2 and len(outcomes) == 3
    return {"checks": checks, "all_mechanical_checks": all(checks.values()), "interpolation_predictions": PREDICTIONS,
            "expected_candidate_coordinates": expected, "submitted_candidate_coordinates": chosen,
            "measured_scores": scores, "score_difference_quadratic_minus_linear":
            scores["quadratic"] - scores["linear"] if {"linear", "quadratic"} <= scores.keys() else None,
            "qualitative_grading": "Requires examination of original review and planner text against case.json interpretive_review."}
