"""Cross-check official-host deliveries, graph decisions and measured subprocess results."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from peer import BASELINE, BASELINE_RATIONALE, COVERAGE, FALLBACK, INVENTORY, METHOD, SURVEY


def digest(data):
    return hashlib.sha256(data).hexdigest()


def db_read(path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def artifact(conn, slot, run, ref):
    row = conn.execute("SELECT * FROM artifacts WHERE run_id=? AND artifact_ref=? AND published=1",
                       (run, ref)).fetchone()
    assert row is not None, ref
    path = slot / "state/artifacts" / row["namespace"] / row["rel_path"]
    assert digest(path.read_bytes()) == row["sha256"], ref
    return row, path


def native_usage(slot, thread, brief_text, call_ids):
    rollout, = (slot / "codex/sessions").rglob(f"*-{thread}.jsonl")
    delivered, count, seen = [], None, []
    with rollout.open() as stream:
        for line in stream:
            event = json.loads(line)
            payload = event["payload"]
            if payload.get("type") == "token_count" and payload.get("info"):
                count = payload["info"]["total_token_usage"]
            if event["type"] == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                delivered.extend(p.get("text", "") for p in payload.get("content", []) if "GX research brief\n" in p.get("text", ""))
            if event["type"] == "response_item" and payload.get("type") == "function_call":
                seen.append(payload["call_id"])
    assert brief_text in delivered and sorted(call_ids) == sorted(seen), (rollout, len(delivered), seen)
    assert count and count["total_tokens"] == count["input_tokens"] + count["output_tokens"]
    return {"rollout": str(rollout.relative_to(slot)), "rollout_sha256": digest(rollout.read_bytes()),
            "fixture_usage": count}


def snapshot(slot, rid, arm, wire, oracle):
    from gx_core.domain import ids
    from gx_core.native_release import native_state
    from gx_core.p0 import v62_maturity
    with native_state(slot / "state"), closing(db_read(slot / "state/gx.db")) as conn:
        def rows(sql):
            return [dict(r) for r in conn.execute(sql, (rid,))]

        requests = {entry["call_id"]: entry for entry in wire}
        assert len(requests) == len(wire)
        native_calls = rows("""SELECT c.call_id,c.thread_id,c.turn_id,c.result_json FROM p0_root_calls c
                       JOIN p0_roots r USING(thread_id) WHERE r.run_id=? ORDER BY c.rowid""")
        assert set(requests) == {c["call_id"] for c in native_calls}
        turns = []
        for root in rows("""SELECT r.thread_id,r.task_id,r.lease_id,r.brief,r.effective_profile_json,
                    d.turn_id,t.scope,t.context_ref,c.event_cursor,t.base_json,
                    m.confidence,m.budget_status,m.settled_tokens FROM p0_roots r
                    JOIN v6_decision_tasks t USING(task_id) JOIN research_contexts c USING(context_ref)
                    JOIN p0_root_deliveries d ON d.thread_id=r.thread_id
                    JOIN p0_meter_native_turns m ON m.thread_id=r.thread_id AND m.turn_id=d.turn_id
                    WHERE r.run_id=? ORDER BY m.sequence"""):
            access = json.loads(root["effective_profile_json"])
            runtime = access["research_access"]
            assert access["provider"] == "gx_loopback" and runtime["environments"] == [] and not runtime["visible_files"]
            assert runtime["sandbox"]["type"] == "readOnly" and runtime["sandbox"]["networkAccess"] is False
            assert root["budget_status"] == "SETTLED"
            items = [requests[c["call_id"]] for c in native_calls if c["thread_id"] == root["thread_id"]]
            raw = root["brief"]
            assert items and all(raw == e["brief_text"] for e in items)
            assert all(set(e["tools"]) - {"gx"} == set(runtime["dynamic_tools"]) for e in items)
            record = native_usage(slot, root["thread_id"], raw, [c["call_id"] for c in items])
            assert record["fixture_usage"]["total_tokens"] == root["settled_tokens"]
            turns.append({"scope": root["scope"], "thread_id": root["thread_id"], "turn_id": root["turn_id"],
                          "lease_id": root["lease_id"], "task_id": root["task_id"], "context_ref": root["context_ref"],
                          "event_cursor": root["event_cursor"], "base": json.loads(root["base_json"] or "null"),
                          "brief_sha256": digest(raw.encode()), "frozen_brief_utf8_bytes": len(raw.encode()),
                          "frozen_brief_tokens": None, "model": access["model"], "effort": access["effort"],
                          "tool_count": len(runtime["dynamic_tools"]), "response_requests": len(items),
                          "calls": [c["call_id"] for c in items], **record})
        jobs = []
        for row in rows("""SELECT n.node_id,n.spec_json,n.branch_id,n.result_ref,n.decision_ref,n.operation_key,
                           n.exec_key,j.job_id,j.status,j.capability_id,j.measured_json,j.reservation_ref
                           FROM v6_nodes n JOIN jobs j ON j.job_id=n.job_id AND n.run_id=j.run_id
                           WHERE n.run_id=? ORDER BY j.rowid"""):
            assert row["status"] == "SUCCEEDED" and row["capability_id"] + "@1" == METHOD
            assert row["reservation_ref"] and row["operation_key"] and row["exec_key"]
            assert row["result_ref"] is not None
            output, output_path = artifact(conn, slot, rid, row["result_ref"])
            assert output["job_id"] == row["job_id"] and output["origin"] == "JOB_OUTPUT"
            result = json.loads(output_path.read_bytes())
            spec = json.loads(row["spec_json"])
            assert spec["parameters"] == {"mask": result["mask"]} and spec["capability"] == METHOD
            source, input_path = artifact(conn, slot, rid, spec["inputs"][0])
            assert source["sha256"] == result["input_sha256"] and source["kind"] == "run_input"
            check, = oracle(input_path, [result])
            outcome = json.loads((slot / "state/jobs" / row["job_id"] / "outcome.json").read_bytes())
            assert outcome["started"] and outcome["tool_pid"] > 0 and outcome["exit_code"] == 0
            jobs.append({"node_id": row["node_id"], "job_id": row["job_id"], "branch_id": row["branch_id"],
                         "decision_ref": row["decision_ref"], "entry": result["entry"], "mask": result["mask"],
                         "selected_positions": result["selected_residues"], "observed_positions": result["observed_union_residues"],
                         "selected_atoms": result["selected_atoms"],
                         "input_ref": source["artifact_ref"], "input_sha256": source["sha256"],
                         "result_ref": row["result_ref"], "sha256": output["sha256"],
                         "measured": json.loads(row["measured_json"]), "tool_pid": outcome["tool_pid"],
                         "independent_fit": check})
        assert len(rows("SELECT job_id FROM jobs WHERE run_id=?")) == len(jobs)
        decisions = []
        for d in rows("SELECT * FROM v6_decisions WHERE run_id=? ORDER BY rowid"):
            req, receipt = json.loads(d["request_json"]), json.loads(d["receipt_json"])
            assert req["decision"] == requests[d["call_id"]]["args"]["decision"]
            assert receipt == json.loads(next(c["result_json"] for c in native_calls if c["call_id"] == d["call_id"]))
            decisions.append({"decision_ref": d["decision_ref"], "scope": req["scope"], "task_id": d["task_id"],
                              "thread_id": d["thread_id"], "turn_id": d["turn_id"], "call_id": d["call_id"],
                              "context_ref": d["context_ref"], "before": req["base"], "after": receipt["result_head"],
                              "rationale": req["decision"]["rationale"], "evidence_refs": req["decision"]["evidence_refs"],
                              "operations": req["decision"]["ops"]})
        claims = rows("""SELECT h.head_revision,r.document_json,r.sha256 FROM hypothesis_revisions r
                         JOIN hypotheses h USING(hypothesis_id) WHERE h.run_id=? ORDER BY r.revision""")
        claim, = claims
        doc = json.loads(claim["document_json"])
        assert ids.sha256_of_obj(doc) == claim["sha256"]
        assert claim["head_revision"] == doc["revision"] == 1
        final_job, = [j for j in jobs if j["entry"] == "2O0O"]
        assert doc["evidence_refs"] == [final_job["result_ref"]]
        assert doc["decision_id"] == decisions[-1]["decision_ref"]
        maturity = doc.get("maturity")
        if arm == "quick":
            assert maturity is None
        else:
            assert maturity["stage"] in ("idea", "grounded") and maturity["reasons"], maturity
            assert v62_maturity.recompute(conn, rid, doc["claim_revision_id"]) == maturity
        reports = rows("""SELECT r.*,e.ts AS published_at FROM v62_module_reports r JOIN events e
                           ON e.event_id=json_extract(r.receipt_json,'$.event_id') WHERE r.run_id=?""")
        wakes = rows("""SELECT w.wake_key,w.round,w.event_id,e.kind,e.payload_json,e.ts
                         FROM v62_module_wakes w JOIN events e USING(event_id) WHERE w.run_id=? ORDER BY w.event_id""")
        published = []
        for row in reports:
            receipt = json.loads(row["receipt_json"])
            data, orig = artifact(conn, slot, rid, row["report_ref"])
            assert data["sha256"] == receipt["report_sha256"] and row["version"] == 1
            bundle = json.loads(orig.read_bytes())
            submitted, = [e for e in wire if e["tool"] == "gx_submit_module_report" and
                          e["call_id"] == receipt["author"]["call_id"]]
            assert submitted["args"]["findings"] == bundle["findings"] and bundle["status"] == "COMPLETE"
            visible = [e for e in wire if e["brief"]["scope"] == "run" and
                       any(r["report_ref"] == row["report_ref"] for r in e["brief"].get("module_reports", []))]
            first = visible[0]
            assert first["utc"] >= row["published_at"] and first["monotonic"] > submitted["monotonic"]
            early = [e["brief"] for e in wire if e["brief"]["scope"] == "run" and e["monotonic"] < first["monotonic"]]
            assert early and all(row["report_ref"] not in json.dumps(b) and
                                 all(f["text"] not in json.dumps(b) for f in bundle["findings"]) for b in early)
            # Published evidence is cited in a newly adopted decision which actually commissioned the next Job.
            chosen, = [d for d in decisions if d["decision_ref"] == final_job["decision_ref"]]
            assert chosen["evidence_refs"] == [row["report_ref"]] and chosen["call_id"] == first["call_id"]
            assert chosen["thread_id"] != receipt["author"]["thread_id"]
            assert all(j["decision_ref"] != final_job["decision_ref"] for j in jobs if j is not final_job)
            original, = [d for d in decisions if d["rationale"] == BASELINE_RATIONALE]
            original_branch, = [b for b in rows("SELECT branch_id,spec_json FROM v6_branches WHERE run_id=?")
                                if json.loads(b["spec_json"]).get("method_family") == f"2O0O {BASELINE} coverage"]
            changed = any(op["op"] == "stop_branch" for op in chosen["operations"])
            if arm in {"change", "maintain"}:
                pilot, = [j for j in jobs if j["entry"] == "2RE9"]
                fact, = bundle["findings"]
                match = COVERAGE.fullmatch(fact["text"])
                assert fact["source_refs"] == [pilot["result_ref"]] and match
                assert all(pilot["result_ref"] not in json.dumps(b) for b in early)
                assert tuple(map(int, match.groups()[1:])) == (pilot["selected_positions"], pilot["observed_positions"])
                assert changed == (5 * pilot["selected_positions"] < 3 * pilot["observed_positions"]) == (arm == "change")
                assert final_job["mask"] == (FALLBACK if changed else BASELINE)
            assert (original_branch["branch_id"] != final_job["branch_id"]) == changed
            assert len(wakes) == 1 and json.loads(wakes[0]["payload_json"])["report_refs"] == [row["report_ref"]]
            assert next(t["event_cursor"] for t in turns if t["context_ref"] == chosen["context_ref"]) >= wakes[0]["event_id"]
            opened = next(e for e in wire if e["tool"] == "gx_open_module")
            published.append({"ref": row["report_ref"], "sha256": data["sha256"], "receipt": receipt,
                              "findings": bundle["findings"], "published_at": row["published_at"],
                              "delivered_at": first["utc"], "delivered_call": first["call_id"],
                              "delegation_to_submission_seconds": round(submitted["monotonic"] - opened["monotonic"], 6),
                              "decision_ref": chosen["decision_ref"], "initial_decision_ref": original["decision_ref"],
                              "original_branch": original_branch["branch_id"], "action": "change" if changed else "maintain",
                              "followup_job": final_job["job_id"]})
        if arm == "quick":
            assert not published and not wakes and len(jobs) == 1 and final_job["mask"] == BASELINE
            assert all(t["scope"] == "run" for t in turns)
        else:
            assert len(published) == 1 and published[0]["receipt"]["module_id"] == (SURVEY if arm == "medium" else INVENTORY)
            assert len(jobs) == (1 if arm == "medium" else 2)
        versions = rows("SELECT version,sha256,parent_sha,decision_ref,graph_json FROM v6_graph_versions WHERE run_id=? ORDER BY version")
        for v in versions:
            assert ids.sha256_of_obj(json.loads(v.pop("graph_json"))) == v["sha256"]
        return {"run_id": rid, "turns": turns, "jobs": jobs, "decisions": decisions,
                "module_reports": published, "round_wakes": wakes,
                "claim": {"sha256": claim["sha256"], "revision": doc["claim_revision_id"],
                          "statement": doc["statement"], "maturity": maturity},
                "graph_versions": versions}
