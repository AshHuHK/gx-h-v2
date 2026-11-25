"""Reconstruct delivered briefs, committed choices and Job effects from original GX and Codex records."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT / "gx-h"), str(ROOT / "acceptance/v6")]
from gx_core.p0 import manifest, response_attribution, v6_costs, v62_outputs
from native_witness import rollout, texts
from verify import digest, need, parse, sha


def read(path):
    return parse(path.read_bytes())


def database(work):
    db = sqlite3.connect((work / "state/gx.db").resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")
    return db


def rows(db, sql, args):
    return [dict(r) for r in db.execute(sql, args)]


def native_session(directory):
    sessions = {}
    for file in directory.rglob("*.jsonl"):
        with file.open("rb") as stream:
            line = stream.readline()
        if line:
            record = parse(line)
            if record.get("type") == "session_meta":
                thread = record["payload"]["id"]
                need(thread not in sessions, "Multiple native transcripts for one thread")
                sessions[thread] = file
    return sessions


def delivered(path, turn_id, brief):
    """A timeout can have an acknowledged, delivered brief without any accepted tool call."""
    session = context = active = stamp = None
    calls, messages = {}, []
    with path.open("rb") as stream:
        for number, line in enumerate(stream, 1):
            event = parse(line)
            kind, obj = event["type"], event["payload"]
            if kind == "session_meta":
                session = {k: obj.get(k) for k in ("id", "cli_version", "model_provider")}
            if kind == "turn_context" or (kind == "event_msg" and obj.get("type") == "task_started"):
                active = obj.get("turn_id")
                if kind == "turn_context" and active == turn_id:
                    context = {k: obj.get(k) for k in ("model", "effort", "turn_id")}
            if (active == turn_id and kind == "response_item" and obj.get("role") == "user"
                    and brief in texts(obj.get("content", [])) and stamp is None):
                stamp = event["timestamp"]
            if active != turn_id or not stamp:
                continue
            if kind == "response_item" and obj.get("role") == "assistant":
                messages.extend(texts(obj.get("content", [])))
            if kind == "response_item" and obj.get("type") == "function_call":
                calls[obj["call_id"]] = (obj["name"].split(".")[-1], parse(obj["arguments"]), number)
            if kind == "event_msg" and obj.get("type") == "item_completed" and obj.get("turn_id") == turn_id:
                item = obj.get("item", {})
                if item.get("type") == "DynamicToolCall" and item.get("success") is True:
                    calls.setdefault(item["id"], (item["tool"], item["arguments"], number))
    need(session and context and stamp, "Official transcript does not establish delivery of this brief")
    return session, context, stamp, calls, messages


def artifact(db, work, rid, ref):
    row = db.execute("SELECT namespace,rel_path,sha256,job_id FROM artifacts WHERE run_id=? AND artifact_ref=?",
                     (rid, ref)).fetchone()
    need(row, "Missing run-scoped artifact")
    base = (work / "state/artifacts").resolve(strict=True)
    file = (base / row["namespace"] / row["rel_path"]).resolve(strict=True)
    need(file.is_relative_to(base), "Artifact path outside GX state")
    raw = file.read_bytes()
    need(sha(raw) == row["sha256"], "Artifact bytes differ from the GX receipt")
    return row, raw


def publication(db, work, rid, projected, cursor, jobs):
    ref = projected["report_ref"]
    row = db.execute("SELECT receipt_json FROM v62_module_reports WHERE run_id=? AND report_ref=?", (rid, ref)).fetchone()
    need(row, "Projected report has no GX publication")
    receipt = parse(row[0])
    event = db.execute("SELECT ts,kind,subject_id FROM events WHERE event_id=? AND run_id=?",
                       (receipt["event_id"], rid)).fetchone()
    need(event and event["kind"] == "v62.module.reported" and event["subject_id"] == projected["module_instance_id"]
         and receipt["event_id"] <= cursor, "Report was unavailable at brief freeze")
    release = db.execute("SELECT min(w.event_id) FROM v62_module_wakes w JOIN events e USING(event_id) "
                         "WHERE w.run_id=? AND w.event_id<=? AND EXISTS (SELECT 1 FROM "
                         "json_each(e.payload_json,'$.report_refs') WHERE value=?)", (rid, cursor, ref)).fetchone()[0]
    need(release is not None, "Report was not released to this Lead at brief freeze")
    meta, raw = artifact(db, work, rid, ref)
    body = parse(raw)
    need(meta["sha256"] == receipt["report_sha256"] and body["run_id"] == rid
         and body["module_instance_id"] == projected["module_instance_id"], "Report content mismatch")
    opened = db.execute("SELECT m.scope,m.branch_id,m.focus,m.parent_task_id,m.parent_context_ref,"
                        "m.receipt_json,e.event_id FROM v62_modules m JOIN events e ON e.run_id=m.run_id "
                        "AND e.subject_id=m.module_instance_id AND e.kind='v62.module.opened' "
                        "WHERE m.run_id=? AND m.module_instance_id=?", (rid, body["module_instance_id"])).fetchone()
    need(opened, "Report lacks its original module delegation")
    parent = parse(opened["receipt_json"])
    sources = {}
    for item in body["sources"]:
        origin, _ = artifact(db, work, rid, item["artifact_ref"])
        need(origin["sha256"] == item["sha256"] and item["receipts"], "Report source lacks a matching receipt")
        traces = []
        for proof in item["receipts"]:
            if "job_id" in proof:
                traces.extend(dict(kind="job", event_id=n["commission_event"], settled_event=n["event_id"],
                                   capability=n["spec"]["capability"], parameters=n["spec"]["parameters"],
                                   input_sha256=n["input_sha256"], job_id=n["job_id"], output=out) for n in jobs
                              if n["branch_id"] == opened["branch_id"] and n["job_id"] == proof["job_id"]
                              and n["status"] == "SUCCEEDED" for out in n["outputs"]
                              if out["artifact_ref"] == item["artifact_ref"] and out["sha256"] == proof["output_sha256"] == item["sha256"])
            elif "operation_key" in proof:
                for op in db.execute("SELECT event_id,payload_json FROM events WHERE run_id=? AND subject_id=? "
                                    "AND kind='v62.module.tool_completed' AND json_extract(payload_json,'$.operation_key')=?",
                                    (rid, body["module_instance_id"], proof["operation_key"])):
                    details = parse(op["payload_json"])
                    if (not origin["job_id"] and details["result_sha256"] == proof["result_sha256"]
                            and item["artifact_ref"] in details["source_refs"]):
                        traces.append(dict(kind="source_read", event_id=op["event_id"], capability=details["tool"],
                                           parameters=details.get("arguments", {}), input_sha256=[item["sha256"]],
                                           output={"artifact_ref":item["artifact_ref"], "sha256":item["sha256"]}))
        sources[item["artifact_ref"]] = {**item, "scoped_operations": traces}
    for finding in projected.get("findings", []):
        need(any(f["text"].startswith(finding["text"]) and
                 set(finding["source_refs"]) <= set(f["source_refs"]) <= sources.keys()
                 for f in body["findings"]), "Delivered finding differs from its published source")
    return dict(ref=ref, sha256=meta["sha256"], published_event=receipt["event_id"], published_at=event["ts"],
                released_event=release, delivered=projected, sources=list(sources.values()),
                author_kind=body["author"]["kind"], module_open_event=opened["event_id"],
                parent_window=[parent["thread_id"], parent["turn_id"]], parent_call=parent["call_id"], focus=opened["focus"],
                original_findings=body["findings"])


def job_inventory(db, work, rid):
    jobs = []
    for row in rows(db, "SELECT n.node_id,n.branch_id,n.decision_ref,n.spec_json,n.job_id,n.status,n.result_ref,"
                    "e.event_id,e.ts FROM v6_nodes n LEFT JOIN events e ON e.run_id=n.run_id AND "
                    "e.subject_id=n.node_id AND e.kind='v6.node.settled' WHERE n.run_id=? ORDER BY n.rowid", (rid,)):
        row["spec"] = parse(row.pop("spec_json"))
        commission = db.execute("SELECT receipt_json FROM v6_decisions WHERE run_id=? AND decision_ref=?",
                                (rid, row["decision_ref"])).fetchone()
        row["commission_event"] = parse(commission[0])["event_id"] if commission else None
        if row["job_id"]:
            proof = manifest.read_manifest(db, row["job_id"])
            need(proof["admission"]["run_id"] == rid, "Job belongs to another Run")
            row["admission_sha256"] = proof["admission_hash"]
            row["completion_sha256"] = proof["manifest_hash"]
            row["input_sha256"] = [v["sha256"] for v in proof["admission"]["inputs"]]
            row["outputs"] = v62_outputs.published_outputs(db, rid, row["job_id"])
            for output in row["outputs"]:
                content, _ = artifact(db, work, rid, output["artifact_ref"])
                need(content["sha256"] == output["sha256"], "Output bytes differ from the declared completion")
                output["bytes_path"] = str(Path("state/artifacts") / content["namespace"] / content["rel_path"])
            if row["status"] in ("SUCCEEDED", "REUSED"):
                content, _ = artifact(db, work, rid, row["result_ref"])
                need(proof["completion"]["status"] == "SUCCEEDED" and any(
                    o["artifact_ref"] == row["result_ref"] and o["sha256"] == content["sha256"]
                    for o in proof["completion"]["outputs"]), "Result has no completed Job manifest")
                row["result_sha256"] = content["sha256"]
        jobs.append(row)
    return jobs


def commissions(db, rid, parent, calls, jobs):
    openings = rows(db, "SELECT m.*,e.event_id FROM v62_modules m JOIN events e ON e.run_id=m.run_id AND "
                    "e.subject_id=m.module_instance_id AND e.kind='v62.module.opened' "
                    "WHERE m.run_id=? AND m.parent_task_id=?", (rid, parent["task_id"]))
    verified = []
    for opened in openings:
        receipt = parse(opened["receipt_json"])
        if (receipt["thread_id"], receipt["turn_id"]) != (parent["thread_id"], parent["turn_id"]):
            continue
        call_id = receipt["call_id"]
        tool, args, line = calls[call_id]
        callback = db.execute("SELECT request_sha,result_json FROM p0_root_calls WHERE thread_id=? AND turn_id=? "
                              "AND call_id=?", (parent["thread_id"], parent["turn_id"], call_id)).fetchone()
        need(tool.endswith("gx_open_module") and digest(args) == opened["request_sha256"] and
             callback and callback["request_sha"] == digest(["gx_open_module", args]) and
             parse(callback["result_json"]) == receipt and receipt["parent_context_ref"] == parent["context_ref"],
             "Module creation differs from the accepted native Lead call")
        real = [j["node_id"] for j in jobs if j["branch_id"] == opened["branch_id"] and j["status"] == "SUCCEEDED"
                and j.get("result_sha256") and j["commission_event"] > opened["event_id"] and j["event_id"] > j["commission_event"]]
        verified.append(dict(module_instance_id=opened["module_instance_id"], call_id=call_id, event_id=opened["event_id"],
                             call_line=line, focus=opened["focus"], branch_id=opened["branch_id"], executed_nodes=real))
    return verified


def signature(spec):
    return digest([spec.get(k) for k in ("capability", "parameters", "inputs", "depends_on")])


def decision_effect(db, rid, decision, jobs):
    accepted = parse(decision["receipt_json"])
    intent = parse(decision["request_json"])["decision"]
    base, head = accepted["base"], accepted["result_head"]
    before = {}
    for pin in (base, head):
        if pin:
            row = db.execute("SELECT * FROM v6_graph_versions WHERE run_id=? AND version=?", (rid, pin["version"])).fetchone()
            need(row and digest(parse(row["graph_json"])) == row["sha256"] == pin["sha256"], "Unverified graph version")
            if pin == base:
                before = parse(row["graph_json"])
            if pin == head and head != base:
                need(row["decision_ref"] == decision["decision_ref"] and row["parent_sha"] == (base or {}).get("sha256"),
                     "Changed graph does not descend from this decision's base")
    ops = [item["spec"] for item in accepted["resolved_ops"]]
    planned = {signature(n) for n in before.get("nodes", {}).values() if n.get("state") == "ACTIVE"}
    new, withdrawn, engine = [], [], []
    for node in jobs:
        if node["event_id"] is None or node["event_id"] <= accepted["event_id"]:
            continue
        if any(op["op"] == "add_node" and op["node"] == node["node_id"] for op in ops):
            if (node["decision_ref"] == decision["decision_ref"] and node["status"] == "SUCCEEDED"
                    and node.get("result_sha256") and signature(node["spec"]) not in planned):
                new.append(node["node_id"])
        elif node["node_id"] in before.get("nodes", {}):
            if node["status"] == "CANCELLED" and any(
                (op["op"] == "cancel_node" and op["node"] == node["node_id"])
                or (op["op"] == "stop_branch" and op["branch"] == node["branch_id"]) for op in ops):
                withdrawn.append(node["node_id"])
            elif node["status"] == "SUCCEEDED":
                engine.append(node["node_id"])
    return dict(decision_ref=decision["decision_ref"], call_id=decision["call_id"], event_id=accepted["event_id"],
                base=base, head=head, rationale=intent["rationale"], cited=intent["evidence_refs"], ops=ops,
                claims=accepted["claim_revisions"], new_jobs=new, withdrawn=withdrawn, preplanned_jobs=engine)


def cost_detail(db, rid, scopes):
    data = response_attribution.report(db, rid)["responses"]
    results = {}
    for name in ("lead", "delegated", "unattributed"):
        def owner(row):
            if row["root_id"] not in scopes or not row["ownership_verified"]:
                return "unattributed"
            return "lead" if row["root_id"] == row["thread_id"] and scopes[row["root_id"]] == "run" else "delegated"
        group = [r for r in data if owner(r) == name]
        fields = {}
        for field in ("input_tokens", "output_tokens", "cached_input_tokens"):
            values = [r[field] for r in group if not r["conflicting"] and r[field] is not None]
            fields[field] = {"observed_sum": sum(values) if values else None, "measured_responses": len(values),
                             "recorded_responses": len(group)}
        results[name] = fields
    return {"ledger": v6_costs.resource_report(db, rid), "response_tokens": results}


def extract(work, run_id, sessions=None):
    work = Path(work).resolve()
    if sessions is None:
        sessions = next((p for p in (work / "home/codex/sessions", work / "codex/sessions") if p.is_dir()),
                        work / "home/codex/sessions")
    paths = native_session(sessions)
    with closing(database(work)) as db:
        source = {table: rows(db, "SELECT * FROM " + table + " WHERE run_id=?", (run_id,))
                  for table in ("p0_roots", "p0_root_deliveries")}
        source["p0_root_calls"] = rows(db, "SELECT c.* FROM p0_root_calls c JOIN p0_roots r USING(thread_id) "
                                      "WHERE r.run_id=?", (run_id,))
        contexts = {r["task_id"]: r for r in rows(db, "SELECT t.*,c.event_cursor FROM v6_decision_tasks t "
                    "JOIN research_contexts c USING(context_ref,run_id) WHERE t.run_id=?", (run_id,))}
        decisions = {(d["thread_id"], d["turn_id"]): d for d in
                     rows(db, "SELECT * FROM v6_decisions WHERE run_id=?", (run_id,))}
        roots = {r["thread_id"]: r for r in source["p0_roots"] if r["thread_id"]}
        scopes = {tid: contexts[r["task_id"]]["scope"] for tid, r in roots.items() if r["task_id"] in contexts}
        jobs = job_inventory(db, work, run_id)
        turns, missing = [], []
        for delivery in source["p0_root_deliveries"]:
            tid, turn = delivery["thread_id"], delivery["turn_id"]
            root = roots.get(tid)
            if not root or tid not in scopes or sha(root["brief"].encode()) != delivery["text_sha256"]:
                continue
            if delivery["state"] != "acknowledged" or tid not in paths:
                missing.append(dict(thread_id=tid, turn_id=turn, status="UNCONFIRMED_DELIVERY"))
                continue
            session, context, timestamp, calls, messages = delivered(paths[tid], turn, root["brief"])
            need(session["id"] == tid, "Native transcript thread mismatch")
            task = contexts[root["task_id"]]
            brief = parse(root["brief"].split("\n", 1)[1])
            host = parse(root["effective_profile_json"])
            item = dict(thread_id=tid, turn_id=turn, task_id=task["task_id"], context_ref=task["context_ref"],
                        scope=scopes[tid], frozen_brief=root["brief"], brief_sha256=delivery["text_sha256"],
                        brief_utf8_bytes=len(root["brief"].encode()), delivered_at=timestamp,
                        rollout_sha256=sha(paths[tid].read_bytes()), rollout_path=str(paths[tid].relative_to(sessions)),
                        native={**session, **context}, host_profile={k:host.get(k) for k in ("provider", "model", "effort")},
                        reports=[], decision=None, native_messages=messages)
            if item["scope"] == "run":
                item["reports"] = [publication(db, work, run_id, r, task["event_cursor"], jobs) for r in brief.get("module_reports", [])]
                item["commissions"] = commissions(db, run_id, item, calls, jobs)
                if (tid, turn) in decisions:
                    d = decisions[tid, turn]
                    try:
                        line = rollout(paths[tid], d, source)["call_line"]
                    except ValueError:
                        # Code-mode nested dynamic calls are attested by a completed native tool item.
                        name, args, line = calls[d["call_id"]]
                        req = parse(d["request_json"])
                        intent = args["decision"] if isinstance(args["decision"], dict) else parse(args["decision"])
                        callback, = [c for c in source["p0_root_calls"] if (c["thread_id"], c["turn_id"], c["call_id"]) ==
                                     (tid, turn, d["call_id"])]
                        need(name.endswith("gx_decide_revision") and callback["request_sha"] == digest(["gx_decide_revision", args])
                             and req["decision"] == intent and digest(intent) == req["draft_sha256"] and
                             (d["task_id"], d["context_ref"], d["lease_id"]) == (task["task_id"], task["context_ref"], root["lease_id"]),
                             "Accepted nested call differs from the original host or GX binding")
                    item["decision"] = {**decision_effect(db, run_id, d, jobs), "native_call_line": line,
                                        "commissioned_jobs": [n for m in item["commissions"] for n in m["executed_nodes"]]}
            turns.append(item)
        turns.sort(key=lambda t: t["delivered_at"])
        covered = {(t["thread_id"], t["turn_id"]) for t in turns if t["decision"]}
        missing.extend(dict(thread_id=tid, turn_id=turn, status="UNVERIFIED_NATIVE_DECISION")
                       for tid, turn in decisions if scopes.get(tid) == "run" and (tid, turn) not in covered)
        wakes = rows(db, "SELECT w.round,w.event_id,e.ts,e.payload_json FROM v62_module_wakes w "
                     "JOIN events e USING(run_id,event_id) WHERE w.run_id=? ORDER BY w.event_id", (run_id,))
        state = db.execute("SELECT r.status,g.status AS goal_status,r.created_at FROM runs r JOIN v6_goals g "
                           "USING(run_id) WHERE run_id=?", (run_id,)).fetchone()
        need(state, "This is not a GX Goal run")
        return dict(run_id=run_id, run=dict(state), turns=turns, missing=missing, jobs=jobs, wakes=wakes,
                    costs=cost_detail(db, run_id, scopes))
