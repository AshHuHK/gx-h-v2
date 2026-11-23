"""Read-only reconciliation of a paid acquisition against official rollouts and GX."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

from runner import save, sha
from audit_workload import quantile, read_artifact, verify_science


def rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args)]


def wall(start, end):
    return round((datetime.fromisoformat(end.replace("Z", "+00:00")) -
                  datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds(), 3)


def write_rows(path, data):
    with path.open("wb") as file, gzip.GzipFile(filename="", mode="wb", fileobj=file, mtime=0) as stream:
        for row in data:
            stream.write((json.dumps(row, sort_keys=True) + "\n").encode())


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def catalog(tools):
    """Record the actual catalog location and the exact namespaced terminal leaf."""
    leaves = [child for spec in tools if spec.get("type") == "namespace" and spec.get("name") == "gx"
              for child in spec.get("tools") or [] if child.get("type") == "function" and child.get("name") == "gx_submit_product"]
    return {"sha256": digest_json(tools), "entries": len(tools),
            "direct_gx_submit_schema_sha256": sorted(digest_json(child) for child in leaves)}


def registered_tool_names(tools):
    names = []
    for tool in tools:
        if tool.get("type") == "namespace":
            names.extend({"namespace": tool.get("name"), "tool": child.get("name")} for child in tool.get("tools") or [])
        else:
            names.append({"namespace": tool.get("namespace"), "tool": tool.get("name")})
    return names


def native_usage(work, threads=None):
    by_thread = {}
    for file in sorted((work / "codex-home/sessions").rglob("*.jsonl")):
        if threads is not None and not any(file.stem.endswith("-" + tid) for tid in threads):
            continue
        with file.open() as stream:
            header = json.loads(next(stream))
            assert header["type"] == "session_meta"
            thread = header["payload"]["id"]
            metadata = header["payload"]
            entry = by_thread.setdefault(thread, {
                "rollout_sha256": [], "responses": {}, "compactions": 0, "turns": {},
                "artifact_read_failures": Counter(), "direct_product_responses": {}, "direct_product_output": {},
                "model_findings": {}, "direct_leaf_positions": {},
                "codex_base_instructions_sha256": digest_json(metadata.get("base_instructions")),
                "initial_dynamic_tools_sha256": digest_json(metadata.get("dynamic_tools")),
                "initial_dynamic_tools": registered_tool_names(metadata.get("dynamic_tools") or []),
                "multi_agent_version": metadata.get("multi_agent_version"),
                "gx_developer_prefix_sha256": [], "brief_messages_by_role": Counter(),
                "persisted_additional_tools": [], "persisted_request_top_level_tools": []})
            entry["rollout_sha256"].append(sha(file))
            active_turn = None
            pending_direct = []
            for line in stream:
                event = json.loads(line)
                body = event.get("payload") or {}
                kind = body.get("type") if isinstance(body, dict) else None
                if event["type"] == "compacted":
                    entry["compactions"] += 1
                elif event["type"] == "response_item" and kind == "additional_tools" and isinstance(body.get("tools"), list):
                    entry["persisted_additional_tools"].append(catalog(body["tools"]))
                elif event["type"] in ("model_request", "provider_request"):
                    request = body.get("body", body)
                    if isinstance(request.get("tools"), list):
                        entry["persisted_request_top_level_tools"].append(catalog(request["tools"]))
                    for input_item in request.get("input") or []:
                        if isinstance(input_item, dict) and input_item.get("type") == "additional_tools" and isinstance(input_item.get("tools"), list):
                            entry["persisted_additional_tools"].append(catalog(input_item["tools"]))
                elif event["type"] == "response_item" and kind == "message" and body.get("role") in ("developer","user"):
                    for item in body.get("content") or []:
                        if isinstance(item, dict) and isinstance(text := item.get("text"), str):
                            if "GX task brief\n" in text:
                                entry["brief_messages_by_role"][body["role"]] += 1
                            if body["role"] == "developer" and (
                                "independent worker for this GX task" in text or "Base your product on the cited evidence" in text):
                                prefix = text.split("GX task brief\n", 1)[0].strip()
                                entry["gx_developer_prefix_sha256"].append(hashlib.sha256(prefix.encode()).hexdigest())
                elif (event["type"] == "response_item" and kind == "function_call"
                      and (body.get("namespace"), body.get("name")) == ("gx", "gx_submit_product")):
                    pending_direct.append(body.get("call_id"))
                    entry["direct_product_output"][body.get("call_id")] = "not_seen"
                    entry["direct_leaf_positions"][body.get("call_id")] = sum(
                        turn == active_turn for turn, _ in entry["responses"])
                    try:
                        submitted = json.loads(body["arguments"]).get("product") or {}
                    except (TypeError, ValueError, KeyError):
                        submitted = {}
                    model_findings = submitted.get("findings_by_path") if isinstance(submitted, dict) else None
                    if isinstance(model_findings, dict) and all(isinstance(f, dict) for f in model_findings.values()):
                        entry["model_findings"][body.get("call_id")] = {key: {
                            field: digest_json(value) for field, value in f.items()} for key, f in model_findings.items()}
                elif (event["type"] == "response_item" and kind == "function_call_output"
                      and body.get("call_id") in entry["direct_product_output"]):
                    entry["direct_product_output"][body["call_id"]] = (
                        "codex_canceled_before_receiving_response"
                        if body.get("output") == "dynamic tool call was cancelled before receiving a response"
                        else "other_tool_output")
                elif event["type"] == "token_usage_record":
                    rid = body["response_id"]
                    key = (body["turn_id"], rid)
                    counters = {k: v for k, v in (body.get("usage") or {}).items() if type(v) is int}
                    previous = entry["responses"].get(key)
                    if previous and previous["usage"] != counters:
                        raise ValueError("Conflicting official usage for " + rid)
                    entry["responses"][key] = {"time": event["timestamp"], "usage": counters}
                    for call_id in pending_direct:
                        if call_id and body["turn_id"] == active_turn:
                            entry["direct_product_responses"][call_id] = rid
                    pending_direct.clear()
                elif event["type"] == "event_msg":
                    if kind in ("task_started", "turn_started"):
                        active_turn = body["turn_id"]
                        pending_direct.clear()
                        entry["turns"].setdefault(active_turn, {})["started"] = event["timestamp"]
                    elif kind in ("task_complete", "turn_complete", "turn_aborted", "task_aborted"):
                        entry["turns"].setdefault(body["turn_id"], {}).update(
                            ended=event["timestamp"], native_end=kind)
                    elif kind == "item_completed" and (item := body.get("item", {})).get("type") == "DynamicToolCall":
                        if item.get("tool") == "gx_read_artifact" and item.get("success") is False:
                            # Keep private provider output private. Distinguish a fenced read from a bad ref.
                            error = json.dumps(item.get("content_items")).lower()
                            if any(s in error for s in ("ownership", "owns the", "lease")):
                                reason = "fenced"
                            elif "not been cited" in error:
                                reason = "outside_cited_task_scope"
                            elif any(s in error for s in ("digest", "checksum", "unknown artifact", "missing artifact")):
                                reason = "stale_or_missing_reference"
                            else:
                                reason = "other"
                            entry["artifact_read_failures"][reason] += 1
    return by_thread


def inspect_workspace(state, native, on_disk, response_rows, raw):
    from gx_core.p0.response_attribution import report
    file = state / "gx.db"
    by_run = []
    with closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        c.execute("BEGIN")
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok" and not c.execute("PRAGMA foreign_key_check").fetchall()
        tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        scientific_receipts, issues = [], []
        verify_science(c, state, scientific_receipts, issues)
        for value in scientific_receipts:
            raw.append({"workspace": state.name, **value})
        if issues:
            raise ValueError("Frozen numerical input or Artifact integrity failed: " + str(issues))
        funds = c.execute("SELECT b.spent_json,b.reserved_json FROM p0_workspace w JOIN budgets b USING(budget_ref)").fetchone()
        ledger, reserved = json.loads(funds[0]), json.loads(funds[1])
        artifacts = {v["artifact_ref"]: v for v in rows(c, "SELECT * FROM artifacts WHERE published=1")}
        runs = rows(c, "SELECT r.*,w.run_key FROM runs r JOIN p0_workspace_runs w USING(run_id) ORDER BY w.run_key")
        ws_tokens = 0
        for run in runs:
            rid, key = run["run_id"], run["run_key"]
            attr = report(c, rid)
            native_tokens = native_responses = compactions = matched = 0
            official_rows = []
            read_failures = Counter()
            counts = Counter()
            roles = rows(c, "SELECT * FROM p0_roots WHERE run_id=? AND task_id IS NOT NULL", (rid,))
            root_by_thread = {r["thread_id"]: r for r in roles}
            children = rows(c, "SELECT child_id,root_id,owner_turn,metadata_verified FROM p0_native_child_meters WHERE run_id=?", (rid,))
            included = set(root_by_thread) | {r["child_id"] for r in children}
            if not included.issubset(native):
                issues.append("official rollout missing for a metered scientific thread")
            official_keys = set()
            started = []
            ended = []
            for tid in sorted(included & native.keys()):
                receipt = native[tid]
                compactions += receipt["compactions"]
                read_failures.update(receipt["artifact_read_failures"])
                for (turn, response), record in receipt["responses"].items():
                    official_keys.add((tid, turn, response))
                    native_tokens += record["usage"]["total_tokens"]
                    native_responses += 1
                for turn, observed in receipt["turns"].items():
                    if observed.get("started"):
                        started.append(observed["started"])
                    if observed.get("ended"):
                        ended.append(observed["ended"])
                    official_rows.append(dict(thread_id=tid, turn_id=turn, **observed))
                raw.append({"kind": "official_session", "workspace": state.name, "run_id": rid,
                            "thread_id": tid, "sha256": receipt["rollout_sha256"], "turns": receipt["turns"],
                            "role": root_by_thread[tid]["role"] if tid in root_by_thread else "child", **{
                                k: receipt[k] for k in ("codex_base_instructions_sha256", "initial_dynamic_tools_sha256",
                                "initial_dynamic_tools", "multi_agent_version", "gx_developer_prefix_sha256", "brief_messages_by_role",
                                "persisted_additional_tools", "persisted_request_top_level_tools")}})
            atr_keys = set()
            for row in attr["responses"]:
                atr_keys.add((row["thread_id"], row["turn_id"], row["response_id"]))
                receipt = native.get(row["thread_id"], {}).get("responses", {}).get((row["turn_id"], row["response_id"]))
                if receipt and row["total_tokens"] == receipt["usage"]["total_tokens"] and not row["conflicting"]:
                    matched += 1
                response_rows.append({"run_key": key, **row})
            bills = rows(c, "SELECT thread_id,turn_id,token_budget,budget_status,terminal_status,confidence,"
                            "settled_tokens,observed_lower_bound,attribution FROM p0_meter_native_turns WHERE run_id=?", (rid,))
            settled = sum(b["settled_tokens"] or 0 for b in bills)
            ws_tokens += settled
            tasks = rows(c, "SELECT task_id,role,attempt,attempts_consumed,brief_sha256,brief_ref FROM tasks WHERE run_id=?", (rid,))
            subs = rows(c, "SELECT s.task_id,t.role,s.attempt,s.submission_id,s.artifact_refs_json,s.result,"
                           "s.created_at,s.lease_id FROM submissions s JOIN tasks t USING(task_id) WHERE t.run_id=?"
                           " ORDER BY s.created_at,s.rowid", (rid,))
            repairs = (rows(c, "SELECT r.task_id,r.attempt,r.role,p.call_id,p.recorded_at,p.category FROM "
                        "p0_product_repairs p JOIN p0_roots r USING(thread_id) WHERE r.run_id=? ORDER BY p.recorded_at", (rid,))
                       if "p0_product_repairs" in tables else [])
            terminals = (rows(c, "SELECT t.*,r.task_id,r.attempt FROM p0_root_terminals t JOIN p0_roots r "
                              "USING(thread_id) WHERE r.run_id=?", (rid,)) if "p0_root_terminals" in tables else [])
            firsts = defaultdict(list)
            for s in subs:
                if s["role"] == "evidence_reviewer":
                    firsts[s["task_id"]].append((s["created_at"], s["result"]))
            for p in repairs:
                if p["role"] == "evidence_reviewer":
                    firsts[p["task_id"]].append((p["recorded_at"], "SCHEMA_INVALID"))
            first_results = {task: min(trials)[1] for task, trials in firsts.items()}
            accepted = [s for s in subs if s["result"] == "ACCEPTED"]
            published = Counter(ref for s in accepted for ref in json.loads(s["artifact_refs_json"]))
            dupes = sum(n - 1 for n in published.values()) + sum(
                n - 1 for n in Counter((s["task_id"], s["attempt"]) for s in accepted).values())
            all_submitted = {ref for s in subs for ref in json.loads(s["artifact_refs_json"])}
            orphan_products = sum(ref not in all_submitted for ref, a in artifacts.items()
                if a["run_id"] == rid and a["origin"] == "HOST_PUT" and a["task_id"]
                and a["kind"] in {"science_assessment", "science_decision"})
            balances = Counter(s["result"] for s in subs)
            jobs = rows(c, "SELECT job_id,idempotency_key,job_attempt,status FROM jobs WHERE run_id=?", (rid,))
            duplicate_jobs = len(jobs) - len({j["idempotency_key"] for j in jobs})
            decisions = rows(c, "SELECT round_index,submission_id,reviewer_submission_id,decision_ref,actions_json "
                              "FROM science_decisions WHERE run_id=?", (rid,))
            accepted_ids = {s["submission_id"] for s in accepted}
            owners = {r["lease_id"]: r["thread_id"] for r in roles}
            accepted_owners = {s["submission_id"]: owners.get(s["lease_id"]) for s in accepted}
            provenance_errors = 0
            for decision in decisions:
                if decision["submission_id"] not in accepted_ids or decision["decision_ref"] not in artifacts:
                    provenance_errors += 1
                if decision["reviewer_submission_id"] and decision["reviewer_submission_id"] not in accepted_ids:
                    provenance_errors += 1
                if decision["reviewer_submission_id"] and accepted_owners.get(decision["reviewer_submission_id"]) == accepted_owners.get(decision["submission_id"]):
                    provenance_errors += 1
            settlements = rows(c, "SELECT path_id,kind,direction,eval_id,artifact_ref,manifest_hash FROM "
                                  "science_settlements WHERE run_id=?", (rid,))
            for s in settlements:
                if s["kind"] == "valid_scientific_outcome" and (s["artifact_ref"] not in artifacts or not s["manifest_hash"]
                      or not c.execute("SELECT 1 FROM evaluations WHERE run_id=? AND eval_id=?", (rid, s["eval_id"])).fetchone()):
                    provenance_errors += 1
            emb_errors = 0
            for root in roles:
                brief = json.loads(root["brief"].split("\n", 1)[1])
                preload = brief.get("preloaded_artifacts", {})
                counts["preloaded_items"] += len(preload)
                counts["task_briefs"] += 1
                allowed = {row[0] for row in c.execute(
                    "SELECT artifact_ref FROM p0_root_artifact_reads WHERE root_key=?", (root["root_key"],))}
                total = 0
                for ref, embedded in preload.items():
                    indexed = artifacts.get(ref)
                    if ref not in allowed:
                        emb_errors += 1
                    if indexed is None:
                        emb_errors += 1
                        continue
                    data = read_artifact(state, indexed)
                    total += len(data)
                    if (embedded.get("sha256") != hashlib.sha256(data).hexdigest() or
                        embedded.get("content") != json.loads(data) or len(data) > 16384):
                        emb_errors += 1
                packed_size = len(json.dumps(preload, sort_keys=True, ensure_ascii=False,
                                             separators=(",", ":")).encode())
                if total > 16384 or packed_size > 16384:
                    emb_errors += 1
            for task in tasks:
                stored = artifacts.get(task["brief_ref"])
                if stored is None or stored["sha256"] != task["brief_sha256"]:
                    emb_errors += 1
            modes = Counter()
            reports, accepted_model_fields = [], 0
            scientific_errors = []
            for a in artifacts.values():
                if a["run_id"] != rid:
                    continue
                if a["kind"] == "science_assessment" and (a["artifact_ref"] in published or a["origin"] == "BACKEND"):
                    product = json.loads(read_artifact(state, a))
                    modes[product.get("assessment_mode", "legacy_model_review")] += 1
                    proposal = json.loads(read_artifact(state, artifacts[product["proposal_ref"]]))
                    expected = {p["id"]: p["expected_reaches_threshold"] for p in proposal["paths"]}
                    findings = product.get("findings", [])
                    if product.get("assessment_mode") == "independent_review":
                        receipt = next((s for s in accepted if a["artifact_ref"] in json.loads(s["artifact_refs_json"])), None)
                        if receipt:
                            terminal = next((t for t in terminals if t["task_id"] == receipt["task_id"] and
                                             t["attempt"] == receipt["attempt"] and t["reason"] == "GX_ACCEPTED"), None)
                            authored = native.get(terminal["thread_id"], {}).get("model_findings", {}).get(terminal["call_id"]) if terminal else None
                            canonical = {f["path_id"]: {k: digest_json(f[k]) for k in ("reason", "risk")} for f in findings}
                            if authored == canonical:
                                accepted_model_fields += sum(len(f) for f in canonical.values())
                            else:
                                scientific_errors.append("accepted review judgment fields lack matching direct model input")
                    if sorted(expected) != sorted(f["path_id"] for f in findings):
                        scientific_errors.append("per-path coverage")
                    for finding in findings:
                        path = finding["path_id"]
                        evidence = json.loads(read_artifact(state, artifacts[finding["evidence_ref"]]))
                        route = next(route for route in evidence["routes"] if route["path_id"] == path)
                        evaluation = json.loads(read_artifact(state, artifacts[route["evaluation_ref"]]))
                        reaches = evaluation["count_at_or_above"] > 0
                        if (finding["evaluation_ref"] != route["evaluation_ref"] or finding["observations_ref"] != route["observations_ref"]
                                or finding["supported"] is not (reaches == expected[path])):
                            scientific_errors.append("signed numerical finding " + path)
                        if run["workflow_version"] == "3" and (
                                finding["measurement_valid"] is not True or finding["reaches_threshold"] is not reaches):
                            scientific_errors.append("frozen numerical evaluator " + path)
                    reports.append({"ref": a["artifact_ref"], "sha256": a["sha256"],
                                    "assessment_mode": product.get("assessment_mode"),
                                    "findings": [{k: f.get(k) for k in ("path_id", "measurement_valid", "reaches_threshold",
                                        "supported", "evidence_ref", "observations_ref", "evaluation_ref")}
                                        for f in product.get("findings", [])]})
            terminal_by_response = {}
            for record in terminals:
                official = native.get(record["thread_id"], {})
                direct_id = official.get("direct_product_responses", {}).get(record["call_id"])
                gx_id = next((r["response_id"] for r in attr["responses"] if any(
                    t["source"] == "GX" and t["call_id"] == record["call_id"]
                    and (t["namespace"], t["tool"]) in ((None, "gx_submit_product"), ("gx", "gx_submit_product"))
                    for t in r["tool_calls"])), None)
                if direct_id and gx_id and direct_id != gx_id:
                    raise ValueError("Direct GX product call differs between official rollout and observed callback")
                terminal_id = direct_id or gx_id
                ordered = [response for turn, response in official.get("responses", {}) if turn == record["turn_id"]]
                position = official.get("direct_leaf_positions", {}).get(record["call_id"])
                same_turn_after = None
                if terminal_id in ordered:
                    same_turn_after = len(ordered) - ordered.index(terminal_id) - 1
                elif position is not None and position == len(ordered):
                    # The terminal function item has no completing Response of its own,
                    # and no further provider completion was recorded after that item.
                    same_turn_after = 0
                terminal_by_response[record["call_id"]] = {"response_id": terminal_id,
                    "official_direct_gx_product_response": direct_id, "gx_linked_response": gx_id,
                    "direct_leaf_observed": position is not None,
                    "official_tool_output": official.get("direct_product_output", {}).get(record["call_id"]),
                    "later_official_completed_responses_in_turn": same_turn_after,
                    "official_turn": official.get("turns", {}).get(record["turn_id"])}
            ksum = Counter(row["request_class"] for row in attr["responses"])
            missing_final_receipts = sum(t["direct_leaf_observed"] and t["response_id"] is None for t in terminal_by_response.values())
            raw.append({"kind": "run", "workspace": state.name, "run_key": key, "run_id": rid,
                        "workflow": [run["workflow_id"], run["workflow_version"], run["workflow_sha256"]],
                        "bills": bills, "children": children, "repairs": repairs, "terminal": terminals,
                        "reconciled_terminal_receipts": terminal_by_response, "submissions": subs,
                        "jobs": jobs, "decisions": decisions, "scientific_settlements": settlements,
                        "accepted_assessments": reports, "first_review_outcome": first_results,
                        "native_turns": official_rows, "unlinked_tool_intervals": attr["unlinked_tools"],
                        "first_request_input_by_turn": attr["first_request_input_tokens"]})
            by_run.append({
                "run_key": key, "run_id": rid, "status": run["status"], "run_wall_seconds": wall(run["created_at"], run["updated_at"]),
                "first_to_last_native_seconds": wall(min(started), max(ended)) if started and ended else None,
                "tokens": settled, "provider_rollout_tokens": native_tokens, "official_completed_responses": native_responses,
                "terminal_model_outputs_without_response_completion": missing_final_receipts,
                "model_invocations_evidenced_lower_bound": native_responses + missing_final_receipts,
                "attribution_requests": len(atr_keys), "duplicate_product_receipts_or_refs": dupes,
                "unreferenced_host_products": orphan_products, "duplicate_jobs": duplicate_jobs, "jobs": len(jobs),
                "jobs_succeeded": sum(j["status"] == "SUCCEEDED" for j in jobs),
                "observations_recomputed": sum(v["data"]["coordinates"] for v in scientific_receipts if
                    v["kind"] == "recomputed_numerical_result" and v["data"]["run_id"] == rid),
                "accepted_products": len(accepted), "published_rejected_submissions": balances["REJECTED"],
                "distinct_structural_failures": len(repairs), "first_reviews": len(first_results),
                "first_reviews_rejected": sum(v != "ACCEPTED" for v in first_results.values()),
                "deterministic_assessments": modes["deterministic"], "decisions": len(decisions),
                "matched_model_authored_reviewer_reason_risk_fields": accepted_model_fields if run["workflow_version"] == "3" else None,
                "independent_review_decisions": sum(bool(d["reviewer_submission_id"]) for d in decisions),
                "provenance_issues": provenance_errors, "signed_scientific_finding_errors": len(scientific_errors),
                "brief_content_or_digest_issues": emb_errors,
                "measured_scientific_settlements": sum(s["kind"] == "valid_scientific_outcome" for s in settlements),
                "settlements_by_direction": dict(Counter(s["direction"] for s in settlements if s["direction"])),
                "estimate_settlements": sum(b["confidence"] == "ESTIMATED" and b["budget_status"] == "SETTLED" for b in bills),
                "unsettled_turns": sum(b["budget_status"] != "SETTLED" for b in bills),
                "native_turns": len(bills), "registered_child_threads": len(children), "compactions": compactions,
                "first_request_input_tokens": [r["input_tokens"] for r in attr["first_request_input_tokens"]],
                "mechanical_request_classes": dict(ksum), "unlinked_tools": len(attr["unlinked_tools"]),
                "matched_provider_usage_records": matched, "provider_vs_attributed_response_ids_equal": atr_keys == official_keys,
                "charged_vs_provider_tokens_equal": settled == native_tokens, "terminal_commits": len(terminals),
                "terminal_interrupt_acks": sum(bool(t["interrupt_ack_at"]) for t in terminals),
                "official_terminal_no_response_cancellations": sum(t["official_tool_output"] == "codex_canceled_before_receiving_response"
                                                                 for t in terminal_by_response.values()),
                "official_other_terminal_tool_outputs": sum(t["official_tool_output"] == "other_tool_output"
                                                           for t in terminal_by_response.values()),
                "post_terminal_completed_same_turn": sum(
                    t["later_official_completed_responses_in_turn"] or 0 for t in terminal_by_response.values()) if terminals else None,
                "unresolved_terminal_response_links": sum(t["response_id"] is None
                                                         for t in terminal_by_response.values()),
                "submitted_terminal_followon_class": ksum["POST_SUBMIT"], "preloaded_items": counts["preloaded_items"],
                "stale_or_missing_artifact_read_failures": read_failures["stale_or_missing_reference"],
                "unclassified_failed_reads": read_failures["other"],
                "read_artifact_failures": dict(read_failures)})
        on_disk.append({"workspace": state.name, "workspace_spent_tokens": ledger.get("tokens", 0),
                        "workspace_reserved_tokens": reserved.get("tokens", 0),
                        "turns_vs_workspace_tokens_equal": ws_tokens == ledger.get("tokens", 0),
                        "science_and_store_issues": issues, "db_sha256": sha(file)})
    return by_run


def audit(work, out):
    sys.path.insert(0, str(work / "install/gx-h"))
    from gx_core.p0.response_attribution import summarize
    sessions, raw, attributions, states, runs = native_usage(work), [], [], [], []
    for file in sorted(work.glob("state-*/gx.db")):
        runs.extend(inspect_workspace(file.parent, sessions, states, attributions, raw))
    fields = ("tokens", "provider_rollout_tokens", "official_completed_responses", "attribution_requests",
              "terminal_model_outputs_without_response_completion", "model_invocations_evidenced_lower_bound",
              "duplicate_product_receipts_or_refs", "unreferenced_host_products", "duplicate_jobs", "jobs", "jobs_succeeded",
              "observations_recomputed", "accepted_products", "published_rejected_submissions", "distinct_structural_failures",
              "first_reviews", "first_reviews_rejected", "deterministic_assessments", "decisions", "independent_review_decisions",
              "matched_model_authored_reviewer_reason_risk_fields",
              "provenance_issues", "signed_scientific_finding_errors", "brief_content_or_digest_issues", "measured_scientific_settlements", "estimate_settlements",
              "unsettled_turns", "native_turns", "registered_child_threads", "compactions", "unlinked_tools",
              "matched_provider_usage_records", "terminal_commits", "terminal_interrupt_acks", "post_terminal_completed_same_turn",
              "official_terminal_no_response_cancellations", "official_other_terminal_tool_outputs",
              "unresolved_terminal_response_links", "submitted_terminal_followon_class", "preloaded_items",
              "stale_or_missing_artifact_read_failures", "unclassified_failed_reads")
    totals = {k: sum(row[k] for row in runs if row[k] is not None) if any(row[k] is not None for row in runs) else None for k in fields}
    callbacks = defaultdict(list)
    with (work / "metrics.jsonl").open() as stream:
        for line in stream:
            row = json.loads(line)
            if row["kind"] == "p0_callback":
                callbacks[row["tool"]].append(row["duration_ms"])
                raw.append({"kind": "gx_callback", **{k: row.get(k) for k in
                    ("run_id", "thread_id", "turn_id", "tool", "duration_ms", "observed_ns")}})
    complete = (out / "finished.json").is_file()
    intervals = [json.loads(line) for line in (out / "waves.jsonl").read_text().splitlines()] if (out / "waves.jsonl").is_file() else []
    write_rows(out / "attribution.jsonl.gz", attributions)
    write_rows(out / "receipts.jsonl.gz", raw)
    save(out / "measured.json", {
        "arm": out.name, "source": json.loads((out / "environment.json").read_bytes()),
        "finished": complete, "native_accounting": "official approval-free ChatGPT OAuth, GX cumulative ledger for charges",
        "scope_of_request_count": "Distinct completed Responses plus a separate lower bound for additional invocations evidenced by an official direct model function item lacking a completion. Other provider-side starts without recorded model output remain unobserved.",
        "catalog_evidence": "Official session_meta records registered GX dynamic tools. Persisted response_item.additional_tools carries the model-facing catalog (corresponds to request.input[].tools). A top-level request.tools schema is reported only when the complete request was persisted; an empty receipt list indicates unobserved.",
        "first_review_denominator": "Distinct reviewer Tasks with a structural refusal or published submission. Deterministic assessments are separate.",
        "duplicate_definition": "Excess accepted receipts per Task/Attempt and repeated Artifact refs across accepted receipts. Unreferenced HOST_PUT scientific products counted separately.",
        "completed_runs": sum(r["status"] == "COMPLETED" for r in runs), "totals": totals, "per_run": runs,
        "workspaces": states, "request_classes": summarize(attributions),
        "first_request_input_tokens": quantile([v for r in runs for v in r["first_request_input_tokens"]]),
        "run_wall_seconds": quantile([r["run_wall_seconds"] for r in runs]),
        "native_active_span_per_run_seconds": quantile([r["first_to_last_native_seconds"] for r in runs]),
        "driver_wall_seconds": quantile([v["driver_wall_seconds"] for v in intervals]),
        "campaign_wall_seconds": json.loads((out / "finished.json").read_bytes())["total_wall_seconds"] if complete else None,
        "callback_duration_ms": {name: quantile(values) for name, values in sorted(callbacks.items())},
        "all_observed_provider_response_ids_retained": all(r["provider_vs_attributed_response_ids_equal"] for r in runs),
        "all_provider_responses_linked": all(r["provider_vs_attributed_response_ids_equal"] for r in runs),
        "charged_vs_provider_tokens_equal": all(r["charged_vs_provider_tokens_equal"] for r in runs),
        "all_workspace_ledgers_reconciled": all(s["turns_vs_workspace_tokens_equal"] and not s["workspace_reserved_tokens"] for s in states),
        "raw_files": {n: sha(out / n) for n in ("attribution.jsonl.gz", "receipts.jsonl.gz")}})


def checkpoint(work, out, wave):
    sys.path.insert(0, str(work / "install/gx-h"))
    from gx_core.p0.response_attribution import summarize
    state = work / f"state-{wave:02d}"
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as conn:
        if {r[0] for r in conn.execute("SELECT status FROM runs")} != {"COMPLETED"}:
            raise ValueError("Checkpoint requires a completed wave")
        threads = {r[0] for r in conn.execute("SELECT thread_id FROM p0_roots WHERE thread_id IS NOT NULL "
                                             "UNION SELECT child_id FROM p0_native_child_meters")}
    states, attributions, raw = [], [], []
    runs = inspect_workspace(state, native_usage(work, threads), states, attributions, raw)
    filename = "first-wave.json" if wave == 0 else f"checkpoint-{wave:02d}.json"
    save(out / filename, {"completed": len(runs), "per_run": runs, "classes": summarize(attributions), "workspace": states,
         "response_attribution": attributions, "receipts": raw,
         "direct_terminal_receipts": [r["reconciled_terminal_receipts"] for r in raw if r["kind"] == "run"],
         "model_catalog": [{"role": r["role"], "additional_tools": r["persisted_additional_tools"]}
                          for r in raw if r["kind"] == "official_session"]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--checkpoint", choices=range(13), type=int)
    args = parser.parse_args()
    if args.checkpoint is None:
        audit(args.private.resolve(), args.evidence.resolve())
    else:
        checkpoint(args.private.resolve(), args.evidence.resolve(), args.checkpoint)
