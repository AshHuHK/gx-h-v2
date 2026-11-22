"""How the jobs of a run ended, path by path: a read for a scheduler's snapshot.

SCHEDULING_DESIGN r2.2 §6.2: a failure of class ``resource`` is routed WAIT while the path's
reservation holds and the job could ever fit, and "a WAIT or a not_started outcome counts per
path and uses no attempt", against ``run_budget.resource_waits_per_path``. A scheduler that
decides this needs to know, for each path, how its jobs ended and how many of those endings
were waits for the device; and it has to learn it without looking at a runner or settling
anything, because its snapshot is one read transaction.

    by_path(conn, run_id) -> {"run_id", "paths": {path_id: summary}, "without_path": summary}

    summary = {"jobs": n, "open": n, "succeeded": n, "failed": n, "cancelled": n,
               "by_failure_class": {class: n},          # of the failed
               "not_started": {reason: n},              # failed, and the tool never ran, by not_started_because
               "resource_waits": n,                     # of those: the device was busy or had no room
               "listed": [{job_id, task_id, status, terminal, observed, failure_class,
                           not_started_because, resource_wait, finished_at}]}

**A pure read of one table.** One SELECT over the run's rows of ``jobs``, as recorded; no
runner file, no ``/proc``, no clock and no limits file is read, so the answer is a function of
the store alone and a snapshot may take it inside its one read transaction. A job whose
runner has written its outcome and that nobody has looked at since is still ``open`` here:
whoever decides on a run calls ``JobService.reconcile(run_id)`` first (the lazy path), and
that is what makes these rows current. Whether an open job is past its deadline needs the
limits and an instant: ask ``leases.job_deadline(row, limits=…, at=…)`` for that.

**What a resource wait is.** A job that FAILED without its tool ever starting, in class
``resource``, because the device was not to be had: the lease's queue ran out
(``lease_timeout``), the device had no room when the job reached it (``preflight_refused``,
or ``gpu_preflight`` when the service itself read it), or an admitted job found no start
within its deadline (``start_deadline``). Not a wait: an operator's switch or hold
(``switch``, ``refused``), a person's refusal (``approval_refused``), a closed network
route. Those need a person, and waiting again does not clear them.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from ..persistence import store

#: ``failure.not_started_because`` values that say the device was not to be had this time.
RESOURCE_WAIT_REASONS = ("lease_timeout", "preflight_refused", "gpu_preflight", "start_deadline")

_TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")


def is_resource_wait(status: str, failure: Mapping[str, Any] | None) -> bool:
    """Whether a job's ending is a wait for the device that never got it (see the module)."""
    failure = failure or {}
    return (status == "FAILED" and failure.get("observed") == "not_started"
            and failure.get("failure_class") == "resource"
            and failure.get("not_started_because") in RESOURCE_WAIT_REASONS)


def _empty() -> dict[str, Any]:
    return {"jobs": 0, "open": 0, "succeeded": 0, "failed": 0, "cancelled": 0, "by_failure_class": {},
            "not_started": {}, "resource_waits": 0, "listed": []}


def by_path(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """How the jobs of ``run_id`` ended, grouped by the path each was run for."""
    rows = conn.execute(
        "SELECT job_id, path_id, task_id, status, outcome_json, finished_at FROM jobs WHERE run_id=?"
        " ORDER BY rowid", (run_id,)).fetchall()
    paths: dict[str, dict[str, Any]] = {}
    without = _empty()
    for row in rows:
        summary = paths.setdefault(row["path_id"], _empty()) if row["path_id"] else without
        status = row["status"]
        failure = (store.loads(row, "outcome_json", {}) or {}).get("failure") or {}
        wait = is_resource_wait(status, failure)
        summary["jobs"] += 1
        if status not in _TERMINAL:
            summary["open"] += 1
        else:
            summary[status.lower()] += 1
        if status == "FAILED":
            name = failure.get("failure_class") or "unclassified"
            summary["by_failure_class"][name] = summary["by_failure_class"].get(name, 0) + 1
            if failure.get("observed") == "not_started":
                reason = failure.get("not_started_because") or "unstated"
                summary["not_started"][reason] = summary["not_started"].get(reason, 0) + 1
        if wait:
            summary["resource_waits"] += 1
        summary["listed"].append({
            "job_id": row["job_id"], "task_id": row["task_id"], "status": status,
            "terminal": status in _TERMINAL, "observed": failure.get("observed"),
            "failure_class": failure.get("failure_class"),
            "not_started_because": failure.get("not_started_because"), "resource_wait": wait,
            "finished_at": row["finished_at"],
        })
    return {"run_id": run_id, "paths": paths, "without_path": without}


__all__ = ["RESOURCE_WAIT_REASONS", "by_path", "is_resource_wait"]
