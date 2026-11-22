"""Verify the completed public-CLI run independently, from read-only GX state."""
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3


def require(condition, detail):
    if not condition:
        raise AssertionError(detail)


def rows(conn, sql):
    return [dict(row) for row in conn.execute(sql)]


def check_native_charges(state_snapshot, expected_reported_tokens):
    usage = state_snapshot["usage"]
    require(all(r["terminal_status"] in ("completed", "interrupted", "failed") and
                r["total_tokens"] is not None for r in usage),
            "GX returned with incomplete official turn usage")
    by_thread = {}
    for receipt in usage:
        tid = receipt["thread_id"]
        by_thread[tid] = max(by_thread.get(tid, 0), receipt["total_tokens"])
    reported = sum(by_thread.values())
    ledger = sum(json.loads(r["spent_json"]).get("tokens", 0) for r in state_snapshot["budgets"])
    calls = sum(json.loads(r["spent_json"]).get("api_calls", 0) for r in state_snapshot["budgets"])
    require(reported == expected_reported_tokens, f"Codex accounted {reported}, fixture delivered {expected_reported_tokens}")
    require(ledger == reported and calls == len(usage),
            f"GX ledger has {ledger} tokens and {calls} turns for {reported} observed tokens / {len(usage)} turns")
    require(all(r["status"] == "SETTLED" for r in state_snapshot["native_turn_reservations"]),
            "completed or interrupted native turn reservation has not been settled")
    return {"tokens": ledger, "native_turns": calls, "reported_tokens": reported}


