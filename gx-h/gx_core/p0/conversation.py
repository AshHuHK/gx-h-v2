"""Project conversation over the operator journal. Each input keeps its send-time Run."""
import json
from pathlib import Path
from uuid import uuid4

from ..domain import ids
from ..errors import IdempotencyConflict, ScopeDenied
from ..persistence import store
from . import workspace


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)).fetchone())


def _goal_version(conn, run_id):
    row = conn.execute("SELECT goal_version,status,stop_decision_ref FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
    if row and row["status"] == "CLOSED" and row["stop_decision_ref"]:
        from .research_report import completed_close
        return completed_close(conn, run_id)["goal_version"]
    return row[0] if row else None


def _conversation(run):
    return "conversation:" + run["project_ref"]


def _public_input(row):
    action = json.loads(row["action_json"])
    return action.get("kind") in ("text", "proposal", "supplement") and action.get("text") == row["message_text"]


def reply_anchor(conn, run, ref):
    """Resolve a public message or a released research reference, including older turns."""
    anchored = conn.execute("""
        SELECT m.*,a.response FROM p0_operator_messages m JOIN runs r USING(run_id)
        LEFT JOIN p0_main_replies a USING(message_id) WHERE m.message_id=? AND r.project_ref=?
        """, (ref,run["project_ref"])).fetchone()
    if anchored and _public_input(anchored):
        return {"kind":"message", "run_id":anchored["run_id"], "message_id":ref,
                "text":anchored["message_text"], "reply":anchored["response"]}
    decision = conn.execute("SELECT t.goal_version FROM v6_decisions d JOIN v6_decision_tasks t "
                            "USING(run_id,task_id) WHERE d.decision_ref=? AND d.run_id=? AND t.scope='run'",
                            (ref,run["run_id"])).fetchone()
    if decision:
        return {"kind":"decision","decision_ref":ref,"goal_version":decision[0]}
    released = conn.execute("""
        SELECT m.branch_id,m.goal_version FROM v62_module_reports r JOIN v62_modules m USING(run_id,module_instance_id)
        JOIN v62_module_wakes w ON w.run_id=m.run_id JOIN events e ON e.event_id=w.event_id
        JOIN json_each(e.payload_json,'$.module_instance_ids') member ON member.value=m.module_instance_id
        WHERE r.run_id=? AND r.report_ref=? LIMIT 1""",(run["run_id"],ref)).fetchone()
    if released:
        return {"kind":"report","report_ref":ref,"branch_id":released[0],"goal_version":released[1]}
    raise ValueError("Unknown conversation message or published research reference on this Run")


def send(conn, run_id, text, message_id=None, *, reply_to=None, branch_id=None, question_id=None,
         answer_option=None, conversation_id=None, goal_version=None, attachments=None, successor=None):
    """Persist verbatim PI input before attempting any native or scientific action."""
    from .driver import enqueue
    if not isinstance(text, str) or not text.strip():
        raise ValueError("A nonempty PI message is required")
    mid = str(uuid4()) if message_id is None else message_id
    for value in (mid, reply_to, branch_id, question_id, conversation_id):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("Conversation and message anchors must be nonempty strings")
    if answer_option is not None and (not question_id or not isinstance(answer_option, str) or not answer_option.strip()):
        raise ValueError("An answer option requires a question ID and a nonempty choice")
    if goal_version is not None and (type(goal_version) is not int or goal_version < 1):
        raise ValueError("Goal version must be a positive integer")
    if attachments is not None and not isinstance(attachments, list):
        raise ValueError("attachments must be an array")
    if successor is not None and not isinstance(successor, dict):
        raise ValueError("successor must be an object")
    run = workspace.find_run(conn, run_id=run_id)
    cid = _conversation(run)
    if conversation_id is not None and conversation_id != cid:
        raise ValueError("Conversation does not belong to this project")
    envelope = [run_id, text, reply_to, branch_id, question_id, attachments or [], successor]
    if answer_option is not None:  # Existing receipts retain their original digest.
        envelope.append(answer_option)
    digest = ids.sha256_of_obj(envelope)
    answer = None
    pinned = successor
    if successor and successor.get("input_files") and not conn.execute(
            "SELECT 1 FROM p0_operator_messages WHERE message_id=?", (mid,)).fetchone():
        from .successor import freeze_files
        pinned = freeze_files(store.data_root(conn), successor)
    with store.transaction(conn, transaction_owner="Conversation", operation="receive", run_id=run_id) as tx:
        existing = tx.execute("SELECT * FROM p0_operator_messages WHERE message_id=?", (mid,)).fetchone()
        metadata = tx.execute("SELECT * FROM p0_conversation_messages WHERE message_id=?", (mid,)).fetchone()
        if existing:
            if (existing["run_id"] != run_id or existing["message_text"] != text or not _public_input(existing)
                    or (metadata and (metadata["envelope_sha256"] != digest or
                        (goal_version is not None and metadata["goal_version"] != goal_version)))
                    or (not metadata and any((reply_to, branch_id, question_id, attachments, successor is not None,
                                               goal_version is not None)))):
                raise IdempotencyConflict("This message ID already belongs to a different input, scope or Run")
            version = metadata["goal_version"] if metadata else None
            if metadata and metadata["answer_json"]:
                answer = json.loads(metadata["answer_json"])
        else:
            current = _goal_version(tx, run_id)
            version = goal_version if goal_version is not None else current
            if goal_version is not None and (current is None or goal_version > current):
                raise ValueError("Unknown Goal version on this Run")
            if reply_to:
                reply_anchor(tx, run, reply_to)
            if question_id or branch_id:
                from . import research_dialogue
                canonical_branch = research_dialogue._target(tx, run_id, branch_id)[1] if branch_id else None
                if question_id:
                    question = next((q for q in research_dialogue.questions(tx, run_id)
                                     if q["question_id"] == question_id), None)
                    if question is None:
                        raise ScopeDenied("No such PI question in this Run")
                    if branch_id and question["branch_id"] != canonical_branch:
                        raise ScopeDenied("The selected branch and question refer to different research")
                    if answer_option is not None and answer_option not in question["choices"]:
                        raise ValueError("Choose an option offered by this exact research question")
            enqueue(tx, run_id, {"kind": "text", "text": text}, text, mid)
            tx.execute("INSERT INTO p0_conversation_messages VALUES(?,?,?,?,?,?,?,NULL,?)",
                       (mid, cid, version, reply_to, branch_id, question_id, digest,
                        ids.canonical_json(pinned) if pinned is not None else None))
            if attachments:
                from . import pi_materials
                pi_materials.register(tx, run_id, mid, branch_id=branch_id, attachments=attachments)
            if answer_option is not None:
                answer = research_dialogue.answer(tx, run_id, question_id, mid, text, goal_version=version,
                                                  selected_choice=answer_option)
                tx.execute("UPDATE p0_conversation_messages SET answer_json=? WHERE message_id=?",
                           (ids.canonical_json(answer), mid))
            store.append_event(tx, kind="p0.conversation.received", actor="gx-operator", run_id=run_id,
                               subject_kind="message", subject_id=mid,
                               payload={"conversation_id":cid, "message_id":mid, "goal_version":version,
                                        "reply_to":reply_to, "branch_id":branch_id, "question_id":question_id})
    return {"message_id":mid, "run_id":run_id, "conversation_id":cid, "goal_version":version,
            "status":"received", **({"answer":answer} if answer is not None else {})}


def _entries(conn, run, *, through=None, limit=None):
    # An immutable pre-upgrade journal need not be migrated just to read it.
    metadata = _table(conn, "p0_conversation_messages")
    columns = ("c.goal_version,c.reply_to,c.branch_id,c.question_id,c.answer_json,c.successor_json" if metadata else
               "NULL goal_version,NULL reply_to,NULL branch_id,NULL question_id,NULL answer_json,NULL successor_json")
    query = ("SELECT m.rowid AS sequence,m.*,r.turn_id,r.status AS turn_status,r.response,d.thread_id,"
             + columns + " FROM p0_operator_messages m LEFT JOIN p0_main_replies r USING(message_id) "
             "LEFT JOIN p0_root_deliveries d ON d.run_id=m.run_id AND d.event_id=('human:'||m.message_id) "
             + ("LEFT JOIN p0_conversation_messages c USING(message_id) " if metadata else "")
             + "WHERE m.run_id=? AND json_extract(m.action_json,'$.text')=m.message_text "
               "AND json_extract(m.action_json,'$.kind') IN ('text','proposal','supplement') ")
    params = [run["run_id"]]
    if through:
        query += "AND m.rowid <= (SELECT rowid FROM p0_operator_messages WHERE message_id=? AND run_id=?) "
        params.extend([through, run["run_id"]])
    if limit is None:
        rows = conn.execute(query + "ORDER BY m.rowid", params)
    else:
        rows = reversed(conn.execute(query + "ORDER BY m.rowid DESC LIMIT ?", (*params, limit)).fetchall())
    entries = []
    for row in rows:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        reply = None if not row["turn_id"] else {"text":row["response"], "turn_id":row["turn_id"],
                                                  "thread_id":row["thread_id"], "status":row["turn_status"]}
        status = ("rejected" if row["rejected"] else "answered" if reply and reply["text"] else
                  "ended_without_reply" if reply else "successor_started" if result and result.get("status") == "successor_started"
                  else "main_received" if result else "received")
        followup = {}
        if result and result.get("status") == "successor_started":
            started = conn.execute("""SELECT 1 FROM p0_roots r JOIN p0_root_deliveries d
                ON d.run_id=r.run_id AND d.thread_id=r.thread_id WHERE r.run_id=? AND r.task_id IS NOT NULL
                AND d.event_id='start:'||r.root_key AND d.state='acknowledged' AND d.turn_id IS NOT NULL LIMIT 1""",
                (result["run_id"],)).fetchone()
            followup["successor_execution"] = "started" if started else "created"
        entries.append({**followup, "run_id":run["run_id"], "conversation_id":_conversation(run),
                        "text":row["message_text"], "status":status, "reply":reply, "result":result,
                        "successor":row["successor_json"] is not None, "answer":json.loads(row["answer_json"]) if row["answer_json"] else None,
                        **{k:row[k] for k in ("message_id","created_at","goal_version","reply_to",
                                              "branch_id","question_id","rejected")}})
    return entries


def pending(conn, run_id):
    """Only ordinary inputs whose actual native answer or successor receipt remains outstanding."""
    return [r[0] for r in conn.execute("""
        SELECT m.message_id FROM p0_operator_messages m LEFT JOIN p0_main_replies r USING(message_id)
        WHERE m.run_id=? AND m.rejected IS NULL AND r.message_id IS NULL
          AND json_extract(m.action_json,'$.kind') IN ('text','proposal','supplement')
          AND json_extract(m.action_json,'$.text')=m.message_text
          AND COALESCE(json_extract(m.result_json,'$.status'),'')!='successor_started'
        ORDER BY m.rowid""", (run_id,))]



def timeline(conn, run_id):
    run = workspace.find_run(conn, run_id=run_id)
    # Give cursor and rows the same snapshot, including when WAL writers append during GET.
    conn.execute("SAVEPOINT gx_conversation_timeline")
    try:
        messages = _entries(conn, run)
        questions = []
        if _table(conn, "p0_conversation_messages"):
            from . import pi_clauses, pi_materials, research_dialogue
            questions = research_dialogue.questions(conn, run_id)
            feedback = {}
            for item in research_dialogue.feedback_status(conn, run_id):
                feedback.setdefault(item["source_message_id"], []).append(item)
            for m in messages:
                mid = m["message_id"]
                m["clauses"] = pi_clauses.clauses(conn, run_id, mid)
                m["feedback"] = feedback.get(mid, [])
                m["materials"] = pi_materials.for_message(conn, run_id, mid)
        runs = [{"run_id":r["run_id"], "run_key":r["run_key"], "status":r["status"],
                 "predecessor":store.loads(r,"inputs_json")["inputs"].get("p0_spec",{}).get("predecessor")}
                for r in workspace.list_runs(conn) if r["project_ref"] == run["project_ref"]]
        return {"conversation_id":_conversation(run), "run_id":run_id, "project_ref":run["project_ref"],
                "messages":messages, "runs":runs, "questions":questions,
                "cursor":conn.execute("SELECT COALESCE(MAX(event_id),0) FROM events").fetchone()[0]}
    finally:
        conn.execute("RELEASE gx_conversation_timeline")


def process(conn, directory, run_id, message_id):
    from .conversation_delivery import process as drive
    return drive(conn, Path(directory), run_id, message_id)
