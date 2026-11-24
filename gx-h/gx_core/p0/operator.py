"""Local operator commands. Only the trusted CLI supplies this ingress and its principal."""
import json
import os

from .. import budgets
from ..context import ENV_SCOPE, ENV_SUBJECT, Ctx, Principal, SCOPE_OPERATOR, principal_from_env
from ..domain import ids
from ..errors import ArtifactNotFound, GxError, PermissionDenied, SchemaInvalid
from ..orchestration import reactor, service
from ..persistence import store
from . import intent, intent_budget, waits

PREFIX = "My requested action is "
INTENT = {"change", "cancel_jobs", "confirm", "proposal", "supplement", "text"}
READS = {"status", "inspect", "usage", "pending", "audit"}
CONTROL = {"pause", "resume", "cancel", "retry", "recover"}
KINDS = READS | CONTROL | {"budget_preview", "budget_apply", "usage_resolve"}


def local_context(conn):
    env = {**os.environ, ENV_SCOPE: os.environ.get(ENV_SCOPE) or SCOPE_OPERATOR,
           ENV_SUBJECT: os.environ.get(ENV_SUBJECT) or "gx-p0-operator"}
    return Ctx(conn, principal_from_env(env, source="local_operator"), tool="gx_p0_operator")


def authorize_project(ctx, project_ref):
    if ctx.transport != "cli" or ctx.native_call is not None or ctx.principal.scope != SCOPE_OPERATOR:
        raise PermissionDenied("This command requires a local GX operator")
    if not ctx.principal.may_access_project(project_ref):
        raise PermissionDenied("This operator has no access to the selected project")


def authorized(ctx, rid):
    row = ctx.conn.execute("SELECT * FROM runs WHERE run_id=?", (rid,)).fetchone()
    if not row or not waits.is_p0_run(row):
        raise ArtifactNotFound("Select a GX scientific run in this workspace")
    authorize_project(ctx, row["project_ref"])
    return row


def _check(action):
    if not isinstance(action, dict) or not isinstance(action.get("kind"), str):
        raise SchemaInvalid("Supply a structured GX action with a kind")
    kind = action["kind"]
    if kind in INTENT:
        intent._check_action(action)
    elif kind in READS | CONTROL:
        optional = {"reason"} if kind in CONTROL else set()
        if not intent._keys(action, {"kind"}, optional) or ("reason" in action and not intent._text(action["reason"])):
            raise SchemaInvalid("Unexpected operator action fields or empty reason")
    elif kind == "usage_resolve":
        from . import operator_usage
        operator_usage.check(action)
    elif kind == "budget_preview":
        if (not intent._keys(action, {"kind", "resource", "limit"}, {"budget_scope"})
                or action.get("budget_scope", "run") not in ("run", "project")
                or not intent._text(action["resource"]) or not intent._number(action["limit"]) or action["limit"] < 0):
            raise SchemaInvalid("A budget preview needs a resource, nonnegative finite limit and run or project scope")
    elif kind == "budget_apply":
        if not (intent._keys(action, {"kind", "preview_id", "preview_sha256"}, {"acknowledge_consequences"})
                and intent._text(action["preview_id"]) and intent._text(action["preview_sha256"])
                and ("acknowledge_consequences" not in action or
                     action["acknowledge_consequences"] in (intent.CANCEL_ACK, intent.IMPACT_ACK))):
            raise SchemaInvalid("Name the exact displayed budget preview and SHA-256")
    else:
        raise SchemaInvalid("Unknown local GX operator action")
    return kind


def _event(ctx, rid, mid, kind, payload):
    store.append_event(ctx.conn, kind="p0.operator." + kind, run_id=rid, actor=ctx.actor,
                       subject_kind="operator_input", subject_id=mid,
                       evidence_level="EXECUTION_OBSERVED", payload=payload)


def _message(conn, mid):
    return conn.execute("SELECT * FROM p0_operator_messages WHERE message_id=?", (mid,)).fetchone()


