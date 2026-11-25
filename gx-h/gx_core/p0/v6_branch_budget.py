"""Project branch use from the original Run ledger. A branch owns no second account."""
from collections import Counter
import json

from ..errors import HoldActive, ScopeDenied
from . import metering


def _native(conn, branch_id):
    """Sum admitted, booked and corrected native exposure of this branch's task roots."""
    rows = conn.execute("""SELECT d.*,x.known_api_calls,x.confirmation_event_id,
        coalesce((SELECT sum(a.delta) FROM p0_meter_adjustments a WHERE a.reservation_ref=d.reservation_ref),0)
        + coalesce((SELECT delta FROM p0_meter_receipt_adjustments a WHERE a.reservation_ref=d.reservation_ref),0)
        AS adjustment FROM v6_decision_tasks t JOIN p0_roots p USING(task_id)
        JOIN p0_meter_native_turns d ON p.thread_id=d.thread_id
        LEFT JOIN p0_usage_releases x ON d.reservation_ref=x.reservation_ref
        WHERE t.scope IN (?,?)""", (branch_id, "branch:" + branch_id))
    used = Counter()
    for row in rows:
        if row["budget_status"] == "HELD":
            # An unknown turn still holds funds; an observed overrun may already exceed them.
            lower = row["observed_lower_bound"] or 0
            if row["turn_id"]:
                _, observed, _, _ = metering.observation(conn, row, interval_floor=False)
                lower = max(lower, observed)
            used["tokens"] += max(row["token_budget"] or 0, lower)
            used["api_calls"] += 1
        elif row["budget_status"] == "RELEASED":
            # accept-exposure/release-hold can be followed by an exact late provider receipt.
            used["tokens"] += row["settled_tokens"] or 0
            used["api_calls"] += 1 if row["confirmation_event_id"] else row["known_api_calls"] or 0
        else:
            baseline = row["settled_tokens"] if row["settled_tokens"] is not None else row["token_budget"]
            used["tokens"] += (baseline or 0) + row["adjustment"]
            used["api_calls"] += 1
    return used


def exposure(conn, branch_id):
    from .v6_jobs import branch_exposure
    used = branch_exposure(conn, branch_id)
    used.update(_native(conn, branch_id))
    row = conn.execute("""SELECT coalesce(sum(json_extract(b.amounts_json,'$.worker_tasks')),0)
        FROM budget_reservations b JOIN v6_decision_tasks d ON d.task_id=b.subject_id
        WHERE b.subject_kind='task' AND b.status IN ('HELD','SETTLED') AND d.scope IN (?,?)""",
        (branch_id, "branch:" + branch_id)).fetchone()
    used["worker_tasks"] += row[0]
    return used


def available_tokens(conn, run_id, binding, available):
    """Inside RootDelivery's reservation transaction, only the current branch grant can tighten it."""
    from . import v6_delegation, v6_native
    if not v6_native.assigned(binding):
        return available
    task = conn.execute("SELECT scope FROM v6_decision_tasks WHERE run_id=? AND task_id=?",
                        (run_id, binding.get("task_id"))).fetchone()
    if not task:
        raise ScopeDenied("No scientific delegation owns the native reservation")
    if task["scope"] == "run":
        return available
    scope = task["scope"]
    if scope.startswith("module:"):
        from . import v62_budget
        return v62_budget.available_tokens(conn, run_id, scope, available)
    branch_id = scope.removeprefix("branch:")
    v6_delegation.require_grant(conn, run_id, binding["task_id"], scope)
    branch = conn.execute("SELECT spec_json,status FROM v6_branches WHERE branch_id=? AND run_id=?",
                          (branch_id, run_id)).fetchone()
    if not branch or branch["status"] != "OPEN":
        raise ScopeDenied("The branch is no longer admitted to start a native turn")
    limits = json.loads(branch["spec_json"])["budget_limits"]
    units = {"api_calls", "tokens"} & limits.keys()
    if not units:
        return available
    used = _native(conn, branch_id)
    for key in sorted(units):
        room = int(limits[key] - used[key])
        if room < 1:
            # This particular branch cannot fund another call. Other branches can proceed.
            raise HoldActive(f"Branch {branch_id} cannot admit another native turn under its {key} ceiling",
                             details={"reason": "budget_reserved", "scope": scope, "key": key,
                                      "limit": limits[key], "held_or_spent": used[key], "available": room})
        if key == "tokens":
            available = min(available, room)
    return available
