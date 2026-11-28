"""Fund module work from the existing GX budget ledger and return unused allocation."""
from collections import Counter
import json

from .. import budgets
from ..errors import BudgetExceeded, HoldActive, ScopeDenied
from ..persistence import store
from . import meter_limits


def remaining(conn, budget_ref):
    row = conn.execute("SELECT limits_json,spent_json,reserved_json FROM budgets WHERE budget_ref=?",
                       (budget_ref,)).fetchone()
    if not row:
        raise ScopeDenied("Module funding no longer exists")
    return budgets._available(*(json.loads(value) for value in row))


def owning_module(conn, run_id, branch_id):
    return conn.execute("SELECT * FROM v62_modules WHERE run_id=? AND branch_id=?",
                        (run_id, branch_id)).fetchone()


def open_child(conn, run_id, instance_id, limits, actor):
    """Called in the module-opening transaction, before a module can receive a task."""
    allocation = budgets.check_amounts(limits, what="module allowance")
    parent = budgets.budget_for(conn, run_id=run_id)
    promises = _branch_promises(conn, run_id)
    room, earmarked = _unallocated(conn, run_id, parent, allocation)
    shortfalls = []
    for key, amount in allocation.items():
        available = room.get(key, 0) - promises[key]
        if amount and amount > available:
            shortfalls.append({"key": key, "requested": amount, "available": available,
                               "short_by": amount - available, "outstanding_branch_allowances": promises[key]})
    if shortfalls:
        raise BudgetExceeded("Unallocated Run budget cannot fund this module", details={
            **shortfalls[0], "budget_ref": parent, "shortfalls": shortfalls,
            "held_reservations": budgets.held_details(conn, parent, {line["key"] for line in shortfalls})})
    requested = {k: v + promises[k] + earmarked[k] for k, v in allocation.items() if v}
    for error in budgets._project_reservation_errors(conn, parent, requested):
        raise error
    return budgets.open_child(conn, parent_ref=parent, subject_kind="research_module",
                              subject_id=instance_id, limits=allocation, actor=actor)["budget_ref"]


def _branch_promises(conn, run_id):
    # Older Goal branches carry a ceiling on the parent's ledger. Module allocations
    # have real child accounts whose entire caps are already reserved in the parent.
    from . import v6_branch_budget
    promises = Counter()
    for branch in conn.execute("""SELECT b.branch_id,b.spec_json FROM v6_branches b
            WHERE b.run_id=? AND b.status='OPEN' AND NOT EXISTS
            (SELECT 1 FROM v62_modules m WHERE m.run_id=b.run_id AND m.branch_id=b.branch_id)""", (run_id,)):
        used = v6_branch_budget.exposure(conn, branch["branch_id"])
        ceilings = json.loads(branch["spec_json"])["budget_limits"]
        promises.update({key: max(0, limit - used[key]) for key, limit in ceilings.items()})
    return promises


def _unallocated(conn, run_id, parent, allocation):
    # Funding holds promise unused child units, while the workspace risk ledger
    # sees actual booked/held/observed consumption. Derive the parent room after
    # both, including overruns that have not yet rolled up from open children.
    room = remaining(conn, parent)
    earmarked = Counter()
    for row in conn.execute("""SELECT b.budget_ref,f.amounts_json FROM budgets b
            JOIN budget_reservations f ON f.reservation_ref=? || b.budget_ref AND f.status='HELD'
            WHERE b.parent_ref=?""", (budgets.FUND_PREFIX, parent)):
        ref, funding = row[0], json.loads(row[1])
        _, spent, reserved = meter_limits.subtree_balances(conn, ref, allocation)
        used = {key: spent[key] + reserved[key] for key in allocation}
        if allocation.get("tokens"):
            used["tokens"] = next(meter_limits.scopes(conn, run_id, ref))["committed_tokens"]
        for key, amount in used.items():
            promised = funding.get(key, 0)
            earmarked[key] += max(0, promised - amount)
            room[key] = room.get(key, 0) - max(0, amount - promised)
    if allocation.get("tokens"):
        for line in meter_limits.scopes(conn, run_id, parent):
            if line["available_tokens"] is not None:
                room["tokens"] = min(room.get("tokens", 0), line["available_tokens"] - earmarked["tokens"])
    return room, earmarked


def close_if_settled(conn, budget_ref):
    """Return unused units only after the real Job and native reservations have settled."""
    if conn.execute("SELECT 1 FROM budget_reservations WHERE budget_ref=? AND status='HELD' LIMIT 1",
                    (budget_ref,)).fetchone():
        return False
    budgets.release(conn, budgets.FUND_PREFIX + budget_ref, actor="gx:module",
                    reason="Module closed and its original reservations settled")
    return True


