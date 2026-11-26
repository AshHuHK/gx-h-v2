"""PI questions, replies and scientific feedback attached to immutable Run decisions."""

from __future__ import annotations

import hashlib

from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied, StaleRevision
from ..persistence import store

ASK = "research.pi.question"
ANSWER = "research.pi.answered"
FEEDBACK = "research.pi.feedback"
DISPOSE = "research.pi.disposition"


def _events(conn, rid, kind, *, through=None):
    return conn.execute("SELECT event_id,ts,subject_id,payload_json FROM events WHERE run_id=? "
                        "AND kind=? AND (? IS NULL OR event_id<=?) ORDER BY event_id",
                        (rid, kind, through, through)).fetchall()


def _save(conn, rid, kind, key, actor, payload):
    return store.append_event(conn, run_id=rid, kind=kind, actor=actor, subject_kind="research_dialogue",
                              subject_id=key, payload=payload)


def _target(conn, rid, scope):
    if scope == "run":
        return scope, None, True
    if not isinstance(scope, str) or not scope.strip():
        raise ScopeDenied("Select an existing branch in this Run")
    value = scope.removeprefix("branch:")
    module = conn.execute("SELECT scope,branch_id,status FROM v62_modules WHERE run_id=? "
                          "AND (scope=? OR branch_id=? OR module_instance_id=?)",
                          (rid, scope, value, value)).fetchone()
    if module:
        return module["scope"], module["branch_id"], module["status"] == "OPEN"
    branch = conn.execute("SELECT status FROM v6_branches WHERE run_id=? AND branch_id=?",
                          (rid, value)).fetchone()
    if branch:
        return "branch:" + value, value, branch[0] == "OPEN"
    raise ScopeDenied("Select an existing branch in this Run")


def _current(conn, rid):
    return conn.execute("SELECT g.goal_version,g.status FROM v6_goals g WHERE run_id=?", (rid,)).fetchone()


def _source(conn, rid, mid, text):
    from .conversation import _public_input
    row = conn.execute("SELECT message_text,action_json FROM p0_operator_messages WHERE run_id=? AND message_id=?",
                       (rid, mid)).fetchone()
    if not row or not _public_input(row) or not isinstance(text, str) or not text.strip() or text not in row[0]:
        raise ScopeDenied("Use an exact passage from this Run's PI message")


def questions(conn, run_id, goal_version=None, *, through=None):
    asked = _events(conn, run_id, ASK, through=through)
    if not asked:
        return []
    answers = {r["subject_id"]: r for r in _events(conn, run_id, ANSWER, through=through)}
    current = _current(conn, run_id)
    result = []
    for row in asked:
        q = store.loads(row, "payload_json")
        if goal_version is not None and q["goal_version"] != goal_version:
            continue
        q.update(event_id=row["event_id"], asked_at=row["ts"], status="OPEN")
        answered = answers.get(row["subject_id"])
        if answered:
            q.update(store.loads(answered, "payload_json"), answer_event_id=answered["event_id"],
                     answered_at=answered["ts"], status="ANSWERED")
        elif (not current or current[0] != q["goal_version"] or current[1] != "OPEN"
              or not _target(conn, run_id, q["scope"])[2]):
            q["status"] = "STALE"
        result.append(q)
    return result


def ask(conn, rid, task, root, receipt, items):
    asked = []
    for item in items:
        scope, branch, active = _target(conn, rid, item.get("branch_id", task["scope"]))
        if not active or (task["scope"] != "run" and scope != task["scope"]):
            raise ScopeDenied("A PI question must concern an authorized open branch")
        question = item["question"].strip()
        choices = item.get("choices", [])
        blocking = item["blocking"]
        version = task["goal_version"]
        key = "piq_" + ids.sha256_of_obj([rid, version, scope, receipt["decision_ref"], question])[:32]
        prior = next((q for q in questions(conn, rid, version) if q["status"] == "OPEN" and
                      (q["question"], q["scope"], q["choices"], q["blocking"]) ==
                      (question, scope, choices, blocking)), None)
        if prior:
            asked.append(prior)
            continue
        if any(q["question_id"] == key for q in asked):
            raise SchemaInvalid("Use distinct PI questions within one decision")
        payload = dict(question_id=key, scope=scope, branch_id=branch, goal_version=version,
                       question=question, choices=choices, blocking=blocking,
                       decision_ref=receipt["decision_ref"], decision_event_id=receipt["event_id"],
                       asking_scope=task["scope"], task_id=task["task_id"])
        _save(conn, rid, ASK, key, root["thread_id"], payload)
        asked.append(next(q for q in questions(conn, rid, version) if q["question_id"] == key))
    return asked


def blocked(conn, rid, *, scope=None, branch_id=None):
    # Called at Job admission. Stop at the first matching current, unanswered question.
    return conn.execute("""SELECT 1 FROM events q JOIN v6_goals g USING(run_id)
        WHERE q.run_id=? AND q.kind=? AND g.status='OPEN' AND g.goal_version=json_extract(q.payload_json,'$.goal_version')
          AND json_extract(q.payload_json,'$.blocking')=1
          AND (json_extract(q.payload_json,'$.scope')=? OR json_extract(q.payload_json,'$.branch_id')=?)
          AND NOT EXISTS (SELECT 1 FROM events a WHERE a.subject_kind='research_dialogue'
              AND a.subject_id=q.subject_id AND a.kind=?)
        LIMIT 1""", (rid, ASK, scope, branch_id, ANSWER)).fetchone() is not None


