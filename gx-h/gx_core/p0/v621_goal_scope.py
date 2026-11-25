"""Attribute Goal progress to the objective that authorized the work."""
from . import v621_policy


def active(conn, rid):
    versions = ", ".join("?" for _ in v621_policy.GOVERNED)
    goal = conn.execute(f"""SELECT g.goal_version FROM runs r JOIN v6_goals g USING(run_id)
        WHERE r.run_id=? AND json_extract(r.inputs_json,'$.inputs.evidence_policy') IN ({versions})""",
        (rid, *v621_policy.GOVERNED)).fetchone()
    return goal[0] if goal else None


def start_cursor(conn, rid, version):
    if version is None or version == 1:
        return 0
    row = conn.execute("""SELECT e.event_id FROM events e, json_each(e.payload_json,'$.items') edit
        WHERE e.run_id=? AND e.kind='v6.intent.changed'
          AND json_extract(edit.value,'$.item_id')='objective' AND json_extract(edit.value,'$.version')=?
        ORDER BY e.event_id LIMIT 1""", (rid, version)).fetchone()
    return row[0] if row else 0


def applies(conn, rid, row, payload, version):
    if version is None or version == 1:
        return True
    if "goal_version" in payload:
        return payload["goal_version"] == version
    # Previously commissioned nodes can finish after an amendment. Recover ownership
    # from the immutable commissioning decision, even when the Job was reused.
    decision = payload.get("decision_ref") or payload.get("decision_id")
    if decision:
        owner = conn.execute("""SELECT t.goal_version FROM v6_decisions d JOIN v6_decision_tasks t
                             USING(task_id) WHERE d.run_id=? AND d.decision_ref=?""", (rid, decision)).fetchone()
        return not owner or owner[0] == version
    if row["kind"] == "v62.claim.reassessed":
        owner = conn.execute("""SELECT t.goal_version FROM hypothesis_revisions r
            JOIN v6_decision_tasks t ON t.context_ref=json_extract(r.document_json,'$.context_ref')
            WHERE t.run_id=? AND r.hypothesis_id=? AND r.revision=?
            """, (rid, row["subject_id"], int(payload["claim_ref"].rsplit("@", 1)[1]))).fetchone()
        return not owner or owner[0] == version
    return True