def release_finished(conn, run_id):
    """Roll up final turn and Job charges even after the scientific Run has closed."""
    rows = conn.execute("""SELECT m.budget_ref FROM v62_modules m JOIN budget_reservations f
            ON f.reservation_ref=? || m.budget_ref
            WHERE m.run_id=? AND m.status!='OPEN' AND f.status='HELD' AND NOT EXISTS
            (SELECT 1 FROM budget_reservations r WHERE r.budget_ref=m.budget_ref AND r.status='HELD')""",
            (budgets.FUND_PREFIX, run_id)).fetchall()
    if not rows:
        return False
    with store.transaction(conn, transaction_owner="ModuleBudget", operation="return_module_balance",
                           run_id=run_id) as tx:
        changed = False
        for row in rows:
            changed = close_if_settled(tx, row["budget_ref"]) or changed
        return changed


def _for_scope(conn, run_id, scope):
    module = conn.execute("SELECT * FROM v62_modules WHERE run_id=? AND scope=?", (run_id, scope)).fetchone()
    if not module:
        raise ScopeDenied("No funded module owns this scope")
    return module


def _open_ref(module):
    if module["status"] != "OPEN":
        raise ScopeDenied("Module is no longer accepting work", details={"scope": module["scope"]})
    return module["budget_ref"]


def view_for_scope(conn, run_id, scope):
    module = _for_scope(conn, run_id, scope)
    account = budgets.state(conn, module["budget_ref"])
    account["native_exposure"] = next(meter_limits.scopes(conn, run_id, module["budget_ref"]))
    if "tokens" not in account["limits"]:
        account["token_funding"] = "shared_run"
        account["available"]["tokens"] = _shared_token_room(conn, run_id)
    return account


def scope_for_task(conn, run_id, task_id):
    row = conn.execute("SELECT scope FROM v6_decision_tasks WHERE run_id=? AND task_id=?",
                       (run_id, task_id)).fetchone()
    return row[0] if row else None


def native_funding(conn, run_id, task_id, fallback):
    scope = scope_for_task(conn, run_id, task_id)
    return _open_ref(_for_scope(conn, run_id, scope)) if scope and scope.startswith("module:") else fallback


def job_funding(conn, request, fallback):
    node_id = (request.get("binding") or {}).get("v6_node_id")
    if not node_id:
        return fallback
    module = conn.execute("""SELECT m.* FROM v62_modules m JOIN v6_nodes n
          ON m.run_id=n.run_id AND m.branch_id=n.branch_id WHERE n.node_id=? AND n.run_id=?""",
          (node_id, request["run_id"])).fetchone()
    return _open_ref(module) if module else fallback


def _shared_token_room(conn, run_id):
    parent = budgets.budget_for(conn, run_id=run_id)
    unallocated, _ = _unallocated(conn, run_id, parent, {"tokens": 1})
    return max(0, unallocated.get("tokens", 0) - _branch_promises(conn, run_id)["tokens"])


def available_tokens(conn, run_id, scope, available):
    ref = _open_ref(_for_scope(conn, run_id, scope))
    room = remaining(conn, ref)
    if "tokens" not in room:
        room["tokens"] = _shared_token_room(conn, run_id)
    for key in ("tokens", "api_calls"):
        if room.get(key, 0) < 1:
            raise HoldActive("Module cannot fund another native decision",
                             details={"reason": "budget_reserved", "scope": scope, "key": key,
                                      "budget_ref": ref, "available": room.get(key, 0)})
    return min(available, int(room["tokens"]))


def exhausted(conn, module):
    """Supply a cost-free GX handback when a new call cannot be funded after admitted work."""
    rid, ref = module["run_id"], module["budget_ref"]
    if conn.execute("SELECT 1 FROM budget_reservations WHERE budget_ref=? AND status='HELD' LIMIT 1",
                    (ref,)).fetchone():
        return None
    if conn.execute("""SELECT 1 FROM v6_nodes WHERE run_id=? AND branch_id=?
            AND status IN ('PENDING','RUNNING','CANCEL_REQUESTED') LIMIT 1""",
            (rid, module["branch_id"])).fetchone():
        return None
    room = remaining(conn, ref)
    for key in ("api_calls", "tokens"):
        if key == "tokens" and key not in room:
            continue
        if room.get(key, 0) < 1:
            return f"module {key} allowance exhausted"
    for line in meter_limits.scopes(conn, rid, ref):
        if line["hard_tokens"] is not None and line["settled_tokens"] >= line["hard_tokens"]:
            return "settled usage exhausted the " + line["scope"] + " token ceiling"
        # A terminated thread can later disclose usage outside its settled turns.
        # When that local exposure consumes all headroom, admission cannot resume.
        # Return the work already done; keep the charged and unattributed amounts distinct.
        if (line["scope"] == ref and line["hard_tokens"] is not None
                and line["active_root_count"] == 0 and line["reserved_tokens"] == 0
                and line["unfunded_closed_thread_tokens"] > 0
                and line["settled_tokens"] + line["unfunded_closed_thread_tokens"] >= line["hard_tokens"]):
            return "closed-thread usage exposure prevents another turn within the module token ceiling"
    for error in budgets._project_reservation_errors(conn, ref, {"api_calls": 1, "tokens": 1}):
        for line in error.details.get("shortfalls", [error.details]):
            if line.get("limit") is not None and line["limit"] - line["spent"] < 1:
                return "settled project usage exhausted " + line["key"]
    return None