def answer(conn, run_id, question_id, message_id, text, *, goal_version=None):
    with store.transaction(conn, transaction_owner="ResearchDialogue", operation="answer", run_id=run_id):
        _source(conn, run_id, message_id, text)
        q = next((q for q in questions(conn, run_id) if q["question_id"] == question_id), None)
        if not q:
            raise ScopeDenied("No such PI question in this Run")
        if goal_version is not None and goal_version != q["goal_version"]:
            return {**q, "status": "STALE"}
        if q["status"] == "ANSWERED":
            return q if q["answer_message_id"] == message_id and q["answer"] == text else {**q, "status": "ALREADY_ANSWERED"}
        if q["status"] == "STALE":
            return q
        payload = {k: q[k] for k in ("goal_version", "scope", "branch_id", "question_id", "asking_scope")}
        payload.update(answer=text, answer_message_id=message_id)
        _save(conn, run_id, ANSWER, question_id, "PI", payload)
        return next(q for q in questions(conn, run_id) if q["question_id"] == question_id)


def add_feedback(conn, run_id, message_id, *, goal_version, scope, text, kind="hypothesis", artifact_refs=()):
    with store.transaction(conn, transaction_owner="ResearchDialogue", operation="record_feedback", run_id=run_id):
        _source(conn, run_id, message_id, text)
        scope, branch, active = _target(conn, run_id, scope)
        refs = list(dict.fromkeys(artifact_refs))
        payload = dict(source_message_id=message_id, goal_version=goal_version, scope=scope,
                       branch_id=branch, text=text, source_kind=kind, artifact_refs=refs)
        key = "pif_" + ids.sha256_of_obj([run_id, payload])[:32]
        payload["feedback_id"] = key
        old = conn.execute("SELECT event_id FROM events WHERE subject_kind='research_dialogue' AND subject_id=? "
                           "AND kind=? AND run_id=?", (key, FEEDBACK, run_id)).fetchone()
        if old:
            return dict(payload, event_id=old[0])
        current = _current(conn, run_id)
        if not current or current[0] != goal_version or current[1] != "OPEN" or not active:
            raise StaleRevision("The selected Goal version or recipient has closed")
        if kind not in {"hypothesis", "information", "material"}:
            raise SchemaInvalid("Specify whether the PI supplied a hypothesis, information, or material")
        return dict(payload, event_id=_save(conn, run_id, FEEDBACK, key, "PI", payload))


def packet(conn, rid, scope, version, *, through=None):
    """Only the next eligible task receives a new snapshot. An unanswered question is never a measurement."""
    items = []
    for q in questions(conn, rid, version, through=through):
        if q["status"] == "STALE" or (scope != q["scope"] and not
                (scope == "run" and (q["asking_scope"] == "run" or q["scope"].startswith("branch:")))):
            continue
        items.append({"kind": "answer" if q["status"] == "ANSWERED" else "question", **q})
    disposed = {row["subject_id"] for row in _events(conn, rid, DISPOSE, through=through)}
    for row in _events(conn, rid, FEEDBACK, through=through):
        f = store.loads(row, "payload_json")
        if f["feedback_id"] in disposed or f["goal_version"] != version:
            continue
        if scope != f["scope"]:
            if scope != "run":
                continue
            if f["scope"].startswith("module:") and _target(conn, rid, f["scope"])[2]:
                continue
            if f["artifact_refs"]:
                # A scoped attachment stays within the recipient's original grant.
                items.append({k: f[k] for k in ("feedback_id", "source_message_id", "scope", "branch_id")}
                             | {"kind": "private_material_pending"})
                continue
        items.append({"kind": "feedback", "event_id": row["event_id"], **f})
    return items


def _native_deliveries(conn, rid):
    from . import v6_native
    result = {}
    for row in conn.execute("""SELECT r.task_id,r.thread_id,r.brief,t.scope,d.turn_id,
            d.event_id AS delivery_event_id,d.text_sha256
        FROM p0_roots r JOIN v6_decision_tasks t ON t.task_id=r.task_id
        JOIN p0_root_deliveries d ON d.run_id=r.run_id AND d.thread_id=r.thread_id
        WHERE r.run_id=? AND d.state='acknowledged' AND d.method='turn/start'
          AND instr(r.brief,'"pi_dialogue"')>0""", (rid,)):
        if hashlib.sha256(row["brief"].encode("utf-8")).hexdigest() != row["text_sha256"]:
            continue
        for item in v6_native.brief(row).get("pi_dialogue", ()):
            if item["kind"] == "feedback":
                fields = ("task_id", "thread_id", "scope", "turn_id", "delivery_event_id")
                result.setdefault(item["feedback_id"], []).append({k: row[k] for k in fields})
    return result


