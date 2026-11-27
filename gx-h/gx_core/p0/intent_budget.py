"""Read prospective intent limits against the same cumulative project ledger used at admission."""
import json

from .. import budgets
from ..budgets.lines import clean, line
from ..errors import SchemaInvalid, StaleRevision
from ..persistence import store


def limits_report(conn, run_id, items):
    caps = [(key, value) for key, value in items if value is not None]
    if not caps:
        return []
    project = conn.execute("SELECT project_ref FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
    spent, held = budgets._project_usage(conn, project)
    rows = []
    for key, cap in caps:
        resource, limit = cap["resource"], cap["max"]
        used, reserved = spent[resource], held[resource]
        balance = line(limit, used, reserved)["available"]
        if limit < used:
            state = "already_over_new_limit"
            message = "已超新上限。此资源停止新增支出，原支出如实保留，在途工作继续结算。"
        elif balance < 0:
            state = "holds_exceed_new_limit"
            message = "在途预留占满新额度。暂停此资源的新预留，让已获准的工作继续并按实结算。"
        elif balance == 0:
            state = "fully_allocated"
            message = "此资源的额度已全部分配。已有工作继续结算。"
        else:
            state = "within_new_limit"
            message = "新上限用于后续准入，已有工作继续结算。"
        row = {"item_id": key, "resource": resource, "unit": cap.get("unit", resource),
                     "limit": limit, "spent": used, "held": reserved, "available_for_new": max(0, balance),
                     "availability_scope": "this project intent limit",
                     "spent_over_limit": max(0, clean(used - limit)), "state": state, "message": message}
        if resource == "tokens":
            row["metering"] = "GX limits new token reservations. Reported actual usage can exceed a reservation."
        elif resource == "api_calls":
            row["metering"] = "GX counts admitted calls. A native turn may issue multiple provider requests."
        rows.append(row)
    return rows


def current_caps(conn, run_id):
    from . import intent
    items = intent.active_items(conn, run_id, "resource_limit")
    return limits_report(conn, run_id, [(key, {**item["value"], "unit": item["effective_unit"]})
                                        for key, item in items.items()])


def cancellation_preview(conn, run_id, job_ids):
    """A human may name Jobs to stop; an intent budget edit alone never selects one."""
    effects = []
    for job_id in sorted(job_ids):
        job = conn.execute(
            "SELECT j.status,j.capability_id,j.capability_version,j.task_id,j.path_id,j.reservation_ref,"
            "b.amounts_json,b.status AS reservation_status FROM jobs j LEFT JOIN budget_reservations b "
            "ON b.reservation_ref=j.reservation_ref WHERE j.run_id=? AND j.job_id=?",
            (run_id, job_id)).fetchone()
        if job is None:
            raise SchemaInvalid(f"Job {job_id} does not belong to this run")
        if job["status"] in ("SUCCEEDED", "FAILED", "CANCELLED", "CANCEL_REQUESTED"):
            raise StaleRevision(f"Job {job_id} is already {job['status']}. Review its current outcome.")
        effects.append({"object_type": "job", "object_id": job_id, "action": "request_cancel",
                        "status": job["status"], "method": f"{job['capability_id']}@{job['capability_version']}",
                        "path_id": job["path_id"], "task_id": job["task_id"],
                        "reservation_ref": job["reservation_ref"],
                        "held": json.loads(job["amounts_json"]) if job["reservation_status"] == "HELD" else {},
                        "consequence": "Cancellation may prevent this Job from producing its pending result. "
                                       "Work already performed remains chargeable. GX retains completed evidence."})
    return effects


def enqueue_cancellations(conn, run_id, change_id, effects):
    for effect in effects:
        job_id = effect["object_id"]
        topic = "p0.intent.cancel_job"
        effect["outbox_id"] = store.enqueue_outbox(
            conn, topic=topic, idempotency_key=f"{topic}:{change_id}:{job_id}",
            payload={"run_id": run_id, "change_id": change_id, "job_id": job_id})