def audit(state):
    files = list(Path(state).rglob("gx.db"))
    require(len(files) == 1, f"expected one project database, found {len(files)}")
    path, = files
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        artifacts = rows(conn, "SELECT artifact_ref,kind,namespace,rel_path,bytes,sha256 FROM artifacts")
        refs = {}
        for a in artifacts:
            raw = (path.parent / "artifacts" / a["namespace"] / a["rel_path"]).read_bytes()
            require(len(raw) == a["bytes"] and hashlib.sha256(raw).hexdigest() == a["sha256"],
                    "artifact hash/size disagrees with GX: " + a["artifact_ref"])
            refs[a["artifact_ref"]] = raw

        def read(ref):
            return json.loads(refs[ref])

        reports = [a for a in artifacts if a["kind"] == "research_report"]
        require(len(reports) == 1, "expected one research report")
        report = read(reports[0]["artifact_ref"])
        history = report["rounds"]
        require([r["round"] for r in history] == [0, 1, 2], "expected three distinct ordered rounds")
        jobs = rows(conn, "SELECT job_id,status,scheduler_ref,idempotency_key,input_manifest_ref,measured_json,submitted_at,finished_at "
                          "FROM jobs ORDER BY rowid")
        require(len(jobs) == 4 and all(j["status"] == "SUCCEEDED" and j["scheduler_ref"] for j in jobs),
                "expected successful CPU jobs for two first-round paths and both follow-ups")
        submissions = rows(conn, "SELECT t.task_id,t.stage,t.attempt,"
                           "json_extract(t.input_json,'$.round') AS round,l.principal,s.submission_id "
                           "FROM tasks t JOIN submissions s ON s.task_id=t.task_id "
                           "JOIN leases l ON l.lease_id=s.lease_id WHERE t.status='ACCEPTED' "
                           "AND s.result='ACCEPTED' AND t.attempt=s.attempt ORDER BY t.rowid")
        require(len(submissions) == 6 and len({s["principal"] for s in submissions}) == 6,
                "expected one author per accepted worker Root")
        roots = rows(conn, "SELECT task_id,attempt,thread_id,role,model,effort,goal_desired "
                           "FROM p0_roots ORDER BY rowid")
        deliveries = rows(conn, "SELECT event_id,thread_id,turn_id,state,method,human FROM p0_root_deliveries ORDER BY rowid")
        usage = rows(conn, "SELECT thread_id,turn_id,total_tokens,terminal_status FROM p0_root_turn_usage ORDER BY rowid")
        require(len(roots) == 7 and len({r["thread_id"] for r in roots}) == 7, "expected main plus six worker Roots")
        worker_ids = {r["thread_id"] for r in roots if r["task_id"] is not None}
        require({s["principal"] for s in submissions} == worker_ids, "submission principal and real worker Root differ")
        require(all(r["effort"] == "xhigh" for r in roots), "model reasoning profile changed")
        require(all(d["turn_id"] and d["state"] == "acknowledged" for d in deliveries)
                and {d["thread_id"] for d in deliveries} == {r["thread_id"] for r in roots},
                "Root without official Codex delivery receipt")
        require(Counter((s["round"], s["stage"]) for s in submissions)
                == Counter((i, role) for i in range(3) for role in ("assessment", "decision")),
                "expected an independent assessment and decision per round")
        by_job = {j["job_id"]: j for j in jobs}
        rounds = []
        for index, entry in enumerate(history):
            proposal = read(entry["proposal_ref"])
            review = read(entry["assessment_ref"])
            decision = read(entry["artifact_ref"])
            evidence = read(entry["evidence_ref"])
            require(review["evidence_refs"] == [entry["evidence_ref"]] and
                    review["proposal_ref"] == decision["proposal_ref"] == entry["proposal_ref"] and
                    decision["assessment_ref"] == entry["assessment_ref"], "broken scientific provenance")
            if index:
                require(proposal["previous_decision_ref"] == history[index - 1]["artifact_ref"],
                        "proposal did not consume the preceding decision")
            routes = []
            for route in evidence["routes"]:
                points = read(route["observations_ref"])
                grid = read(route["input_ref"])
                require(points["input_sha256"] == hashlib.sha256(refs[route["input_ref"]]).hexdigest(),
                        "worker consumed a different grid from the assigned route")
                measured = points["observations"]
                require([(p["candidate_id"], p["x"]) for p in measured] ==
                        [(p["candidate_id"], p["x"]) for p in grid["candidates"]],
                        "candidate mismatch between observations and frozen grid")
                require(all(p["score"] == -(p["x"] - 2)**2 for p in measured),
                        "grid result disagrees with independent numerical recomputation")
                finding, = [f for f in review["findings"] if f["path_id"] == route["path_id"]]
                require(finding["supported"] == (max(p["score"] for p in measured) >= proposal["threshold"]),
                        "review verdict disagrees with the prespecified threshold")
                require(finding.get("evaluation_ref") and finding["evaluation_ref"] == route["evaluation_ref"],
                        "review has no frozen evaluation artifact")
                require(route["job_id"] in by_job and route["status"] == "SUCCEEDED", "bad job reference")
                routes.append({"path_id": route["path_id"], "job_id": route["job_id"],
                               "prediction": next(p["prediction"] for p in proposal["paths"] if p["id"] == route["path_id"]),
                               "evaluation_ref": route["evaluation_ref"], "supported": finding["supported"],
                               "observations": measured})
            rounds.append({"round": index, "threshold": proposal["threshold"], "routes": routes, "decision": decision})
        r0, r1, r2 = rounds
        path_ids = lambda r: {route["path_id"] for route in r["routes"]}
        require(len(path_ids(r0)) == 2 and {r["supported"] for r in r0["routes"]} == {True, False},
                "required supported and refuted competing hypotheses in R0")
        continued = r0["decision"]["continue_paths"]
        require(len(continued) == 1 and set(continued) == path_ids(r1), "R0 decision did not prune and continue")
        pivot, = r1["decision"]["pivots"]
        require(pivot["from_path"] in path_ids(r1) and {pivot["id"]} == path_ids(r2)
                and r2["routes"][0]["prediction"] == pivot["prediction"], "pivot was not executed")
        require(all(p["path_id"] != pivot["id"] for p in r0["routes"]) and
                not r2["decision"]["continue_paths"] and not r2["decision"].get("pivots")
                and report["stop_reason"], "final decision didn't close the investigation")
        decisions = rows(conn, "SELECT round_index,submission_id,reviewer_submission_id,actions_json,decision_ref "
                              "FROM science_decisions ORDER BY round_index")
        require([d["decision_ref"] for d in decisions] == [e["artifact_ref"] for e in history],
                "decision index and research report disagree")
        require([sorted(a["action"] for a in json.loads(d["actions_json"])) for d in decisions]
                == [["continue", "prune"], ["pivot"], ["stop"]], "canonical decision history mismatch")
        settlements = rows(conn, "SELECT s.round_index,s.path_id,s.hypothesis_id,s.job_id,s.submission_id,s.kind,"
                           "s.direction,s.eval_id,s.evidence_id,s.manifest_hash,e.artifact_refs_json,"
                           "e.eval_spec_id,e.eval_spec_version,e.frozen_sha,spec.frozen_sha AS spec_sha,"
                           "h.head_revision FROM science_settlements s JOIN evaluations e USING(eval_id) "
                           "JOIN eval_specs spec ON spec.eval_spec_id=e.eval_spec_id AND spec.version=e.eval_spec_version "
                           "JOIN evidence v USING(evidence_id) JOIN hypotheses h ON h.hypothesis_id=s.hypothesis_id "
                           "WHERE spec.status='FROZEN' ORDER BY s.round_index,s.path_id")
        require(len(settlements) == 4 and all(s["kind"] == "valid_scientific_outcome" and
                s["spec_sha"] == s["frozen_sha"] for s in settlements), "missing frozen evaluation or settlement")
        manifests = rows(conn, "SELECT * FROM p0_execution_manifests ORDER BY job_id")
        require({m["job_id"] for m in manifests} == set(by_job), "missing execution manifest")
        for manifest in manifests:
            for prefix in ("admission", "completion"):
                document = json.loads(manifest.pop(prefix + "_json"))
                canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                require(hashlib.sha256(canonical.encode()).hexdigest() == manifest[prefix + "_sha256"],
                        f"execution manifest {prefix} hash mismatch")
                manifest[prefix] = document
        for s in settlements:
            route = next(r for r in rounds[s["round_index"]]["routes"] if r["job_id"] == s["job_id"])
            require(s["direction"] == ("supports" if route["supported"] else "refutes")
                    and json.loads(s["artifact_refs_json"])[1] == route["evaluation_ref"], "wrong canonical Evidence")
        holds = rows(conn, "SELECT subject_id,status,amounts_json FROM budget_reservations "
                          "WHERE subject_kind='native_turn' ORDER BY rowid")
        budgets = rows(conn, "SELECT spent_json,reserved_json FROM budgets ORDER BY rowid")
        outbox = rows(conn, "SELECT outbox_id,topic,attempts,consumed_at FROM outbox ORDER BY outbox_id")
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts = {table: conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in tables}
        return dict(run_id=report["run_id"], report_ref=reports[0]["artifact_ref"], rounds=rounds,
                    stop_reason=report["stop_reason"], jobs=jobs, submissions=submissions, budgets=budgets, native_turn_reservations=holds,
                    outbox=outbox, roots=roots, deliveries=deliveries, usage=usage, decisions=decisions, settlements=settlements, manifests=manifests, table_counts={k: v for k, v in counts.items() if v},
                    artifact_hashes={a["artifact_ref"]: a["sha256"] for a in artifacts})
