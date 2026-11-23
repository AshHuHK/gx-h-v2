"""Read primary B5 store and native receipts and recompute the acquisition comparison exactly."""
import argparse
from contextlib import closing
from datetime import datetime
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import sqlite3


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def fraction(value):
    return Fraction(str(value))


def interpolation(observations, catalog):
    points = sorted((fraction(r["x"]), fraction(r["score"])) for r in observations)
    (x0, y0), (x1, y1), (x2, y2) = points
    first = (y1 - y0) / (x1 - x0)
    second = (y2 - y1) / (x2 - x1)
    curvature = (second - first) / (x2 - x0)
    rows = []
    for coordinate in catalog:
        x = fraction(coordinate)
        if not x0 <= x <= x2:
            raise ValueError("Catalog asks for extrapolation")
        linear = y0 + first * (x - x0) if x <= x1 else y1 + second * (x - x1)
        quadratic = y0 + first * (x - x0) + curvature * (x - x0) * (x - x1)
        rows.append({"x": x, "quadratic": quadratic, "linear": linear})
    maxima = {}
    for method in ("quadratic", "linear"):
        top = max(row[method] for row in rows)
        maxima[method] = [str(row["x"]) for row in rows if row[method] == top]
    return [{k: str(v) for k, v in row.items()} for row in rows], maxima


def native_record(sessions, thread, call):
    files = list(sessions.rglob("*" + thread + "*.jsonl"))
    if len(files) != 1:
        raise ValueError("Expected one official rollout for " + thread)
    path = files[0]
    arguments, starts, exposed_tools, started = [], set(), set(), None
    submission_identity = set()
    with path.open() as stream:
        for line in stream:
            record = json.loads(line)
            event = record["payload"]
            kind = event.get("type")
            if record["type"] == "session_meta" and event["id"] != thread:
                raise ValueError("Foreign official thread")
            if kind == "task_started":
                starts.add(event["turn_id"])
            if record["type"] == "response_item" and kind == "function_call":
                name = event.get("name") or ""
                namespace = event.get("namespace") or ""
                exposed_tools.add((namespace + "." if namespace else "") + name)
                if event["call_id"] == call["call_id"]:
                    arguments.append(json.loads(event["arguments"]))
                    submission_identity.add((namespace, name))
                    started = record["timestamp"]
            item = event.get("item") or {}
            if item.get("type") == "DynamicToolCall" and item.get("id") == call["call_id"]:
                arguments.append(item["arguments"])
                submission_identity.add((item.get("namespace") or "", item["tool"]))
                started = started or record["timestamp"]
    if not arguments or call["turn_id"] not in starts:
        raise ValueError("Original native submission arguments or official Turn start unavailable")
    if any(sha(canonical(["gx_submit_product", args])) != call["request_sha"] for args in arguments):
        raise ValueError("Native model submission differs from GX request hash")
    return {"thread_id": thread, "turn_id": call["turn_id"], "call_id": call["call_id"],
            "native_submission_observed": started, "request_sha256": call["request_sha"],
            "rollout_sha256": sha(path.read_bytes()), "logged_function_call_names": sorted(exposed_tools),
            "official_submission_names": [{"namespace": namespace or None, "name": name}
                                          for namespace, name in sorted(submission_identity)],
            "original_product": arguments[0]["product"]}