def _native_owns(conn, rid, mid):
    return conn.execute("SELECT 1 FROM p0_root_deliveries WHERE run_id=? AND event_id=? "
                        "AND state IN ('sending','acknowledged')", (rid, "human:" + mid)).fetchone() is not None


def _local_row(row):
    return row["message_text"] == PREFIX + row["action_json"]


def submit(ctx, rid, action, message_id=None, *, queue_only=False):
    """Apply a structured action or stage it for the local driver. Replays return the first receipt."""
    from .driver import enqueue
    authorized(ctx, rid)
    _check(action)
    if message_id is not None and not intent._text(message_id):
        raise SchemaInvalid("Supply a nonempty message ID")
    with store.transaction(ctx.conn,transaction_owner="Operator",operation="submit",run_id=rid):
        prior = _message(ctx.conn, message_id) if message_id else None
        text = PREFIX + ids.canonical_json(action)
        # Retain an earlier native --action kind=text's real receipt on replay.
        if prior and prior["result_json"] and action["kind"] == "text" and prior["message_text"] == action["text"]:
            text = prior["message_text"]
        mid = enqueue(ctx.conn, rid, action, text, message_id)
        if not prior:
            _event(ctx, rid, mid, "submitted", {"action": action["kind"], "project_refs": ctx.principal.project_refs,
                                                "subject": ctx.principal.subject})
    if queue_only:
        return {"message_id": mid, "status": "queued"}
    return {"message_id": mid, "result": _apply_or_reject(ctx, rid, mid)}


def apply_bootstrap(conn, rid, action, event_id):
    """Initialize a keyed workspace run with the supplied local operator Intent."""
    if action.get("kind") != "change":
        raise SchemaInvalid("A new run requires initial Intent items")
    return submit(local_context(conn), rid, action, event_id)["result"]


def consume_queued(ctx, rid, row):
    if not _local_row(row) or _native_owns(ctx.conn, rid, row["message_id"]):
        return False
    authorized(ctx, rid)
    ctx = _submitted_context(ctx,rid,row["message_id"])
    try:
        _apply_or_reject(ctx, rid, row["message_id"])
    except GxError:
        pass
    return True


def _submitted_context(ctx, rid, mid):
    origin = ctx.conn.execute("SELECT payload_json FROM events WHERE run_id=? AND kind='p0.operator.submitted' "
                              "AND subject_id=? ORDER BY event_id LIMIT 1", (rid,mid)).fetchone()
    if not origin:
        return ctx
    data = json.loads(origin[0])
    return ctx.child(principal=Principal(data["subject"], SCOPE_OPERATOR, tuple(data["project_refs"]),source="local_operator"))


def _apply_or_reject(ctx, rid, mid):
    try:
        return _consume(ctx, rid, mid)
    except GxError as exc:
        if exc.side_effect_status == "UNKNOWN":
            raise
        with store.transaction(ctx.conn,transaction_owner="Operator",operation="reject",run_id=rid):
            saved = _message(ctx.conn,mid)
            if saved["result_json"] and json.loads(saved["action_json"])["kind"] == "usage_resolve":
                return json.loads(saved["result_json"])
            updated = ctx.conn.execute("UPDATE p0_operator_messages SET rejected=? WHERE run_id=? AND message_id=? "
                                       "AND rejected IS NULL AND result_json IS NULL", (exc.message, rid, mid))
            if updated.rowcount:
                _event(ctx, rid, mid, "rejected", {"code": exc.code, "reason": exc.message})
        raise


