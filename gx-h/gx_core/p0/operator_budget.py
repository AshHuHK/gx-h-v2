"""Exact local previews for a run allocation or prospective project resource limit."""
import json

from .. import budgets
from ..domain import ids
from ..errors import BudgetExceeded, PermissionDenied, SchemaInvalid, StaleRevision
from ..governance import approvals
from ..persistence import store
from . import intent, intent_text


def pending(conn, rid):
    rows = conn.execute("""
        SELECT m.message_id,m.result_json FROM p0_operator_messages m
        WHERE m.run_id=? AND json_extract(m.action_json,'$.kind')='budget_preview'
          AND json_extract(m.result_json,'$.budget_scope')='run' AND NOT EXISTS
          (SELECT 1 FROM events e WHERE e.run_id=m.run_id AND e.subject_id=m.message_id
           AND e.kind='p0.operator.budget_applied') ORDER BY m.rowid""", (rid,))
    return [{"message_id": row["message_id"], "result": json.loads(row["result_json"])} for row in rows]


def mark_displayed(conn, shown):
    with store.transaction(conn):
        for mid, result in shown:
            if result.get("budget_scope") != "run":
                continue
            row = conn.execute("SELECT run_id,result_json FROM p0_operator_messages WHERE message_id=? "
                               "AND json_extract(action_json,'$.kind')='budget_preview'", (mid,)).fetchone()
            if not row or not row["result_json"] or json.loads(row["result_json"]) != result:
                continue
            rid = row["run_id"]
            if conn.execute("SELECT 1 FROM events WHERE run_id=? AND subject_id=? "
                            "AND kind='p0.operator.budget_displayed'", (rid, mid)).fetchone():
                continue
            position = conn.execute("SELECT max(rowid) FROM p0_operator_messages WHERE run_id=?", (rid,)).fetchone()[0]
            store.append_event(conn, kind="p0.operator.budget_displayed", run_id=rid, subject_kind="operator_input",
                               subject_id=mid, actor="operator-client", payload={"position": position,
                               "preview_sha256": result["preview_sha256"]})


def _binding(conn, run, resource):
    row = conn.execute("SELECT * FROM budgets WHERE budget_ref=?", (run["budget_ref"],)).fetchone()
    if row is None:
        raise SchemaInvalid("This run has no recorded budget allocation")
    limits = store.loads(row, "limits_json")
    if resource not in limits:
        raise SchemaInvalid("The run allocation has no such resource line")
    event = conn.execute("""
        SELECT max(event_id) FROM events WHERE subject_kind='budget' AND subject_id=? AND
        (kind IN ('budget.created','budget.child_opened') OR
         (kind IN ('budget.raised','budget.allotment_changed') AND json_extract(payload_json,'$.key')=?) OR
         (kind='budget.tightened' AND EXISTS(SELECT 1 FROM json_each(payload_json,'$.limits') WHERE key=?)))""",
        (run["budget_ref"], resource, resource)).fetchone()
    return {"run_id": run["run_id"], "budget_ref": run["budget_ref"], "resource": resource,
            "current_limit": limits[resource], "previous_event": event[0], "parent_budget_ref": row["parent_ref"]}, row


def _preview(ctx, run, mid, action):
    key, target = action["resource"], action["limit"]
    if action.get("budget_scope", "run") == "project":
        value = {"resource": key, "max": target}
        if key in intent.NATIVE_UNITS:
            value["unit"] = intent.NATIVE_UNITS[key]
        prepared = intent_text._single_edit(ctx.conn, run["run_id"], "resource_limit", value, key, resource=key)
        if prepared.get("kind") != "change":
            return prepared
        return intent._apply_local(ctx.conn, run["run_id"], mid, prepared, force_preview=True)
    binding, row = _binding(ctx.conn, run, key)
    plan = {**binding, "new_limit": target}
    held = store.loads(row, "reserved_json", {}).get(key, 0)
    spent = store.loads(row, "spent_json", {}).get(key, 0)
    if target < spent + held:
        raise BudgetExceeded("A run allocation preserves spent and reserved funds. Use a project scope limit "
                             "to stop new expenditure prospectively while preserving these funds.",
                             details={"spent": spent, "reserved": held, "requested": target})
    return {"status": "awaiting_confirmation", "budget_scope": "run", "preview_id": mid, "preview": plan,
            "preview_sha256": ids.sha256_of_obj({"message_id": mid, **plan}), "spent": spent, "reserved": held}