def attempts(folder):
    """Retain adverse or incomplete acquisitions, including numerical choices a worker only proposed."""
    freeze = json.loads((folder / "freeze.json").read_bytes())
    case_path = Path(__file__).with_name("case.json")
    case = json.loads(case_path.read_bytes())
    state = Path(freeze["private_state"])
    sessions = state.parent / ("codex-home" if freeze["mode"] == "live" else "codex") / "sessions"
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        def rows(sql, params=()):
            return [dict(r) for r in db.execute(sql, params)]
        index = {r["artifact_ref"]: r for r in rows("SELECT * FROM artifacts")}
        def body(ref):
            record = index[ref]
            blob = (state / "artifacts" / record["namespace"] / record["rel_path"]).read_bytes()
            if (len(blob), sha(blob)) != (record["bytes"], record["sha256"]):
                raise ValueError("Artifact differs from the primary index: " + ref)
            return json.loads(blob)
        proposals = {ref: body(ref) for ref, item in index.items() if item["kind"] == "research_proposal"}
        receipts, sources, routes = [], {}, []
        for src in rows("SELECT source_id,provenance_json FROM sources WHERE kind='operator_note'"):
            meta = json.loads(src["provenance_json"])
            ref = meta["review_artifact_ref"]
            note = body(ref)
            message, = rows("SELECT created_at,result_json FROM p0_operator_messages WHERE message_id=?",
                            (meta["client_message_id"],))
            assert src["source_id"] == note["source_id"] == json.loads(message["result_json"])["source_id"]
            sources[meta["client_message_id"]] = {"source_id": src["source_id"], "ref": ref,
                "sha256": index[ref]["sha256"], "at": message["created_at"], "text": note["text"]}
        for ref, record in index.items():
            if record["kind"] != "research_evidence":
                continue
            evidence = body(ref)
            proposal = proposals[evidence["proposal_ref"]]
            for route in evidence["routes"]:
                job, = rows("SELECT job_id,status,submitted_at,finished_at FROM jobs WHERE job_id=?", (route["job_id"],))
                points, evaluation, valid = [], None, False
                if route.get("observations_ref") and route.get("evaluation_ref"):
                    original = body(route["observations_ref"])
                    points = original["observations"]
                    evaluation = body(route["evaluation_ref"])
                    declared = body(route["input_ref"])
                    valid = (job["status"] == "SUCCEEDED" and route["outcome_kind"] == "valid_scientific_outcome"
                        and original["input_sha256"] == index[route["input_ref"]]["sha256"]
                        and evaluation["input_ref"] == route["observations_ref"]
                        and evaluation["input_sha256"] == index[route["observations_ref"]]["sha256"]
                        and [(v["candidate_id"], v["x"]) for v in points] == [(v["candidate_id"], v["x"]) for v in declared["candidates"]]
                        and all(fraction(v["score"]) == -(fraction(v["x"]) - 2) ** 2 for v in points)
                        and fraction(evaluation["threshold"]) == fraction(proposal["threshold"])
                        and evaluation["count_at_or_above"] == sum(v["score"] >= proposal["threshold"] for v in points)
                        and evaluation["max_score"] == max(v["score"] for v in points))
                routes.append({"round": proposal["round"], "path": route["path_id"], "job": job,
                    "proposal_ref": evidence["proposal_ref"], "evidence_ref": ref, "observations_ref": route.get("observations_ref"),
                    "evaluation_ref": route.get("evaluation_ref"), "points": points, "evaluation": evaluation, "valid": valid})
        for sub in rows("SELECT s.submission_id,s.created_at,s.result,s.validation_json,s.artifact_refs_json,"
                "s.attempt,l.principal,l.lease_id,t.role,t.task_id,json_extract(t.input_json,'$.round') AS round,"
                "r.brief,r.root_key,r.thread_id,r.model,r.effort,r.provider FROM submissions s JOIN tasks t USING(task_id) "
                "JOIN leases l ON l.lease_id=s.lease_id JOIN p0_roots r ON r.lease_id=l.lease_id ORDER BY s.rowid"):
            ref, = (r for r in json.loads(sub["artifact_refs_json"]) if index[r]["kind"] in ("science_assessment", "science_decision"))
            product = body(ref)
            link, = rows("SELECT * FROM p0_root_calls WHERE thread_id=? AND json_extract(result_json,'$.submission_id')=?",
                         (sub["thread_id"], sub["submission_id"]))
            native = native_record(sessions, sub["thread_id"], link)
            assert sub["principal"] == index[ref]["put_by"] == sub["thread_id"]
            brief = json.loads(sub["brief"].split("GX task brief\n", 1)[1])
            wanted = brief["input"]
            future = {r for r, entry in index.items() if entry["kind"] == "synthetic_grid" and entry["created_at"] > sub["created_at"]}
            read_refs = {r["artifact_ref"] for r in rows("SELECT artifact_ref FROM p0_root_artifact_reads WHERE root_key=?", (sub["root_key"],))}
            receipts.append({"role": sub["role"], "round": sub["round"], "attempt": sub["attempt"],
                "result": sub["result"], "submitted": sub["created_at"], "submission_id": sub["submission_id"],
                "lease_id": sub["lease_id"], "task_id": sub["task_id"], "ref": ref, "product_sha256": index[ref]["sha256"],
                "validation_records": json.loads(sub["validation_json"]).get("records", []), **native,
                "final_product": product, "proposal_author": proposals[wanted["proposal_ref"]]["author_principal"],
                "assigned_evidence": wanted["evidence_refs"], "assigned_proposal": wanted["proposal_ref"],
                "assigned_assessment": wanted.get("assessment_ref"), "assigned_source_ids": [s["source_id"] for s in wanted.get("supplement_sources", [])],
                "brief_sha256": sha(sub["brief"].encode()), "model": sub["model"], "effort": sub["effort"], "provider": sub["provider"],
                "later_observation_refs_in_brief": sorted(r for r in future if r in sub["brief"]),
                "later_observation_refs_in_reads": sorted(future & read_refs),
                "complete_rubric_entries_in_brief": [k for k, v in case["rubric"].items() if v in sub["brief"]]})
        initial = next((r for r in routes if r["round"] == 0 and r["path"] == "high" and r["valid"]), None)
        catalog = json.loads(re.search(r"\[[^\]]+\]", sources["follow-up-controller-catalog"]["text"]).group())
        predictions, maxima = interpolation(initial["points"], catalog) if initial else ([], {})
        return {"run": rows("SELECT run_id,status,created_at,updated_at FROM runs")[0], "routes": routes,
            "sources": sources, "submissions": receipts, "exact_interpolation": predictions, "maxima": maxima,
            "freeze_sha256": sha((folder / "freeze.json").read_bytes()), "case_sha256_matches": sha(case_path.read_bytes()) == freeze["case_sha256"],
            "public_spec_excludes_rubric": "rubric" not in freeze["model_visible_project"],
            "grader_in_staged_install": (state.parent / "install/acceptance/v31/quality/grade.py").exists(),
            "scientific_decisions": rows("SELECT * FROM science_decisions ORDER BY round_index"),
            "settlements": rows("SELECT round_index,path_id,kind,direction,hypothesis_id,evidence_id,job_id,submission_id FROM science_settlements ORDER BY rowid"),
            "turns": rows("SELECT thread_id,turn_id,budget_status,confidence,settled_tokens FROM p0_meter_native_turns")}


