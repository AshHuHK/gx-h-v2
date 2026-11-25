"""Read signed GX and official-host facts for the registered coordinate comparison."""
from contextlib import closing
import json
from pathlib import Path

from acceptance.v62b.study import receipts
from acceptance.v62c import materials, witness as released
from acceptance.v62c.scoring import digest, need, validate_assay


def read_outputs(path, turn, calls):
    """Yield tool returns the native conversation received before its accepted decision."""
    active = None
    awaiting = []
    cutoff = (turn.get("decision") or {}).get("native_call_line")
    with path.open("rb") as stream:
        for line_no, line in enumerate(stream, 1):
            entry = json.loads(line)
            obj = entry["payload"]
            kind = entry["type"]
            if kind == "turn_context" or (kind == "event_msg" and obj.get("type") == "task_started"):
                active = obj.get("turn_id")
            if active != turn["turn_id"] or (cutoff and line_no > cutoff):
                continue
            if kind != "response_item":
                continue
            if obj.get("type") == "function_call" or obj.get("role") == "assistant":
                # A returned tool item followed by a real assistant output made it into the
                # model's conversation. A GX callback without a delivered answer is insufficient.
                yield from awaiting
                awaiting.clear()
            if obj.get("type") != "function_call_output" or obj["call_id"] not in calls:
                continue
            name, args, origin = calls[obj["call_id"]]
            if name != "gx_read_artifact" or origin >= line_no:
                continue
            try:
                result = json.loads(obj["output"])
            except (TypeError, ValueError):
                continue
            if isinstance(result, dict):
                awaiting.append((obj["call_id"], args.get("ref"), result, line_no))


def _candidates(db, work, rid, turn, turns, paths, jobs):
    brief = json.loads(turn["frozen_brief"].split("\n", 1)[1])
    candidates = []
    for obs in brief.get("observations", []):
        for m in obs.get("verified_metrics", []):
            if obs["artifact_ref"] == m.get("artifact_ref"):
                candidates.append({"metric":m, "delivery":"frozen_brief"})

    scoped = {}
    reports = list(turn["reports"])
    projected = {r["ref"] for r in reports}
    # Larger Runs can deliver a report solely in the index, with no preview row.
    # The original publication authenticates its scope; only an actual tool
    # result can supply its numbers to this turn.
    for entry in brief.get("module_report_index", []):
        if entry["report_ref"] not in projected:
            index = {key:entry[key] for key in ("report_ref","module_instance_id")}
            reports.append(receipts.publication(db,work,rid,index,turn["cursor"],jobs))
    for report in reports:
        if report["author_kind"] != "MODULE":
            continue
        row = db.execute("SELECT m.scope,r.receipt_json FROM v62_modules m JOIN v62_module_reports r "
                         "USING(module_instance_id,run_id) WHERE r.run_id=? AND r.report_ref=?",
                         (rid, report["ref"])).fetchone()
        need(row, "Delivered module report has no corresponding publication")
        receipt = json.loads(row["receipt_json"])
        author = receipt["author"]
        producer = turns.get((author["thread_id"],author["turn_id"]))
        confirmed = bool(producer and producer["scope"] == row["scope"] and producer["task_id"] == author["task_id"]
                         and released._native_call(db, paths, producer, author["call_id"], "gx_submit_module_report") == receipt)
        evidence = dict(report_ref=report["ref"], scope=row["scope"], native_publication=confirmed,
                        published_event=report["published_event"], released_event=report["released_event"],
                        report_sha256=report["sha256"], report_sources=[s["artifact_ref"] for s in report["sources"]])
        scoped[report["ref"]] = evidence
        for m in report["delivered"].get("verified_metrics", []):
            candidates.append({"metric":m,"delivery":"frozen_brief",**evidence})

    calls = receipts.delivered(paths[turn["thread_id"]],turn["turn_id"],turn["frozen_brief"])[3]
    for call_id, ref, model_result, line_no in read_outputs(paths[turn["thread_id"]], turn, calls):
        callback = released._native_call(db, paths, turn, call_id, "gx_read_artifact")
        if model_result != callback:
            continue
        for m in model_result.get("verified_metrics", []):
            if ref in scoped or ref == m.get("artifact_ref"):
                candidates.append({"metric":m, "delivery":"authorized_tool_read", "call_id":call_id,
                                   "rollout_line":line_no, **scoped.get(ref,{})})
    # A full read following the full brief may deliver the exact same measurement twice.
    return list({(c.get("report_ref"),c["metric"].get("metric_ref")):c for c in reversed(candidates)}.values())


