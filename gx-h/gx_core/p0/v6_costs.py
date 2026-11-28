"""Read the effective Run ledger and native measurement receipts in their original units."""
from ..persistence import store
from . import meter_limits, metering


UNITS = {"job_submissions": "submissions", "cpu_seconds": "CPU seconds",
         "gpu_jobs": "GPU jobs", "gpu_minutes": "GPU minutes", "tokens": "tokens",
         "paid_api_calls": "booked external call slots", "api_calls": "calls", "worker_tasks": "tasks", "wall_hours": "wall hours"}


def resource_report(conn, run_id, *, meter=None):
    """Keep measured CPU, booked consumption, unsettled exposure and money distinct."""
    conn.execute("SAVEPOINT v6_resource_report")
    try:
        row = conn.execute("SELECT b.budget_ref,b.limits_json FROM budgets b "
                           "JOIN runs r ON r.budget_ref=b.budget_ref WHERE r.run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"Run budget missing for {run_id}")
        limits = store.loads(row, "limits_json")
        _, spent, held = meter_limits.subtree_balances(conn, row["budget_ref"], limits)
        lines = {key: {"unit": UNITS.get(key, key), "limit": cap, "booked": spent[key],
                       "reserved": held[key], "remaining": cap - spent[key] - held[key]}
                 for key, cap in limits.items()}
        cpu = conn.execute("""
            SELECT count(*) AS jobs, count(cpu) AS measured_jobs, coalesce(sum(cpu),0) AS seconds,
                   count(*) FILTER (WHERE cpu IS NULL AND possibly_started) AS unmeasured
            FROM (
              SELECT CASE WHEN json_extract(measured_json,'$.estimated') IS NULL
                          THEN json_extract(measured_json,'$.cpu_seconds') END AS cpu,
                     coalesce(json_extract(measured_json,'$.started'),1) AS possibly_started
              FROM jobs WHERE run_id=?
            )
            """, (run_id,)).fetchone()
        usage = meter if meter is not None else metering.inspect(conn, run_id)
        receipts = usage["cost_report"]
        return {
            "budget_ref": row["budget_ref"], "lines": lines,
            "jobs_recorded": cpu["jobs"],
            "measured_cpu": {"unit": "CPU seconds", "observed": round(cpu["seconds"], 3),
                             "observed_jobs": cpu["measured_jobs"], "pending_or_unmeasured_jobs": cpu["unmeasured"]},
            "tokens": ({"unit": "tokens", "provider_attributed": receipts["PROVIDER_CONFIRMED"]["tokens"],
                        "provisional_booked": receipts["ESTIMATED"]["provisional_budget_tokens"],
                        "unattributed_delta": receipts["UNATTRIBUTED_DELTA"]["tokens"],
                        "unresolved": receipts["UNRESOLVED"]["reservation_refs"],
                        "possibly_understated": receipts["possibly_understated_estimate_refs"]}
                       if receipts is not None else None),
            # Native token receipts carry no tariff, subscription price or USD invoice.
            "provider_charge": {"unit": "USD", "amount": usage.get("provider_money_cost"),
                                "source": "provider_money_cost"},
        }
    finally:
        conn.execute("RELEASE v6_resource_report")
