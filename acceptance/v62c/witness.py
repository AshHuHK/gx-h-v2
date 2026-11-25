"""Read a finished public pilot's GX store and original Codex deliveries, without modifying them."""
from contextlib import closing
import json
from pathlib import Path

from acceptance.v62b.study import receipts as previous
from gx_core.p0 import manifest, v621_assignment, v621_metrics, v62_outputs
from .scoring import digest, need


def _event(db, rid, eid, kind):
    row = db.execute("SELECT subject_id,payload_json FROM events WHERE run_id=? AND event_id=? AND kind=?",
                     (rid, eid, kind)).fetchone()
    need(row, f"Missing original {kind} event {eid}")
    return row["subject_id"], json.loads(row["payload_json"])


def _binding(db, rid, request, opening):
    subject, assigned = _event(db, rid, request["event_id"], v621_assignment.ASSIGNED)
    need(subject == request["artifact_ref"] == assigned["artifact_ref"] and assigned["sha256"] == request["sha256"],
         "Selected assignment differs from the Run's original file pin")
    need(assigned["decision_ref"] == opening["decision_ref"] and
         opening["event_id"] < assigned["acquisition_event_id"] < request["event_id"],
         "The raw material must follow the first accepted Lead plan")
    _, acquisition = _event(db, rid, assigned["acquisition_event_id"], "v6.observation.added")
    need(acquisition["artifact_ref"] == subject and acquisition["sha256"] == request["sha256"] and
         acquisition["acquisition"]["artifact_ref"] == subject and acquisition["acquisition"]["sha256"] == request["sha256"],
         "Assignment has no matching acquisition record")
    return {**assigned, "event_id": request["event_id"], "acquisition": acquisition["acquisition"],
            "acquisition_scope": acquisition["scope"], "acquisition_sha256": digest(acquisition)}


def _grant(db, rid, producer, assignment, openings):
    brief = json.loads(producer["frozen_brief"].split("\n", 1)[1])
    item = next((m for m in brief.get("materials", []) if
                 all(m.get(k) == assignment[k] for k in ("artifact_ref", "sha256"))
                 and m.get("assignment_event_id") == assignment["event_id"]), None)
    if not item:
        return False, 0
    if producer["scope"] == "run":
        return True, assignment["event_id"]
    module_id = brief.get("module", {}).get("module_instance_id")
    opened = openings.get(module_id)
    if not opened or producer["scope"] != "module:" + module_id:
        return False, 0
    if opened["event_id"] > assignment["event_id"]:
        grant = db.execute("SELECT grant_json FROM v62_modules WHERE run_id=? AND module_instance_id=?",
                           (rid, module_id)).fetchone()
        return bool(grant and item["artifact_ref"] in json.loads(grant[0])["input_refs"]), opened["event_id"]
    eid = item.get("grant_event_id")
    if not eid:
        return False, 0
    subject, proof = _event(db, rid, eid, v621_assignment.GRANTED)
    authorized = (module_id == subject == proof["module_instance_id"] and proof["scope"] == producer["scope"]
                  and proof["assignment_event_id"] == assignment["event_id"] and
                  all(proof[k] == assignment[k] for k in ("artifact_ref", "sha256", "decision_ref", "goal_version")))
    return authorized, eid


def _native_call(db, sessions, turn, call_id, name):
    """Bind the native tool payload (including nested code-mode calls) to the exact GX root receipt."""
    path = sessions[turn["thread_id"]]
    calls = previous.delivered(path, turn["turn_id"], turn["frozen_brief"])[3]
    callback = db.execute("SELECT request_sha,result_json FROM p0_root_calls WHERE thread_id=? AND turn_id=? AND call_id=?",
                          (turn["thread_id"], turn["turn_id"], call_id)).fetchone()
    call = calls.get(call_id)
    if call and callback and call[0] == name and callback["request_sha"] == digest([name, call[1]]):
        return json.loads(callback["result_json"])
    return {}