def _displayed_plan(conn, rid, mid, preview_id, digest):
    row = conn.execute("""
        SELECT p.result_json FROM p0_operator_messages p JOIN p0_operator_messages a ON a.run_id=p.run_id
        JOIN events e ON e.run_id=p.run_id AND e.subject_id=p.message_id AND e.kind='p0.operator.budget_displayed'
        WHERE p.run_id=? AND p.message_id=? AND a.message_id=? AND a.rowid>json_extract(e.payload_json,'$.position')
          AND json_extract(p.action_json,'$.kind')='budget_preview' AND json_extract(p.result_json,'$.budget_scope')='run'
          AND json_extract(p.result_json,'$.preview_sha256')=? AND json_extract(e.payload_json,'$.preview_sha256')=?
          AND json_extract(a.action_json,'$.kind')='budget_apply' AND json_extract(a.action_json,'$.preview_id')=?
          AND json_extract(a.action_json,'$.preview_sha256')=? AND a.rejected IS NULL AND a.result_json IS NULL
          AND NOT EXISTS(SELECT 1 FROM events c WHERE c.run_id=p.run_id AND c.subject_id=p.message_id
                         AND c.kind='p0.operator.budget_applied')""",
        (rid, preview_id, mid, digest, digest, preview_id, digest)).fetchone()
    if row:
        return json.loads(row["result_json"])["preview"]


def allows_grant(ctx, approval_id, mid):
    """Existing approvals owner verifies the subject of a displayed local budget authorization."""
    from .operator import authorized
    if not mid or ctx.tool != "gx_p0_operator":
        return False
    row = ctx.conn.execute("SELECT * FROM approvals WHERE approval_id=? AND action_kind=? AND status='PENDING'",
                           (approval_id, budgets.INCREASE_ACTION)).fetchone()
    if not row:
        return False
    run = authorized(ctx, row["run_id"])
    message = ctx.conn.execute("SELECT action_json FROM p0_operator_messages WHERE run_id=? AND message_id=?",
                               (run["run_id"], mid)).fetchone()
    if not message:
        return False
    action = json.loads(message[0])
    plan = _displayed_plan(ctx.conn, run["run_id"], mid, action.get("preview_id"), action.get("preview_sha256"))
    if not plan or plan["budget_ref"] != row["subject_id"]:
        return False
    subject = budgets.increase_subject(plan["budget_ref"], plan["resource"], plan["new_limit"])
    return subject.kind == row["subject_kind"] and subject.sha256 == row["subject_sha256"]


def perform(ctx, run, mid, action):
    if action["kind"] == "budget_preview":
        return _preview(ctx, run, mid, action)
    conn, rid = ctx.conn, run["run_id"]
    preview_id, digest = action["preview_id"], action["preview_sha256"]
    formal = conn.execute("SELECT preview_json FROM p0_intent_changes WHERE run_id=? AND change_id=?",
                          (rid, preview_id)).fetchone()
    if formal:
        if not all(d["item_type"] == "resource_limit" for d in json.loads(formal[0])):
            raise SchemaInvalid("The indicated change also edits non-budget Intent. Use its exact confirm action.")
        confirm = {"kind": "confirm", "change_id": preview_id, "preview_sha256": digest}
        if "acknowledge_consequences" in action:
            confirm["acknowledge_consequences"] = action["acknowledge_consequences"]
        return intent._apply_local(conn, rid, mid, confirm)
    plan = _displayed_plan(conn, rid, mid, preview_id, digest)
    if not plan:
        raise PermissionDenied("Confirm this run's displayed pending budget preview in a later local action")
    key, target = plan["resource"], plan["new_limit"]
    binding, _ = _binding(conn, run, key)
    if {**binding, "new_limit": target} != plan:
        raise StaleRevision("The allocation changed after display. Request a fresh budget preview.")
    budget_ref = run["budget_ref"]
    approval_id = None
    if plan["parent_budget_ref"]:
        from ..budgets.allotment import set_run_limit
        set_run_limit(conn, rid, budget_ref, key=key, limit=target, actor=ctx.actor)
    elif target > plan["current_limit"]:
        approval = budgets.request_increase(ctx, budget_ref, key=key, limit=target, reason="Local operator budget adjustment")
        approval_id = approval["approval_id"]
        if approval["status"] == approvals.PENDING:
            approvals.decide(ctx, approval_id=approval_id, decision=approvals.GRANTED, note="Confirmed local budget preview",
                             expected_subject_sha256=approval["subject"]["sha256"], operator_budget_message=mid)
        budgets.raise_limit(conn, budget_ref, key=key, limit=target, approval_id=approval_id, actor=ctx.actor)
    elif target < plan["current_limit"]:
        budgets.tighten(conn, budget_ref, limits={key: target}, actor=ctx.actor, reason="Confirmed local budget preview")
    store.bump_revision(conn, rid)
    store.append_event(conn, kind="p0.operator.budget_applied", run_id=rid, subject_kind="operator_input",
                       subject_id=preview_id, actor=ctx.actor, payload={"message_id": mid, "preview_sha256": digest,
                       "approval_id": approval_id, "resource": key, "limit": target})
    return {"status": "committed", "budget_scope": "run", "preview_id": preview_id, "preview_sha256": digest,
            "approval_id": approval_id, "resource": key, "limit": target}