def _consume(ctx, rid, mid):
    run = authorized(ctx, rid)
    row = _message(ctx.conn, mid)
    action = json.loads(row["action_json"])
    kind = _check(action)
    if kind == "usage_resolve":
        ctx = _submitted_context(ctx,rid,mid)
        authorized(ctx,rid)
    if row["rejected"]:
        raise SchemaInvalid(row["rejected"])
    if row["result_json"]:
        saved = json.loads(row["result_json"])
        if kind in READS and saved.get("local_read") == kind:
            return _read(ctx, run, kind, mid)
        return saved
    if _native_owns(ctx.conn, rid, mid):
        return {"status": "pending_native_receipt", "message_id": mid}
    if kind in CONTROL:
        # Job cancellation and recovery use their existing outside-transaction protocols.
        result = _control(ctx, run, mid, action)
    if kind in READS:
        result = _read(ctx, run, kind, mid)
        read_digest = ids.sha256_of_obj(result)
    proof = None
    if kind == "usage_resolve" and action.get("source") == "codex-rollout":
        from . import operator_usage
        proof = operator_usage.prepare_history(ctx.conn,rid,action)
    with store.transaction(ctx.conn,transaction_owner="Operator",operation="apply",run_id=rid):
        fresh = _message(ctx.conn, mid)
        if fresh["rejected"]:
            raise SchemaInvalid(fresh["rejected"])
        if fresh["result_json"]:
            saved = json.loads(fresh["result_json"])
            if kind in READS and saved.get("local_read") == kind:
                return result
            return saved
        if kind == "usage_resolve":
            from . import operator_usage
            result = operator_usage.perform(ctx,rid,mid,action,proof)
        elif kind in INTENT:
            result = intent._apply_local(ctx.conn, rid, mid, action)
        elif kind in ("budget_preview", "budget_apply"):
            from . import operator_budget
            result = operator_budget.perform(ctx, run, mid, action)
        # Views are reconstructed from their owners. Repeated inspection of a long run
        # must not copy its budget history or an entire audit export into the message log.
        receipt = {"local_read": kind, "run_id": rid} if kind in READS else result
        ctx.conn.execute("UPDATE p0_operator_messages SET result_json=? WHERE message_id=?",
                         (ids.canonical_json(receipt), mid))
        _event(ctx, rid, mid, "applied", {"action": kind, "status": result.get("status"),
                                         "result_sha256": read_digest if kind in READS else ids.sha256_of_obj(result)})
    return result


def _control(ctx, run, mid, action):
    from .replan import prepare_replan
    rid, kind = run["run_id"], action["kind"]
    if kind in ("retry", "recover"):
        from . import metering
        pending = ctx.conn.execute("SELECT * FROM p0_operator_messages WHERE run_id=? AND message_id<>? "
                                   "AND rejected IS NULL AND result_json IS NULL "
                                   "AND json_extract(action_json,'$.kind') NOT IN ('retry','recover') ORDER BY rowid",
                                   (rid, mid)).fetchall()
        local_messages = [row["message_id"] for row in pending if consume_queued(ctx, rid, row)]
        metering.maintain(ctx.conn, rid)
        recovered = reactor.lazy(ctx, rid, retry_parked=kind == "retry")
        return {"run_id": rid, "recovery": recovered, "local_messages": local_messages, "warnings": ctx.warnings,
                "status": ctx.conn.execute("SELECT status FROM runs WHERE run_id=?", (rid,)).fetchone()[0]}
    target = "continue" if kind == "resume" else kind
    key = "p0-operator/" + rid + "/" + mid
    prior = ctx.conn.execute("SELECT 1 FROM idempotency WHERE idempotency_key=?",
                             (store.tool_key("gx_run_control", key),)).fetchone()
    settled = ((kind == "resume" and run["status"] != "PAUSED") or
               (kind == "pause" and run["status"] == "PAUSED") or
               (kind == "cancel" and run["status"] == "CANCELLED"))
    if settled and not prior:
        return {"run_id": rid, "status": run["status"], "unchanged": True}
    if kind == "resume" and not prior:
        prepare_replan(ctx.conn, rid)
    return service.run_control(ctx, {"run_id": rid, "action": target, "reason": action.get("reason", "Operator " + kind),
                                     "idempotency_key": key}, defer_advance=True)