def _source_jobs(db, work, rid, native, turns, paths, assignment, rule):
    proofs = {}
    openings = {m["module_instance_id"]: m for t in turns.values() for m in t.get("commissions", [])}
    for job in native["jobs"]:
        if (job["status"] != "SUCCEEDED" or job["spec"]["capability"] != rule["capability"]
                or job["spec"]["parameters"] != rule["parameters"]):
            continue
        d = db.execute("SELECT * FROM v6_decisions WHERE run_id=? AND decision_ref=?",
                       (rid, job["decision_ref"])).fetchone()
        producer = turns.get((d["thread_id"], d["turn_id"])) if d else None
        if not producer:
            continue
        call = _native_call(db, paths, producer, d["call_id"], "gx_decide_revision")
        # GX wraps accepted decisions as a decision receipt, depending on the native transport.
        receipt = json.loads(d["receipt_json"])
        if call != receipt or producer["context_ref"] != d["context_ref"] or producer["task_id"] != d["task_id"]:
            continue
        authorized, grant_event = _grant(db, rid, producer, assignment, openings)
        record = manifest.read_manifest(db, job["job_id"])
        create = db.execute("SELECT event_id FROM events WHERE run_id=? AND kind='job.created' AND subject_id=?",
                            (rid, job["job_id"])).fetchone()
        need(create, "Completed Job lacks a GX admission event")
        for out in record["completion"]["outputs"]:
            if out["output_name"] != rule["output_name"]:
                continue
            ref = out["artifact_ref"]
            pin, body = previous.artifact(db, work, rid, ref)
            source = v62_outputs.classify(db, rid, ref, scope=producer["scope"], through_event=job["event_id"])
            # Checked GX metric envelopes are recomputed against the installed reader and frozen output declaration.
            verified = v621_metrics.from_output(db, rid, ref)
            # Scientific selectors apply to the registered ratio method's JSON projection.
            # Other formats need the frozen reader's decoded counts, never json.loads on their raw bytes.
            try:
                document = json.loads(body)
            except (UnicodeDecodeError, ValueError):
                document = {}
            proofs[ref] = dict(artifact_ref=ref, sha256=pin["sha256"], job_id=job["job_id"], run_id=rid,
                method=record["admission"]["capability"], capability=job["spec"]["capability"],
                parameters=record["admission"]["parameters"], source_inputs=out.get("source_inputs", []),
                nature=source["nature"], output_name=out["output_name"],
                original_observations=source["original_observations"], original_inputs_grounded=source.get("original_inputs_grounded", False),
                data_refs=source["data_refs"], document=document,
                verified_metrics=verified, commission_event=job["commission_event"], admission_event=create[0],
                settled_event=job["event_id"], admission_sha256=record["admission_hash"],
                completion_sha256=record["manifest_hash"], scope=producer["scope"], actor_cursor=producer["cursor"],
                actor_turn=producer["turn_id"], actor_task=producer["task_id"], actor_brief_sha256=producer["brief_sha256"],
                grant_event=grant_event, scope_authorized=authorized)
    return proofs


def _action(db, rid, turn, jobs):
    d = turn.get("decision")
    if d is None:
        return None
    effects = []
    for n in d["new_jobs"]:
        j = jobs[n]
        effects.append({"kind": "completed_job", "ref": n, "branch": j["branch_id"], "job_id": j["job_id"], "result_ref": j["result_ref"],
                        "sha256": j["result_sha256"], "capability": j["spec"]["capability"], "parameters": j["spec"]["parameters"], "input_sha256": j["input_sha256"]})
    if d["cited"] and not any(op["op"] in ("cancel_node", "stop_branch", "close_run") for op in d["ops"]):
        for n in d["preplanned_jobs"]:
            j = jobs[n]
            admission = db.execute("SELECT event_id FROM events WHERE run_id=? AND kind='job.created' AND subject_id=?",
                                   (rid, j["job_id"])).fetchone()
            if admission and admission[0] > d["event_id"]:
                effects.append({"kind": "maintained_job", "ref": n, "branch": j["branch_id"], "job_id": j["job_id"],
                                "result_ref": j["result_ref"], "sha256": j["result_sha256"],
                                "capability": j["spec"]["capability"], "parameters": j["spec"]["parameters"], "input_sha256": j["input_sha256"]})
    effects.extend({"kind": "withdrawn_node", "ref": n} for n in d["withdrawn"])
    for op in d["ops"]:
        if op["op"] == "stop_branch":
            row = db.execute("SELECT status FROM v6_branches WHERE run_id=? AND branch_id=? AND stop_decision_ref=?",
                             (rid, op["branch"], d["decision_ref"])).fetchone()
            if row and row[0] == "STOPPED":
                effects.append({"kind": "withdrawn_branch", "ref":op["branch"]})
        if op["op"] == "close_run":
            row = db.execute("SELECT status FROM v6_goals WHERE run_id=? AND stop_decision_ref=?", (rid, d["decision_ref"])).fetchone()
            if row and row[0] == "CLOSED":
                effects.append({"kind": "closed_run", "ref": rid})
    return {**d, "effects": effects}


