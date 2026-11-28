"""Export scope, delivery, raw data reads and actual decisions of the first late-material study."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import evidence

RUN = "gxr_01m8fs36kqvx147ysh"


def load(path):
    return json.loads(path.read_bytes())


def collect(work, run=RUN):
    state = work / "state"
    host = evidence.rollouts(work)
    db = sqlite3.connect((state / "gx.db").resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")
    # Use the same deployment that admitted the official first Run for status projections.
    release = work/"release" if (work/"release/gx-h").is_dir() else work.parent/"hydro/release"
    sys.path.insert(0, str(release/"gx-h"))
    from gx_core.p0 import research_dialogue, v6_goal
    wait = v6_goal.waiting_for(db, run)
    feedback = research_dialogue.feedback_status(db,run)
    goal = dict(db.execute("SELECT run_id,goal_version,status FROM v6_goals WHERE run_id=?", (run,)).fetchone())
    files = {}
    documents = {}
    for name in ("first-station.json", "later-station.json"):
        raw = (HERE / name).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        row = db.execute("SELECT artifact_ref FROM artifacts WHERE run_id=? AND sha256=? AND published=1", (run, digest)).fetchone()
        if row:
            ref = row["artifact_ref"]
            files[ref] = {"name": name, "sha256": digest,
                          "raw_matches_accepted_input": evidence.bytes_for(db, state, ref, digest) == raw}
            documents[ref] = json.loads(raw)
    reports = []
    for row in db.execute("SELECT receipt_json FROM v62_module_reports WHERE run_id=? ORDER BY rowid", (run,)):
        rec = json.loads(row[0])
        raw = evidence.bytes_for(db, state, rec["report_ref"], rec["report_sha256"])
        body = json.loads(raw) if raw else {}
        author = rec["author"]
        official = host.get((author["thread_id"], author["turn_id"]), {}).get("provider") == "openai"
        reports.append({**evidence.pick(rec, "event_id wake_event_id scope report_ref report_sha256 author status source_refs"),
                        "whole_report_verified": bool(raw), "official_worker": official,
                        **evidence.pick(body, "researcher_statement findings gaps next_steps endpoint_state")})
        documents[rec["report_ref"]] = body
    calls = []
    trace = db.execute("""SELECT n.rowid AS seq,n.*,c.result_json,t.scope FROM p0_v6_native_calls n
        JOIN p0_root_calls c USING(thread_id,turn_id,call_id) JOIN p0_roots r USING(thread_id)
        JOIN v6_decision_tasks t USING(task_id) WHERE r.run_id=? ORDER BY n.rowid""", (run,))
    for row in trace:
        req, out = json.loads(row["request_json"]), json.loads(row["result_json"])
        if not isinstance(out, dict):
            continue
        tool, args = req["tool"], req["arguments"]
        item = {**evidence.pick(row, "seq thread_id turn_id scope call_id"), "tool": tool,
                "official_turn": host.get((row["thread_id"], row["turn_id"]), {}).get("provider") == "openai"}
        if tool == "gx_read_artifact":
            ref = args.get("ref")
            item.update(ref=ref, full_document_match=ref in documents and documents[ref] == out,
                        returned_keys=sorted(out.keys()), endpoint_total=out.get("endpoint_total"))
        elif tool == "gx_read_source":
            item.update(ref=args.get("source_ref"), query=args.get("query"), **evidence.pick(out,"source_id receipt_id segment_id text_sha256"))
        elif tool == "gx_query_catalog":
            item.update(arguments=args, returned_total=out.get("total"), detail_reference=out.get("reference"))
        elif tool == "gx_decide_revision":
            draft = args["decision"]
            item.update(rationale=draft.get("rationale"), evidence_refs=draft.get("evidence_refs",[]),
                        operations=[evidence.pick(op,"op reason intent question") for op in draft.get("ops",[])],
                        pi_questions=draft.get("pi_questions", []), pi_dispositions=draft.get("pi_dispositions", []),
                        refusal=out.get("decision_refusal"), **evidence.pick(out,"decision_ref outcome"))
        elif tool == "gx_task_context":
            item.update(present_materials=sorted(set(files).intersection(evidence.strings(out))),
                        report_content=evidence.delivered_content(out), pi_dialogue=out.get("pi_dialogue", []),
                        peer_hints=out.get("peer_hints", []),
                        research_topics=out.get("research_topics", []),
                        topic_findings=out.get("research_topic_findings", []))
        elif tool == "gx_open_module":
            item.update(appointment=evidence.pick(args,"researcher focus expected_delivery communication route_intent"),
                        resulting_scope=out.get("scope"), module_instance_id=out.get("module_instance_id"))
        elif tool in ("gx_send_research_hint", "gx_watch_join", "gx_submit_module_report"):
            item.update(arguments=evidence.pick(args,"recipient_scope target topic action text question interpretation parent_hint_ids"),
                        returned=evidence.pick(out,"outcome event_id report_ref hint_id wake_event_id"))
        else:
            continue
        calls.append(item)
    timeline = []
    for row in db.execute("SELECT event_id,kind,ts,subject_id,payload_json FROM events WHERE run_id=? ORDER BY event_id", (run,)):
        kind = row["kind"]
        if (kind.startswith(("research.topic.","research.join.","research.pi.","pi.","v6.route.","v62.module.peer"))
            or kind in ("p0.conversation.received","v621.module.material_granted","v6.decision.committed",
                        "v62.module.reported","v6.run.closed","v6.research.successor_opened")):
            body = json.loads(row["payload_json"])
            timeline.append({**evidence.pick(row,"event_id kind ts subject_id"),
                             "receipt":evidence.pick(body,"branch_id scope module_instance_id event_id goal_version message_id "
                                "status feedback_id source_message_id text artifact_ref artifact_refs report_ref hint_id sender_scope "
                                "recipient_scope topic action op intent revision phase decision_ref evidence_refs outcome reason")})
    turns = evidence.turns(db,run,host,set(documents))
    for entry in turns:
        if entry["full_brief_acknowledged"]:
            brief = db.execute("SELECT brief FROM p0_roots WHERE thread_id=?", (entry["thread_id"],)).fetchone()[0]
            packet = json.loads(brief.split("\n", 1)[1])
            entry["pi_dialogue"] = packet.get("pi_dialogue", [])
            entry["assigned_materials"] = packet.get("assigned_materials", [])
    decisions = evidence.decisions(db,run)
    jobs = evidence.jobs(db,state,run)
    usage = [dict(r) for r in db.execute("""SELECT u.thread_id,u.turn_id,u.terminal_status,u.total_tokens,u.measured_tokens,
                          u.metering_reason FROM p0_root_turn_usage u
                   JOIN p0_roots r USING(thread_id) WHERE r.run_id=? ORDER BY u.rowid""",(run,))]
    archived = {key:load(work/(key + ".json")) for key in
                ("entry","observer-recovery","pre-crash","disconnect","queued","resumed-driver-process","after-resume")
                if (work/(key + ".json")).exists()}
    for name in ("entry","resumed-driver-process"):
        if name in archived:
            archived[name] = evidence.pick(archived[name], "stamp pid protocol_sha256 binary_sha256 model effort")
    db.close()
    return {"run":goal, "protocol_sha256":archived["entry"]["protocol_sha256"],
            "recovery":archived,"waiting_for":wait,"feedback":feedback,"materials":files,"timeline":timeline,"native_turns":turns,"calls":calls,
            "reports":reports,"decisions":decisions,"jobs":jobs,"turn_usage":usage}


if __name__ == "__main__":
    json.dump(collect(Path(sys.argv[1]).resolve(), sys.argv[2] if len(sys.argv)>2 else RUN),sys.stdout,ensure_ascii=False,indent=2)
    print()