def _read(ctx, run, kind, mid):
    from . import metering, operator_budget
    from .driver import snapshot
    conn, rid = ctx.conn, run["run_id"]
    base = {"run_id": rid, "status": run["status"]}
    if kind in ("inspect", "status"):
        result = snapshot(conn, rid)
        # Read receipts can contain complete views. Keep only delivery identity in nested history.
        result["messages"] = [{"message_id": row["message_id"], "rejected": row["rejected"]} for row in result["messages"]]
        return result
    if kind == "usage":
        return {**base, "run_budget": budgets.state(conn, run["budget_ref"]), "metering": metering.inspect(conn, rid),
                "project_limits": intent_budget.current_caps(conn, rid)}
    if kind == "pending":
        rows = conn.execute("""
            SELECT m.message_id,json_extract(m.action_json,'$.kind') AS kind,m.created_at,h.receipt_turn_id,
                   CASE WHEN h.received_at IS NOT NULL THEN 'interpreting'
                        WHEN d.state='sending' THEN 'awaiting_native_receipt' ELSE 'queued' END AS phase
            FROM p0_operator_messages m LEFT JOIN p0_human_inputs h ON h.message_id=m.message_id
            LEFT JOIN p0_root_deliveries d ON d.run_id=m.run_id AND d.event_id='human:' || m.message_id
            WHERE m.run_id=? AND m.message_id<>? AND m.rejected IS NULL AND
                  (m.result_json IS NULL OR (h.received_at IS NOT NULL AND h.consumed_at IS NULL))
            ORDER BY m.rowid""", (rid, mid))
        return {**base, "messages": [dict(row) for row in rows],
                "pending_previews": intent.pending_previews(conn, rid) + operator_budget.pending(conn, rid),
                "attempt_waits": waits.pending(conn, rid)}
    events = [{**dict(row), "payload": json.loads(row["payload_json"])} for row in
              conn.execute("SELECT * FROM events WHERE run_id=? ORDER BY event_id", (rid,))]
    for event in events:
        del event["payload_json"]
    return {**base, "events": events}


def add_commands(commands):
    from pathlib import Path
    parser = commands.add_parser("operator", help="Inspect and manage a run locally without a model turn")
    parser.add_argument("operation", choices=sorted(op.replace("_", "-") for op in KINDS))
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--message-id", help="Stable ID for retrying this exact command")
    parser.add_argument("--reason")
    parser.add_argument("--resource", help="Budget resource, such as tokens or api_calls")
    parser.add_argument("--limit", type=float)
    parser.add_argument("--budget-scope", choices=("run", "project"))
    parser.add_argument("--preview-id", help="preview_id of a run allocation or change_id of a project limit")
    parser.add_argument("--preview-sha256")
    parser.add_argument("--acknowledge-consequences", choices=(intent.IMPACT_ACK, intent.CANCEL_ACK))
    parser.add_argument("--output", type=Path, help="Write the audit report to this JSON file")
    from . import operator_usage
    parser.add_argument("--reservation-ref")
    parser.add_argument("--decision", choices=operator_usage.DECISIONS)
    parser.add_argument("--source", choices=operator_usage.SOURCES)
    parser.add_argument("--thread-id")
    parser.add_argument("--turn-id")
    parser.add_argument("--response-id")
    parser.add_argument("--acceptance-event-id",type=int)
    parser.add_argument("--approve-release",action="store_const",const=True,help="Release the explicitly accepted exposure")


def cli_action(args):
    kind = args.operation.replace("-", "_")
    if args.output and kind != "audit":
        raise SchemaInvalid("--output belongs to the audit command")
    action = {"kind": kind}
    for field in ("reason", "resource", "limit", "budget_scope", "preview_id", "preview_sha256",
                  "acknowledge_consequences","reservation_ref","decision","source","thread_id","turn_id",
                  "response_id","acceptance_event_id","approve_release"):
        value = getattr(args, field)
        if value is not None:
            action[field] = int(value) if field == "limit" and value.is_integer() else value
    return action