def _candidates(db, rid, turn, turns, paths):
    brief = json.loads(turn["frozen_brief"].split("\n", 1)[1])
    candidates = []
    for obs in brief.get("observations", []):
        for metric in obs.get("metrics", []):
            if obs.get("artifact_ref") == metric.get("artifact_ref"):
                candidates.append({"metric": metric})
    for report in turn["reports"]:
        if report["author_kind"] != "MODULE":
            continue
        row = db.execute("SELECT scope,r.receipt_json FROM v62_modules m JOIN v62_module_reports r "
                         "USING(module_instance_id,run_id) WHERE r.run_id=? AND r.report_ref=?", (rid, report["ref"])).fetchone()
        receipt = json.loads(row["receipt_json"])
        author = receipt["author"]
        worker = turns.get((author["thread_id"],author["turn_id"]))
        confirmed = bool(worker and worker["scope"] == row["scope"] and worker["task_id"] == author["task_id"]
                         and _native_call(db, paths, worker, author["call_id"], "gx_submit_module_report") == receipt)
        for metric in report["delivered"].get("metrics", []):
            candidates.append({"metric":metric, "report_ref":report["ref"], "scope":row["scope"],
                               "native_publication":confirmed, "published_event":report["published_event"],
                               "released_event":report["released_event"], "report_sha256":report["sha256"],
                               "report_sources":[s["artifact_ref"] for s in report["sources"]]})
    return candidates


def extract_run(work, rid, assay, assignment, sessions=None):
    if sessions is None:
        sessions = next((p for p in (work/"home/codex/sessions", work/"codex/sessions") if p.is_dir()), work/"home/codex/sessions")
    sessions = Path(sessions)
    native = previous.extract(work, rid, sessions)
    paths = previous.native_session(sessions)
    turns = {(t["thread_id"],t["turn_id"]):t for t in native["turns"]}
    jobs = {n["node_id"]:n for n in native["jobs"]}
    lead = [t for t in turns.values() if t["scope"] == "run"]
    opening = next((t["decision"] for t in lead if t["decision"]), None)
    need(opening, "No first accepted Lead plan")
    with closing(previous.database(work)) as db:
        binding = _binding(db, rid, assignment, opening)
        previous.artifact(db, work, rid, binding["artifact_ref"])
        for t in turns.values():
            t["cursor"] = db.execute("SELECT event_cursor FROM research_contexts WHERE run_id=? AND context_ref=?",
                                     (rid,t["context_ref"])).fetchone()[0]
        proofs = _source_jobs(db, work, rid, native, turns, paths, binding, assay)
        windows = []
        for t in lead:
            windows.append({"window": digest([t["thread_id"],t["turn_id"],t["brief_sha256"]]),
                            "brief_sha256": t["brief_sha256"], "thread_id": t["thread_id"], "turn_id": t["turn_id"],
                            "cursor": t["cursor"], "delivered_at": t["delivered_at"], "rollout_sha256":t["rollout_sha256"],
                            "candidates": _candidates(db,rid,t,turns,paths), "action": _action(db,rid,t,jobs),
                            "automatic_nodes": [k for k,j in jobs.items() if j["status"] == "SUCCEEDED" and
                                                j["commission_event"] <= t["cursor"] < j["event_id"]]})
        claims = []
        for claim in db.execute("""SELECT r.sha256,r.document_json FROM hypothesis_revisions r
                JOIN hypotheses h USING(hypothesis_id) WHERE h.run_id=? ORDER BY r.rowid""", (rid,)):
            document = json.loads(claim["document_json"])
            need(digest(document) == claim["sha256"], "Formal claim revision differs from its digest")
            claims.append({**{k: document.get(k) for k in ("claim_revision_id", "decision_id", "scope", "level",
                           "statement", "evidence_refs", "publication", "metrics", "limitations")},
                           "sha256": claim["sha256"]})
        inputs = json.loads(db.execute("SELECT inputs_json FROM runs WHERE run_id=?",(rid,)).fetchone()[0])["inputs"]
        modules = db.execute("SELECT count(*) FROM v62_modules WHERE run_id=?",(rid,)).fetchone()[0]
    from .scoring import SCHEMA
    return {"schema": SCHEMA, "run_id": rid, "assay": assay, "assignment": binding, "initial_plan": opening,
            "lead_windows": windows, "measurements": proofs, "offered": bool(inputs["research_catalog"]["modules"]),
            "opened": modules, "claim_publications": claims, "closed": native["run"]["goal_status"] == "CLOSED", "missing": native["missing"],
            "actor_class": ("SCRIPTED_REHEARSAL" if turns and all(t["native"]["model_provider"] == "gx_loopback"
                            for t in turns.values()) else "OFFICIAL_PROVIDER_UNADJUDICATED"),
            "costs": native["costs"]}
