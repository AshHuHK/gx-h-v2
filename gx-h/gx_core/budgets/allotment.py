"""Resize a run's existing funding hold inside its workspace budget."""
from . import FUND_PREFIX, HELD, _amounts, _available, _clean, _funding_state, _reservation, _row, _write
from ..domain import ids
from ..errors import BudgetExceeded, SchemaInvalid
from ..persistence import store


def set_run_limit(conn, run_id, budget_ref, *, key, limit, actor):
    checked = _amounts({key: limit}, what="run allocation")[key]
    with store.transaction(conn):
        child = _row(conn, budget_ref)
        funding = _reservation(conn, FUND_PREFIX + budget_ref)
        if (child["run_id"] != run_id or funding["subject_kind"] != "run" or funding["subject_id"] != run_id
                or funding["budget_ref"] != child["parent_ref"] or funding["status"] != HELD):
            raise SchemaInvalid("This run has no open workspace funding hold to resize")
        parent = _row(conn, child["parent_ref"])
        if parent["parent_ref"] and _funding_state(conn, parent["budget_ref"]) != HELD:
            raise BudgetExceeded("The parent allocation is closed")
        limits = store.loads(child, "limits_json", {})
        committed = store.loads(child, "spent_json", {}).get(key, 0) + store.loads(child, "reserved_json", {}).get(key, 0)
        if checked < committed:
            raise BudgetExceeded("The new allocation must cover this run's spent and held funds")
        held = store.loads(funding, "amounts_json")
        if key not in limits or key not in held:
            raise SchemaInvalid("The run has no funding line for this resource")
        delta = checked - held[key]
        reserved = store.loads(parent, "reserved_json", {})
        available = _available(store.loads(parent, "limits_json"), store.loads(parent, "spent_json", {}), reserved)
        if delta > 0 and delta > available.get(key, 0):
            raise BudgetExceeded("The workspace has insufficient unallocated funds for this increase",
                                 details={"resource": key, "additional": delta, "available": available.get(key, 0)})
        before = limits[key]
        limits[key] = held[key] = checked
        reserved[key] = _clean(reserved.get(key, 0) + delta)
        _write(conn, parent["budget_ref"], reserved=reserved)
        conn.execute("UPDATE budget_reservations SET amounts_json=? WHERE reservation_ref=?",
                     (ids.canonical_json(held), funding["reservation_ref"]))
        _write(conn, budget_ref, limits=limits)
        store.append_event(conn, kind="budget.allotment_changed", run_id=run_id, actor=actor,
                           subject_kind="budget", subject_id=budget_ref, evidence_level="EXECUTION_OBSERVED",
                           payload={"key": key, "from": before, "to": checked, "parent_ref": child["parent_ref"],
                                    "reservation_ref": funding["reservation_ref"], "funding_delta": delta})