def inspect(folder):
    freeze = json.loads((folder / "freeze.json").read_bytes())
    case_file = Path(__file__).with_name("case.json")
    case = json.loads(case_file.read_bytes())
    state = Path(freeze["private_state"])
    sessions = state.parent / ("codex-home" if freeze["mode"] == "live" else "codex") / "sessions"
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        def rows(sql, params=()):
            return [dict(row) for row in db.execute(sql, params)]
        records = {r["artifact_ref"]: r for r in rows("SELECT * FROM artifacts")}
        cache = {}
        def artifact(ref):
            record = records[ref]
            path = state / "artifacts" / record["namespace"] / record["rel_path"]
            blob = path.read_bytes()
            if len(blob) != record["bytes"] or sha(blob) != record["sha256"]:
                raise ValueError("Artifact bytes differ for " + ref)
            cache[ref] = record["sha256"]
            return json.loads(blob)
        report_ref, = (ref for ref, item in records.items() if item["kind"] == "research_report")
        report = artifact(report_ref)
        jobs = {r["job_id"]: r for r in rows("SELECT job_id,status,submitted_at,finished_at,idempotency_key FROM jobs")}
        first_job_submit = min(j["submitted_at"] for j in jobs.values())
        freeze_precedes_execution = datetime.fromisoformat(freeze["frozen_utc"]) < datetime.fromisoformat(first_job_submit)
        public_spec = json.loads((state.parent / "spec.json").read_bytes())
        expected_project = {**case["project"], "workflow_version": public_spec["workflow_version"], "plugins": public_spec["plugins"]}
        isolation = {"case_digest_matches_freeze": sha(case_file.read_bytes()) == freeze["case_sha256"],
            "published_spec_exactly_matches_scientific_project": public_spec == freeze["model_visible_project"] == expected_project,
            "public_project_has_no_rubric": "rubric" not in public_spec,
            "private_install_contains_grader": (state.parent / "install/acceptance/v31/quality/grade.py").exists(),
            "frozen_before_initial_job_submitted": freeze_precedes_execution, "first_job_submitted": first_job_submit}
        sources = {}
        for src in rows("SELECT source_id,provenance_json FROM sources WHERE kind='operator_note'"):
            meta = json.loads(src["provenance_json"])
            source_ref = meta["review_artifact_ref"]
            message = rows("SELECT message_id,created_at,result_json FROM p0_operator_messages WHERE message_id=?",
                           (meta["client_message_id"],))[0]
            doc = artifact(source_ref)
            assert json.loads(message["result_json"])["source_id"] == doc["source_id"] == src["source_id"]
            sources[message["message_id"]] = {"at": message["created_at"], "id": src["source_id"],
                "ref": source_ref, "sha256": cache[source_ref], "text": doc["text"]}
        catalog = json.loads(re.search(r"\[[^\]]+\]", sources["follow-up-controller-catalog"]["text"]).group())
        rounds, samples, qualities = [], {}, []
        for stage in sorted(report["rounds"], key=lambda r: r["round"]):
            number = stage["round"]
            proposal = artifact(stage["proposal_ref"])
            evidence = artifact(stage["evidence_ref"])
            assert evidence["proposal_ref"] == stage["proposal_ref"] and proposal["round"] == number
            data = {"round": number, "proposal_ref": stage["proposal_ref"], "author": proposal["author_principal"],
                "evidence_ref": stage["evidence_ref"], "prior_decision_ref": proposal.get("previous_decision_ref"),
                "routes": [], "native_submissions": {}}
            for route in evidence["routes"]:
                path = route["path_id"]
                observed = artifact(route["observations_ref"])
                grid = artifact(route["input_ref"])
                evaluation = artifact(route["evaluation_ref"])
                points = observed["observations"]
                cutoff = fraction(proposal["threshold"])
                reaches = sum(fraction(point["score"]) >= cutoff for point in points)
                job = jobs[route["job_id"]]
                qualities.append(job["status"] == "SUCCEEDED" and route["outcome_kind"] == "valid_scientific_outcome"
                    and observed["input_sha256"] == cache[route["input_ref"]]
                    and [(r["candidate_id"], r["x"]) for r in points] == [(r["candidate_id"], r["x"]) for r in grid["candidates"]]
                    and all(fraction(r["score"]) == -(fraction(r["x"]) - 2) ** 2 for r in points)
                    and evaluation["input_ref"] == route["observations_ref"]
                    and evaluation["input_sha256"] == cache[route["observations_ref"]]
                    and fraction(evaluation["threshold"]) == cutoff and evaluation["count_at_or_above"] == reaches
                    and fraction(evaluation["max_score"]) == max(fraction(r["score"]) for r in points))
                samples[number, path] = points
                data["routes"].append({"path": path, "job": job, "grid_ref": route["input_ref"],
                    "observations_ref": route["observations_ref"], "evaluation_ref": route["evaluation_ref"],
                    "cutoff": str(cutoff), "crossings": reaches, "measured": points})
            stored = rows("SELECT * FROM science_decisions WHERE round_index=?", (number,))[0]
            data["decision_created"] = stored["created_at"]
            data["decision_stop_reason"] = stored["stop_reason"]
            for role, source_col, ref_col in [("evidence_reviewer", "reviewer_submission_id", "assessment_ref"),
                                              ("planner", "submission_id", "artifact_ref")]:
                ref, sid = stage[ref_col], stored[source_col]
                product = artifact(ref)
                worker, = rows("SELECT s.task_id,s.result,s.created_at,s.attempt,s.lease_id,l.principal,"
                    "s.artifact_refs_json,r.role,r.model,r.provider,r.effort,r.thread_id,r.brief,r.root_key "
                    "FROM submissions s JOIN leases l USING(lease_id) JOIN p0_roots r USING(lease_id) WHERE s.submission_id=?", (sid,))
                call, = rows("SELECT thread_id,turn_id,call_id,request_sha FROM p0_root_calls WHERE thread_id=? "
                    "AND json_extract(result_json,'$.submission_id')=?", (worker["thread_id"], sid))
                native = native_record(sessions, worker["thread_id"], call)
                qualities.append(worker["result"] == "ACCEPTED" and worker["role"] == role
                    and worker["principal"] == worker["thread_id"] == records[ref]["put_by"]
                    and ref in json.loads(worker["artifact_refs_json"]) and product["proposal_ref"] == stage["proposal_ref"])
                if role == "evidence_reviewer":
                    qualities.append(product["evidence_refs"] == [stage["evidence_ref"]]
                                     and worker["principal"] != proposal["author_principal"])
                else:
                    qualities.append(product["assessment_ref"] == stage["assessment_ref"]
                                     and data["native_submissions"]["evidence_reviewer"]["thread_id"] != worker["thread_id"])
                reads = rows("SELECT artifact_ref FROM p0_root_artifact_reads WHERE root_key=?", (worker["root_key"],))
                data["native_submissions"][role] = {**native, "submitted": worker["created_at"], "task_id": worker["task_id"],
                    "submission_id": sid, "lease_id": worker["lease_id"], "attempt": worker["attempt"], "ref": ref,
                    "proof_of_artifact_authorship": records[ref]["put_by"], "sha256": cache[ref], "provider": worker["provider"],
                    "model": worker["model"], "effort": worker["effort"], "read_refs": [r["artifact_ref"] for r in reads],
                    "brief_sha256": sha(worker["brief"].encode()), "final_product": product}
                if number == 0:
                    held_out_refs = [ref for ref, record in records.items()
                                     if record["kind"] == "synthetic_grid" and record["created_at"] > worker["created_at"]]
                    data["native_submissions"][role]["original_visibility"] = {
                        "complete_rubric_strings_present": [key for key, value in case["rubric"].items() if value in worker["brief"]],
                        "later_observation_refs_in_brief": [ref for ref in held_out_refs if ref in worker["brief"]],
                        "later_observation_refs_in_root_reads": [r["artifact_ref"] for r in reads if r["artifact_ref"] in held_out_refs]}
            rounds.append(data)
        predictions, maxima = interpolation(samples[0, "high"], catalog)
        held_out = {s["candidate_id"]: s for s in samples[1, "high"]}
        difference = fraction(held_out["quadratic"]["score"]) - fraction(held_out["linear"]["score"])
        prior = rounds[0]["native_submissions"]["planner"]
        selected = {c["candidate_id"]: str(fraction(c["x"])) for c in
                    prior["original_product"]["next_grids"]["high"]["candidates"]}
        qualities.append(rounds[1]["prior_decision_ref"] == prior["ref"] and
            all(selected[m] == str(fraction(held_out[m]["x"])) and maxima[m] == [selected[m]] for m in maxima)
            and all(fraction(c["x"]) not in {fraction(p["x"]) for key, ps in samples.items() if key[0] == 0 for p in ps}
                    for c in held_out.values()) and prior["submitted"] < rounds[1]["routes"][0]["job"]["submitted_at"])
        return {"freeze_sha256": sha((folder / "freeze.json").read_bytes()), "mode": freeze["mode"], "run_id": report["run_id"],
            "report_ref": report_ref, "script_sha256": sha(Path(__file__).read_bytes()),
            "frozen_inputs": isolation, "all_measurement_and_ownership_checks": all(qualities), "sources": sources, "rounds": rounds,
            "exact_fraction_predictions_from_observed_points": predictions, "unique_discrete_maxima": maxima,
            "prospectively_chosen_coordinates": selected, "held_out_quadratic_minus_linear": str(difference),
            "settlements": rows("SELECT round_index,path_id,hypothesis_id,evidence_id,job_id,kind,direction FROM science_settlements"),
            "stop_reason": report["stop_reason"], "verified_artifact_digests": cache}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--attempts", action="store_true", help="Read all original attempts, including incomplete acquisitions")
    args = parser.parse_args()
    print(json.dumps((attempts if args.attempts else inspect)(args.evidence), indent=2, ensure_ascii=False))