def feedback_status(conn, run_id, message_id=None):
    rows = _events(conn, run_id, FEEDBACK)
    if not rows:
        return []
    deliveries = _native_deliveries(conn, run_id)
    dispositions = {r["subject_id"]: dict(store.loads(r, "payload_json"), event_id=r["event_id"])
                    for r in _events(conn, run_id, DISPOSE)}
    result = []
    for row in rows:
        f = store.loads(row, "payload_json")
        if message_id is not None and f["source_message_id"] != message_id:
            continue
        key = f["feedback_id"]
        seen = deliveries.get(key, [])
        disposed = dispositions.get(key)
        trace = _consequences(conn, run_id, f, disposed) if disposed else {}
        result.append(dict(f, event_id=row["event_id"], disclosed_at=row["ts"], deliveries=seen,
                           disposition=disposed, status="DISPOSED" if disposed else "DELIVERED" if seen else "PENDING",
                           **trace))
    return result


def _consequences(conn, rid, feedback, disposition):
    # A scoped disposition, its commissioned work, and a Lead citing the released product
    # are distinct results. Link a later decision only when it actually cites this chain.
    ref = disposition["product_ref"]
    nodes = [dict(r) for r in conn.execute(
        "SELECT node_id,branch_id,status,job_id,result_ref FROM v6_nodes "
        "WHERE run_id=? AND decision_ref=? ORDER BY rowid", (rid, ref))]
    sources = {ref}
    sources.update(n["result_ref"] for n in nodes if n["result_ref"])
    report = None
    if feedback["scope"].startswith("module:"):
        row = conn.execute("SELECT p.report_ref,p.receipt_json FROM v62_module_reports p "
                           "JOIN v62_modules m USING(module_instance_id,run_id) WHERE m.run_id=? AND m.scope=?",
                           (rid, feedback["scope"])).fetchone()
        if row and (row["report_ref"] == ref or row["report_ref"] in disposition["evidence_refs"]
                    or set(store.loads(row, "receipt_json")["source_refs"]) & sources):
            report = row["report_ref"]
            sources.add(report)
    lead = []
    for row in conn.execute("SELECT d.decision_ref,d.request_json,d.receipt_json FROM v6_decisions d "
                            "JOIN v6_decision_tasks t USING(task_id) WHERE d.run_id=? AND t.scope='run' "
                            "AND t.goal_version=? AND json_extract(d.receipt_json,'$.event_id')>=? ORDER BY d.rowid",
                            (rid, feedback["goal_version"], disposition["product_event_id"])):
        decision = store.loads(row, "request_json")["decision"]
        cited = sorted(sources.intersection(decision["evidence_refs"]))
        if cited or row["decision_ref"] == ref:
            receipt = store.loads(row, "receipt_json")
            lead.append(dict(decision_ref=row["decision_ref"], event_id=receipt["event_id"],
                             rationale=decision["rationale"], cited_refs=cited, effects=receipt["resolved_ops"]))
    return dict(follow_up_nodes=nodes, module_report_ref=report, lead_decisions=lead)


def dispose(conn, rid, root, turn, task, items, *, product_ref, product_kind, event_id, evidence_refs, effects):
    from . import v6_native

    actual = v6_native.acknowledged_brief(conn, root, turn)
    seen = {item["feedback_id"]: item for item in (actual[0].get("pi_dialogue", []) if actual else [])
            if item["kind"] == "feedback"}
    accepted = []
    for item in items:
        if (not isinstance(item, dict) or set(item) != {"feedback_id", "outcome", "reason"}
                or not isinstance(item.get("feedback_id"), str)
                or item.get("outcome") not in ("adopt", "maintain", "measure", "defer")
                or not isinstance(item.get("reason"), str) or not item["reason"].strip()):
            raise SchemaInvalid("A feedback disposition requires its feedback_id, outcome and scientific reason")
        key = item["feedback_id"]
        if key not in seen:
            raise ScopeDenied("Only dispose a PI suggestion present in this task's acknowledged native brief")
        prior = conn.execute("SELECT event_id FROM events WHERE subject_kind='research_dialogue' "
                             "AND subject_id=? AND kind=? AND run_id=?", (key, DISPOSE, rid)).fetchone()
        if prior:
            raise StaleRevision("A scientific disposition already exists for this feedback",
                                details={"feedback_id": key, "event_id": prior[0]})
        if item["outcome"] == "measure":
            branch = seen[key]["branch_id"]
            if not any(op.get("op") == "add_node" and (branch is None or op["branch"] == branch)
                       for op in effects):
                raise SchemaInvalid("A measure disposition must commission a measurement in the addressed branch")
        payload = dict(item, product_ref=product_ref, **{product_kind + "_ref": product_ref},
                       product_event_id=event_id, task_id=task["task_id"], scope=task["scope"],
                       goal_version=task["goal_version"], thread_id=root["thread_id"], turn_id=turn,
                       delivery_event_id=actual[1]["event_id"], delivery_turn_id=actual[1]["turn_id"],
                       evidence_refs=evidence_refs, effects=effects)
        accepted.append(dict(payload, event_id=_save(conn, rid, DISPOSE, key, root["thread_id"], payload)))
    return accepted
