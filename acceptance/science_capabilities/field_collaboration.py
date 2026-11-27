"""Read two field Runs and their official Codex rollouts without starting a host or stepping either Run."""
import argparse
from contextlib import closing
import hashlib
import itertools
import json
import os
from pathlib import Path
import sqlite3
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gx-h"))
from gx_core.p0 import research_dialogue, research_routes


def only(db, query, *params):
    row, = db.execute(query, params)
    return row


def selected(record, *keys):
    return {key: record[key] for key in keys}


def route_receipt(work):
    home_name = "science-" + hashlib.sha256(os.fsencode(work.resolve())).hexdigest()
    home = Path.home() / ".local/state/gx-h-oauth" / home_name
    if (home / "sessions").resolve() != work / "codex/sessions":
        raise ValueError("The OAuth deployment does not point to this Run's sessions")
    with (home / "config.toml").open("rb") as stream:
        cfg = tomllib.load(stream)
    proof = selected(cfg, "model_provider", "forced_login_method", "model", "model_reasoning_effort")
    if (proof["model_provider"], proof["forced_login_method"]) != ("openai", "chatgpt"):
        raise ValueError("Expected official ChatGPT OAuth configuration")
    return proof


def completed_turns(db, rid, version, work):
    records = []
    for root in db.execute("""SELECT r.*,t.scope,m.branch_id FROM p0_roots r
        JOIN v6_decision_tasks t USING(run_id,task_id)
        JOIN v62_modules m USING(run_id,scope,goal_version)
        WHERE r.run_id=? AND t.goal_version=? ORDER BY r.rowid""", (rid, version)):
        for t in research_routes._turns(db, rid, work / "state", root):
            if t["classification"] != "NATIVE_MODEL" or not t["start"] or not t["end"]:
                continue
            ack = db.execute("""SELECT event_id FROM p0_root_deliveries WHERE run_id=? AND thread_id=?
                    AND turn_id=? AND method='turn/start' AND state='acknowledged'""",
                             (rid, t["thread_id"], t["turn_id"])).fetchone()
            if ack:
                records.append({**selected(t, "thread_id","turn_id","model","classification","start","end"),
                                **selected(root, "task_id","scope","branch_id"), "run_id": rid,
                                "delivery_receipt": ack[0]})
    return records


def interval(a, b):
    seconds = research_routes._interval(a, b)
    if seconds is not None:
        return {"left": a, "right": b, "start_utc": max(a["start"], b["start"]),
                "end_utc": min(a["end"], b["end"]), "seconds": seconds}


def pi_trace(db, rid, mid, turns):
    msg = only(db, """SELECT m.message_text,m.created_at,c.goal_version,c.branch_id,e.event_id,e.ts
        FROM p0_operator_messages m JOIN p0_conversation_messages c USING(message_id)
        JOIN events e ON e.run_id=m.run_id AND e.subject_id=m.message_id AND e.kind='p0.conversation.received'
        WHERE m.run_id=? AND m.message_id=?""", rid, mid)
    feedback, = research_dialogue.feedback_status(db, rid, mid)
    target = feedback["scope"]
    if msg["branch_id"] != feedback["branch_id"] or msg["goal_version"] != feedback["goal_version"]:
        raise ValueError("PI feedback belongs to a different branch or version")
    active = [t for t in turns if t["start"] <= msg["ts"] < t["end"]]
    simultaneous = next((interval(a,b) for a in active if a["scope"] == target
                          for b in active if b["scope"] != target), None)
    routed = only(db, "SELECT event_id,payload_json FROM events WHERE run_id=? AND subject_id=? "
                      "AND kind='pi.message.routed'", rid, mid)
    if not any(c.get("feedback_id") == feedback["feedback_id"] and c.get("source_message_id") == mid
               and c.get("branch_id") == msg["branch_id"] for c in json.loads(routed[1])["clauses"]):
        raise ValueError("Could not link Main's route to this exact PI feedback")
    trace = {"message_id": mid, "arrival": dict(msg), "feedback_id": feedback["feedback_id"],
             "main_routed_event": routed[0], "feedback_event": feedback["event_id"],
             "status": feedback["status"], "scope": target, "arrived_during_overlap": simultaneous,
             "delivery_receipts": feedback["deliveries"], "module_report_ref": feedback.get("module_report_ref"),
             "lead_citations": [selected(d, "event_id","decision_ref","cited_refs") for d in feedback.get("lead_decisions", [])]}
    disp = feedback["disposition"]
    if not disp:
        return trace
    if (disp["feedback_id"] != feedback["feedback_id"] or disp["scope"] != target
        or disp["goal_version"] != msg["goal_version"] or not any(
            x["delivery_event_id"] == disp["delivery_event_id"] and x["turn_id"] == disp["turn_id"]
            and x["task_id"] == disp["task_id"] and x["scope"] == target for x in feedback["deliveries"])):
        raise ValueError("Disposition lacks its directed delivery")
    receipt, = only(db, "SELECT receipt_json FROM v6_decisions WHERE run_id=? "
                                "AND decision_ref=? AND task_id=? AND thread_id=? AND turn_id=?",
                                rid, disp["product_ref"], disp["task_id"], disp["thread_id"], disp["turn_id"])
    decision = json.loads(receipt)
    if decision["event_id"] != disp["product_event_id"] or not any(
            d["feedback_id"] == feedback["feedback_id"] and d["event_id"] == disp["event_id"]
            for d in decision.get("pi_dispositions", [])):
        raise ValueError("Disposition cannot be joined to its adopted decision")
    trace["disposition"] = selected(disp, "event_id","product_ref","product_event_id","reason","outcome","evidence_refs")
    jobs = []
    for node in feedback["follow_up_nodes"]:
        spec, completion = only(db, """SELECT n.spec_json,m.completion_json FROM v6_nodes n
            LEFT JOIN p0_execution_manifests m ON m.job_id=n.job_id WHERE n.run_id=? AND n.decision_ref=?
            AND n.node_id=? AND n.branch_id=?""", rid,disp["product_ref"],node["node_id"],msg["branch_id"])
        outputs = json.loads(completion or "{}").get("outputs", [])
        refs = {o["artifact_ref"] for o in outputs}
        cited_later = []
        for later in db.execute("""SELECT d.decision_ref,t.scope,d.receipt_json,d.request_json,d.task_id,d.turn_id
            FROM v6_decisions d JOIN v6_decision_tasks t USING(run_id,task_id) WHERE d.run_id=?
            AND t.goal_version=? AND json_extract(d.receipt_json,'$.event_id')>? ORDER BY d.rowid""",
            (rid,msg["goal_version"],disp["product_event_id"])):
            cited = sorted(set(json.loads(later["request_json"])["decision"]["evidence_refs"]) & refs)
            if cited:
                saved = json.loads(later["receipt_json"])
                cited_later.append({**selected(later,"scope","decision_ref","task_id","turn_id"),
                                    "event_id": saved["event_id"], "cited_outputs": cited,
                                    "operations": [o["spec"]["op"] for o in saved["resolved_ops"]]})
        events = [dict(row) for row in db.execute("""SELECT event_id,ts,new_state FROM events WHERE run_id=?
            AND subject_id=? AND kind='job.transition' AND new_state IN ('RUNNING','SUCCEEDED','FAILED','CANCELLED')
            ORDER BY event_id""", (rid,node["job_id"]))]
        jobs.append({**node, "capability":json.loads(spec)["capability"], "events":events,
                     "outputs":[selected(o,"output_name","artifact_ref") for o in outputs], "cited_later":cited_later})
    trace["jobs"] = jobs
    return trace


