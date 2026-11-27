"""Provenance and scoped delivery for the clauses interpreted by the native Codex Main."""
import json

from ..domain import clock, ids
from ..errors import PermissionDenied, SchemaInvalid, ScopeDenied, StaleRevision
from ..persistence import store
from . import report_language

KINDS = ("question", "discussion", "preference", "feedback", "material", "reply", "clarify")
_FIELDS = {
    "kind": {"type": "string", "enum": list(KINDS)},
    "quote": {"type": "string", "description": "Exact continuous excerpt from this human message."},
    "branch_id": {"type": "string", "description": "Actual branch ID from gx_project_state, or run for the Lead."},
    "source_kind": {"type": "string", "enum": ["hypothesis", "information"], "description": "PI conjecture or PI-reported information."},
    "report_language": {"type": "string", "enum": list(report_language.LANGUAGES),
                        "description": "For an explicit scientific report language preference; omit for chat replies."},
    "reader": {"type": "object", "description": "On an explicit PI request to translate the sealed report, include the translated "
               "pi_message.sealed_report fields, preserving references, quantities and units.",
               "properties": {"reason": {"type": "string"}, "rationale": {"type": "string"},
                              "unresolved": {"type": "array", "items": {"type": "string"}}},
               "required": ["reason", "rationale", "unresolved"], "additionalProperties": False},
    "persistence": {"type": "string", "enum": ["turn", "conversation"], "description": "Use conversation only for an enduring request."},
    "question_id": {"type": "string", "description": "An actual question ID for reply."},
    "artifact_refs": {"type": "array", "items": {"type": "string"}, "description": "Refs attached to this message."},
    "clarification": {"type": "string", "description": "What the PI needs to disambiguate for this clause."},
}
TOOL = {"type": "function", "name": "gx_route_pi_message",
        "description": "Record this PI message's clauses together. Answer questions in your reply. Send a "
        "scientific suggestion only when requested, to its addressed route. Use clarify for the ambiguous "
        "clause and process the others. B continuing or discussion only needs no new research work. "
        "A question ID alone is a discussion reference. Use reply only when the PI explicitly answers the "
        "researcher; inquiries and deferred choices remain discussion. Preserve original wording. "
        "PI input is never a verified measurement.",
        "inputSchema": {"type": "object", "properties": {"clauses": {"type": "array", "minItems": 1,
            "items": {"type": "object", "properties": _FIELDS, "required": ["kind", "quote"],
                      "additionalProperties": False}}}, "required": ["clauses"], "additionalProperties": False}}
ROUTED = "pi.message.routed"


def clauses(conn, run_id, message_id):
    row = conn.execute("SELECT payload_json FROM events WHERE run_id=? AND kind=? AND subject_id=? "
                       "ORDER BY event_id LIMIT 1", (run_id, ROUTED, message_id)).fetchone()
    return json.loads(row[0])["clauses"] if row else []


def _binding(conn, run_id, message_id):
    # Old journals have no conversation envelope. They remain readable by their original Run.
    from .conversation import _table
    if _table(conn, "p0_conversation_messages"):
        row = conn.execute("SELECT c.* FROM p0_conversation_messages c JOIN p0_operator_messages m "
                           "USING(message_id) WHERE m.run_id=? AND c.message_id=?", (run_id, message_id)).fetchone()
        if row:
            return dict(row)
    return {}