def _first_module_grant(db, rid, job, turns, paths, assigned):
    # Opening a module builds its first scoped brief and then emits module.opened
    # in one transaction. Authenticate that very task and brief, as well as the
    # parent call, when the brief cursor legitimately precedes the opening event.
    if not (job["scope_authorized"] and assigned["event_id"] <= job["actor_cursor"]
            < job["grant_event"] < job["commission_event"] and job["scope"].startswith("module:")):
        return None
    subject, grant = released._event(db, rid, job["grant_event"], "v62.module.opened")
    parent = turns.get((grant["thread_id"], grant["turn_id"]))
    child = next((t for t in turns.values() if t["task_id"]==job["actor_task"] and
                  t["turn_id"]==job["actor_turn"]), None)
    if not (child and parent and parent["scope"]=="run" and parent["task_id"]==grant["parent_task_id"]
            and parent["context_ref"]==grant["parent_context_ref"] and child["task_id"]==grant["task_id"]
            and child["context_ref"]==grant["context_ref"] and grant["scope"]==job["scope"]=="module:"+subject
            and grant["run_id"]==rid and grant["goal_version"]==assigned["goal_version"]
            and assigned["artifact_ref"] in grant["effective_grant"]["input_refs"]
            and job["capability"] in grant["effective_grant"]["capabilities"]
            and released._native_call(db,paths,parent,grant["call_id"],"gx_open_module")==grant):
        return None
    return {"task_id":child["task_id"], "context_ref":child["context_ref"], "grant_event":job["grant_event"],
            "grant_sha256":digest(grant),"parent_call_id":grant["call_id"],"scope":grant["scope"]}


def _named_job(j):
    return {k:j[k] for k in ("node_id","branch_id","decision_ref","status","job_id","commission_event",
                             "event_id","input_sha256","result_sha256")} | {
        "capability":j["spec"]["capability"], "parameters":j["spec"]["parameters"]}


def extract(work, rid, rule, assignment, *, outcomes):
    """The first-stage public packet has only the opening and delivered quantitative evidence."""
    work = Path(work)
    sessions = next((p for p in (work/"codex/sessions",work/"home/codex/sessions") if p.is_dir()),None)
    need(sessions, "Original Codex session directory is missing")
    native = receipts.extract(work, rid, sessions)
    paths = receipts.native_session(sessions)
    turns = {(t["thread_id"],t["turn_id"]):t for t in native["turns"]}
    lead = [t for t in native["turns"] if t["scope"]=="run"]
    first = next((t["decision"] for t in lead if t["decision"]),None)
    need(first, "No accepted initial Lead decision")
    with closing(receipts.database(work)) as db:
        binding = released._binding(db,rid,assignment,first)
        pin,_ = receipts.artifact(db,work,rid,binding["artifact_ref"])
        for t in native["turns"]:
            t["cursor"] = db.execute("SELECT event_cursor FROM research_contexts WHERE run_id=? AND context_ref=?",
                                     (rid,t["context_ref"])).fetchone()[0]
        assay = validate_assay(rule)
        source_jobs = released._source_jobs(db,work,rid,native,turns,paths,binding,assay)
        for proof in source_jobs.values():
            proof["first_module_grant"] = _first_module_grant(db,rid,proof,turns,paths,binding)
        jobs = {j["node_id"]:j for j in native["jobs"]}
        windows = []
        for t in lead:
            w = {"window":digest([t["thread_id"],t["turn_id"],t["brief_sha256"]]),"brief_sha256":t["brief_sha256"],
                 "thread_id":t["thread_id"],"turn_id":t["turn_id"],"delivered_at":t["delivered_at"],
                 "cursor":t["cursor"], "rollout_sha256":t["rollout_sha256"],
                 "candidates":_candidates(db,work,rid,t,turns,paths,native["jobs"])}
            if outcomes:
                w["action"] = released._action(db,rid,t,jobs)
                w["automatic_nodes"] = [j["node_id"] for j in jobs.values() if j["status"]=="SUCCEEDED" and
                                        j["commission_event"]<=t["cursor"]<j["event_id"]]
            windows.append(w)
        trace = {"run_id":rid,"assignment":binding,"assay":assay,"lead_windows":windows,
                 "initial_plan":first,"measurements":source_jobs}
        if not outcomes:
            return trace
        data_file = work/"state/artifacts"/pin["namespace"]/pin["rel_path"]
        trace["independent_count"] = dict(**materials.coverage(data_file), sha256=pin["sha256"])
        claims = []
        for row in db.execute("SELECT r.sha256,r.document_json FROM hypothesis_revisions r "
                              "JOIN hypotheses h USING(hypothesis_id) WHERE h.run_id=? ORDER BY r.rowid",(rid,)):
            doc = json.loads(row["document_json"])
            need(digest(doc)==row["sha256"],"Historical claim revision content differs")
            claims.append({k:doc.get(k) for k in ("claim_revision_id","scope","level","statement","evidence_refs",
                         "publication","verified_metrics","limitations","maturity","prediction","proposition")} | {"sha256":row["sha256"]})
        inputs = json.loads(db.execute("SELECT inputs_json FROM runs WHERE run_id=?",(rid,)).fetchone()[0])["inputs"]
        trace.update(claim_publications=claims, completed_jobs=[_named_job(j) for j in jobs.values()
                     if j["status"] in ("SUCCEEDED","REUSED") and j.get("result_sha256")],
                     offered=bool(inputs["research_catalog"]["modules"]), opened=db.execute(
                     "SELECT count(*) FROM v62_modules WHERE run_id=?",(rid,)).fetchone()[0],
                     evidence_policy=inputs["evidence_policy"], closed=native["run"]["goal_status"]=="CLOSED",
                     missing=native["missing"], costs=native["costs"], actor_class="SCRIPTED_REHEARSAL"
                     if turns and all(t["native"]["model_provider"]=="gx_loopback" for t in turns.values())
                     else "OFFICIAL_PROVIDER_UNADJUDICATED")
        return trace