def snapshot(work, mid=None):
    with closing(sqlite3.connect((work / "state/gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        goal = only(db, "SELECT run_id,goal_version,status FROM v6_goals")
        rid = goal["run_id"]
        roles = completed_turns(db,rid,goal["goal_version"],work)
        routes = research_routes.routes(db,rid,work / "state",goal_version=goal["goal_version"])
        event = only(db, "SELECT event_id,ts FROM events WHERE run_id=? ORDER BY event_id DESC LIMIT 1",rid)
        handoffs = []
        for h in routes["handoffs"]:
            handoffs.append({k:h[k] for k in ("publisher","report_ref","published_event","released_event",
                            "receiver_scope","received_content","decision_ref","decision_event","status",
                            "publisher_classification","receiver_classification","real_model_handoff") if k in h})
        result = {"goal":dict(goal), "observed_through":dict(event), "oauth_deployment":route_receipt(work),
                  "completed_official_module_turns":len(roles), "reports":[
                      {"scope":m["scope"], "status":m["status"], "author":(m["report"] or {}).get("author",{}).get("kind"),
                       "report_ref":(m["report"] or {}).get("report_ref")} for m in routes["modules"]],
                  "module_report_handoffs":handoffs}
        if mid:
            result["pi"] = pi_trace(db,rid,mid,roles)
        return result, roles


def label_frames(work, turn):
    thread, tid = turn["thread_id"], turn["turn_id"]
    rollout, = (work / "codex/sessions").rglob(f"rollout-*-{thread}.jsonl")
    lines = {}
    with rollout.open("rb") as stream:
        meta = json.loads(next(stream))["payload"]
        if meta["id"] != thread or meta["model_provider"] != "openai":
            raise ValueError("The rollout is not the task's official OpenAI session")
        for index, raw in enumerate(stream, 2):
            try:
                row = json.loads(raw)
            except ValueError:
                # The host may be appending a later turn while we read a completed interval.
                break
            payload = row.get("payload", {})
            if row.get("type") != "event_msg" or payload.get("turn_id") != tid:
                continue
            for bound, kinds in (("start", ("task_started","turn_started")),
                                 ("end", ("task_complete","turn_complete","turn_aborted"))):
                if payload.get("type") in kinds and row["timestamp"] == turn[bound]:
                    lines[bound] = {"line":index, "event":payload["type"]}
    if set(lines) != {"start","end"}:
        raise ValueError("Official task-frame boundaries did not match this interval")
    return {"rollout":str(rollout.relative_to(work)), "codex_version":meta.get("cli_version"),
            "originator":meta.get("originator"), **lines}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trehalose", type=Path)
    parser.add_argument("sucrose", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    a, ta = snapshot(args.trehalose.resolve(), "sugar-original-assay-question")
    b, tb = snapshot(args.sucrose.resolve())
    both = sorted((o for x,y in itertools.product(ta,tb) if (o:=interval(x,y))), key=lambda o:o["start_utc"])
    result = {"schema":"gx.field_collaboration/1", "evidence":"GX SQLite read snapshots and completed official Codex rollouts",
              "trehalose":a, "sucrose":b, "cross_run_module_overlap_pairs":len(both),
              "first_cross_run_module_overlap": next(iter(both), None)}
    work_by_run = {a["goal"]["run_id"]:args.trehalose, b["goal"]["run_id"]:args.sucrose}
    for overlap in (a["pi"]["arrived_during_overlap"], result["first_cross_run_module_overlap"]):
        if overlap:
            for side in ("left","right"):
                turn = overlap[side]
                turn["official_frames"] = label_frames(work_by_run[turn["run_id"]], turn)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