def main_context(conn, run_id, message_id):
    from . import pi_materials
    goal = conn.execute("SELECT goal_version FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
    if not goal:
        return {}
    binding = _binding(conn, run_id, message_id)
    version = binding.get("goal_version") or goal[0]
    prefs = conn.execute("""SELECT e.subject_id,c.value FROM events e JOIN runs r USING(run_id)
        JOIN json_each(e.payload_json,'$.clauses') c
        JOIN p0_operator_messages m ON m.message_id=e.subject_id JOIN p0_operator_messages asked
        ON asked.message_id=? AND asked.rowid>=m.rowid
        WHERE e.kind=? AND json_extract(c.value,'$.kind')='preference'
        AND json_extract(c.value,'$.persistence')='conversation'
        AND r.project_ref=(SELECT project_ref FROM runs WHERE run_id=?)
        ORDER BY e.event_id DESC LIMIT 8""", (message_id, ROUTED, run_id))
    source = _sealed_source(conn, run_id, message_id, version)
    return {"message_id": message_id, "goal_version": version,
            **({"sealed_report": source} if source else {}),
            "branch_id": binding.get("branch_id"), "question_id": binding.get("question_id"),
            "reply_to": binding.get("reply_to"),
            "presentation_preferences": [{"source_message_id": p[0], "preference": json.loads(p[1])["quote"]}
                                         for p in prefs],
            "this_messages_materials": pi_materials.for_message(conn, run_id, message_id)}



def _sealed_source(conn, rid, mid, version):
    from . import research_report
    row = conn.execute("""SELECT message_text FROM p0_operator_messages m JOIN v6_goals g USING(run_id)
                          WHERE m.message_id=? AND m.run_id=? AND g.status='CLOSED'""", (mid, rid)).fetchone()
    if row is None or not report_language.REPORT.search(row[0]):
        return None
    close = research_report.completed_close(conn, rid)
    if close["goal_version"] != version:
        return None
    final = research_report.closing_decision(conn, rid, close)
    rendered = research_report.render_close(conn, rid, final, {})
    readings = [{k: record["reading"][k] for k in ("metric_ref", "display", "unit") if k in record["reading"]}
                for record in final["used_endpoints"]]
    return {"decision_ref": close["decision_ref"], "goal_version": version,
            "evidence_refs": final["evidence_refs"], "measurements": readings, **rendered}


def _source(conn, run_id, thread_id, turn_id):
    rows = conn.execute("""SELECT i.* FROM p0_intent_main main JOIN p0_human_inputs i USING(run_id)
        JOIN p0_operator_messages m USING(run_id,message_id)
        WHERE main.run_id=? AND main.thread_id=? AND i.receipt_turn_id=? AND i.received_at IS NOT NULL
        AND i.action_json=m.action_json AND m.message_text=json_extract(i.action_json,'$.text')
        AND json_extract(i.action_json,'$.kind')='text' LIMIT 2""", (run_id, thread_id, turn_id)).fetchall()
    if len(rows) != 1:
        raise PermissionDenied("This tool requires the bound Main turn for an acknowledged original PI message")
    return rows[0]


def _validate(parts, original):
    if not isinstance(parts, list) or not parts:
        raise SchemaInvalid("Provide the interpreted clauses together")
    for part in parts:
        if not isinstance(part, dict) or part.get("kind") not in KINDS or set(part) - _FIELDS.keys():
            raise SchemaInvalid("Unknown PI clause or field")
        quote = part.get("quote")
        if not isinstance(quote, str) or not quote.strip() or quote not in original:
            raise PermissionDenied("Each clause must quote a continuous excerpt of this PI message verbatim")
        for field in ("branch_id", "question_id", "clarification"):
            if field in part and (not isinstance(part[field], str) or not part[field].strip()):
                raise SchemaInvalid(f"Provide a nonempty {field}")
        if part.get("source_kind", "hypothesis") not in ("hypothesis", "information"):
            raise SchemaInvalid("PI testimony can be recorded as hypothesis or PI-reported information")
        if part.get("persistence", "turn") not in ("turn", "conversation"):
            raise SchemaInvalid("Choose turn or conversation for a presentation preference")
        if {"report_language", "reader"} & part.keys() and part["kind"] != "preference":
            raise SchemaInvalid("Use a preference clause for report presentation")
        refs = part.get("artifact_refs", [])
        if not isinstance(refs, list) or any(not isinstance(x, str) or not x for x in refs):
            raise SchemaInvalid("Supply attachment references as strings")
        if refs and part["kind"] != "material":
            raise SchemaInvalid("Attach material with a scoped material clause")
        if part["kind"] == "material" and not refs:
            raise SchemaInvalid("Select the actual material attached to this PI message")
        if part["kind"] == "clarify" and not part.get("clarification"):
            raise SchemaInvalid("Identify what is unclear in this clause")


def _scope(conn, run_id, scope):
    if not scope or scope == "run":
        return scope
    row = conn.execute("SELECT branch_id FROM v62_modules WHERE run_id=? "
                       "AND (? IN (branch_id,module_instance_id,scope))", (run_id, scope)).fetchone()
    return row[0] if row else scope.removeprefix("branch:")


def _apply(conn, run_id, row, binding, goal, clause, *, reader_source=None):
    from . import pi_materials, research_dialogue
    mid = row["message_id"]
    version = binding.get("goal_version") or goal["goal_version"]
    kind, quote = clause["kind"], clause["quote"]
    key = "clause_" + ids.sha256_of_obj([mid, clause])[:32]
    result = {"clause_id": key, "kind": kind, "quote": quote, "source_message_id": mid, "goal_version": version}
    if kind == "preference":
        preference = {**result, "status": "recorded", "persistence": clause.get("persistence", "turn")}
        if branch := binding.get("branch_id") or clause.get("branch_id"):
            preference["branch_id"] = branch
        if lang := report_language.requested(quote, clause.get("report_language")):
            preference["report_language"] = lang
        if "reader" in clause:
            source = reader_source
            if not source or source["goal_version"] != version or branch not in (None, "run") or not lang:
                raise SchemaInvalid("A translated reader needs the selected sealed Run and an explicit report language")
            report_language.verify_reader(source, clause["reader"], lang)
            preference.update(reader=clause["reader"], decision_ref=source["decision_ref"])
        return preference
    if kind == "clarify":
        return {**result, "status": "needs_clarification", "clarification": clause["clarification"]}
    if kind in ("discussion", "question"):
        return {**result, "status": "for_main_reply"}
    target = _scope(conn, run_id, binding.get("branch_id"))
    branch = _scope(conn, run_id, clause.get("branch_id")) or target
    if target and target != branch:
        raise ScopeDenied("This message was addressed to a different research route")
    if kind == "reply":
        anchor = binding.get("question_id")
        qid = clause.get("question_id") or anchor
        if not qid or (anchor and qid != anchor):
            raise ScopeDenied("Resolve the reply to the selected PI question")
        questions = research_dialogue.questions(conn, run_id)
        question = next((q for q in questions if q["question_id"] == qid), None)
        if question is None or (branch and branch not in (question["branch_id"], question["scope"])):
            raise ScopeDenied("This question is outside the addressed research route")
        answer = research_dialogue.answer(conn, run_id, qid, mid, quote, goal_version=version)
        return {**result, "question_id": qid, "answer": answer, "status": answer["status"]}
    if not branch:
        raise SchemaInvalid("Select the addressed branch ID, or run for the Scientific Lead")
    if version != goal["goal_version"] or goal["status"] != "OPEN":
        return {**result, "branch_id": branch, "status": "historical", "reason": "Goal version or admission changed"}
    refs = clause.get("artifact_refs", [])
    if kind == "material":
        pi_materials.grant(conn, run_id, mid, clause_id=key, branch_id=None if branch == "run" else branch,
                           artifact_refs=refs)
    source_kind = "material" if kind == "material" else clause.get("source_kind", "hypothesis")
    feedback = research_dialogue.add_feedback(conn, run_id, mid, goal_version=version, scope=branch,
                                               text=quote, kind=source_kind, artifact_refs=refs)
    return {**result, "status": "routed", "branch_id": branch, "source_kind": source_kind,
            "artifact_refs": refs, "feedback_id": feedback["feedback_id"]}


def process(conn, run_id, thread_id, turn_id, clauses):
    """Record one interpretation under the actual main-thread receipt. No clause writes a Goal or Job."""
    reader_source = None
    if isinstance(clauses, list) and any(isinstance(part, dict) and "reader" in part for part in clauses):
        # Rendering an immutable sealed decision can open several named artifacts. Do it outside the writer.
        msg = _source(conn, run_id, thread_id, turn_id)
        if not msg["consumed_at"]:
            version = _binding(conn, run_id, msg["message_id"]).get("goal_version")
            reader_source = _sealed_source(conn, run_id, msg["message_id"], version)
    with store.transaction(conn, transaction_owner="Conversation", operation="route_clauses", run_id=run_id):
        row = _source(conn, run_id, thread_id, turn_id)
        previous = json.loads(row["result_json"])
        if row["consumed_at"]:
            return previous
        if previous.get("interpretation", {}).get("status") != "conversation":
            raise PermissionDenied("This formal input belongs to the existing Intent preview and confirmation flow")
        original = json.loads(row["action_json"])["text"]
        _validate(clauses, original)
        mid = row["message_id"]
        binding = _binding(conn, run_id, mid)
        goal = conn.execute("SELECT goal_version,status FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
        applied = []
        for clause in clauses:
            try:
                # An invalid attachment or ambiguous target must not undo unrelated, grounded clauses.
                with store.transaction(conn, transaction_owner="Conversation", operation="apply_pi_clause", run_id=run_id):
                    outcome = _apply(conn, run_id, row, binding, goal, clause, reader_source=reader_source)
            except (ScopeDenied, SchemaInvalid, StaleRevision, PermissionDenied) as exc:
                if "reader" in clause:
                    raise
                outcome = {"kind": clause["kind"], "quote": clause["quote"], "source_message_id": mid,
                           "status": "needs_clarification", "clarification": exc.message}
            if outcome not in applied:
                applied.append(outcome)
        result = {"status": "routed", "message_id": mid, "clauses": applied}
        if "unapplied_proposal" in previous:
            result["unapplied_proposal"] = previous["unapplied_proposal"]
        store.append_event(conn, kind=ROUTED, actor=thread_id, run_id=run_id,
                           subject_kind="message", subject_id=mid, payload=result)
        encoded = ids.canonical_json(result)
        conn.execute("UPDATE p0_human_inputs SET consumed_at=?,result_json=? WHERE message_id=?",
                     (clock.now_iso(), encoded, mid))
        conn.execute("UPDATE p0_operator_messages SET result_json=? WHERE run_id=? AND message_id=?",
                     (encoded, run_id, mid))
        return result
