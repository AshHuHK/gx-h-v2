"""Interleave durable P0 runs while the process owns the workspace native lock."""
from __future__ import annotations

import time

from ..domain import machines
from ..errors import GxError, OutcomeUnknown
from ..hostobs.app_server_client import AppServerDisconnected, AppServerTimeout
from ..persistence import store
from .driver import Driver, snapshot


def queue_start(conn, run_key, spec):
    """A second CLI can request a start while the workspace driver owns native_state."""
    from . import workspace
    if not isinstance(run_key, str) or not run_key.strip():
        raise ValueError("Starting another run in an active workspace needs --run-key")
    key = "p0.workspace.start:" + run_key
    with store.transaction(conn):
        previous = conn.execute("SELECT * FROM outbox WHERE idempotency_key=?", (key,)).fetchone()
        if previous and store.loads(previous, "payload_json")["spec"] != spec:
            raise ValueError("This run key already names a different specification")
        run = workspace.find_run(conn, run_key=run_key)
        if run:
            if store.loads(run, "inputs_json")["inputs"]["p0_spec"] != spec:
                raise ValueError("This run key already names a different specification")
            return snapshot(conn, run["run_id"])
        if previous and previous["consumed_at"] and previous["last_error"]:
            raise ValueError(previous["last_error"])
        oid = store.enqueue_outbox(conn, topic="p0.workspace.start",
                                   payload={"run_key":run_key, "spec":spec}, idempotency_key=key)
        return {"status":"queued_for_active_driver", "run_key":run_key, "outbox_id":oid}


def drain_starts(conn, directory):
    from .cli import start
    for row in conn.execute("SELECT * FROM outbox WHERE topic='p0.workspace.start' "
                            "AND consumed_at IS NULL ORDER BY outbox_id").fetchall():
        data = store.loads(row, "payload_json")
        try:
            start(conn, directory, data["spec"], run_key=data["run_key"])
        except (AppServerDisconnected, AppServerTimeout, OutcomeUnknown):
            # The run start key reconciles a committed creation on the next pass.
            raise
        except (ValueError, GxError) as exc:
            with store.transaction(conn):
                store.fail_outbox(conn, row["outbox_id"], str(exc)[:500])
                store.complete_outbox(conn, row["outbox_id"], "gx-p0-start")
        else:
            with store.transaction(conn):
                store.complete_outbox(conn, row["outbox_id"], "gx-p0-start")


def overview(conn):
    from .workspace import budget_summary, list_runs
    runs = list_runs(conn)
    requests = [dict(row) for row in conn.execute(
        "SELECT outbox_id,json_extract(payload_json,'$.run_key') AS run_key, "
        "CASE WHEN consumed_at IS NULL THEN 'queued' ELSE 'rejected' END AS status,last_error "
        "FROM outbox WHERE topic='p0.workspace.start' AND (consumed_at IS NULL OR last_error IS NOT NULL) "
        "ORDER BY outbox_id")]
    details = {"workspace_requests": requests} if requests else {}
    allocation = budget_summary(conn)
    if allocation is not None:
        details["workspace_budget"] = allocation
    if not runs:
        return {"status":"not_started","runs":[], **details}
    if len(runs) == 1:
        return {**snapshot(conn, runs[0]["run_id"]), **details}
    rows = [snapshot(conn, r["run_id"]) for r in runs]
    states = {r["status"] for r in rows}
    if len(states) == 1:
        status = next(iter(states))
    elif all(s == "PAUSED" or s in machines.terminal_states("run") for s in states):
        status = "QUIESCENT"
    else:
        status = "RUNNING"
    return {"status":status, "project":runs[0]["project_ref"], "runs":rows, **details}


def _inbox_version(conn, rid):
    return conn.execute("SELECT MAX(rowid) FROM p0_operator_messages WHERE run_id=?", (rid,)).fetchone()[0]


def drive_workspace(conn, directory, timeout):
    from .cli import start
    from .workspace import list_runs
    deadline = time.monotonic() + timeout
    drivers, finished, retry_at, failures = {}, {}, {}, {}
    try:
        while time.monotonic() < deadline:
            drain_starts(conn, directory)
            runs = list_runs(conn)
            if not runs:
                raise ValueError("Start a project before using this command")
            for run in runs:
                rid = run["run_id"]
                if rid in finished:
                    if finished[rid] == (run["status"], _inbox_version(conn, rid)):
                        continue
                    del finished[rid]
                if time.monotonic() < retry_at.get(rid, 0):
                    continue
                if rid not in drivers:
                    if not store.latest_checkpoint(conn, rid):
                        from .workspace import key_for_run
                        start(conn, directory, store.loads(run, "inputs_json")["inputs"]["p0_spec"],
                              run_key=key_for_run(conn, rid))
                    driver = Driver(conn, directory, rid)
                    drivers[rid] = (driver, driver.passes(deadline, shared=True))
                driver, passes = drivers[rid]
                inbox = _inbox_version(conn, rid)
                try:
                    next(passes)
                except StopIteration as end:
                    driver.close()
                    del drivers[rid]
                    if end.value and end.value.get("driver") != "time_limit":
                        finished[rid] = (end.value["status"], inbox)
                        if end.value["status"] in machines.terminal_states("run"):
                            from .workspace import release_finished
                            release_finished(conn)
                except (AppServerDisconnected, AppServerTimeout, OutcomeUnknown) as exc:
                    drivers.pop(rid)
                    driver.close()
                    failures[rid] = failures.get(rid, 0) + 1
                    with store.transaction(conn):
                        store.append_event(conn, kind="p0.transport_recovery", run_id=rid, actor="gx-p0",
                            subject_kind="run", subject_id=rid, reason=type(exc).__name__,
                            evidence_level="EXECUTION_OBSERVED", payload={"attempt":failures[rid]})
                    retry_at[rid] = time.monotonic() + min(5, 0.2 * 2 ** min(failures[rid], 5))
            if not drivers and all(finished.get(r["run_id"]) == (r["status"], _inbox_version(conn, r["run_id"]))
                                   for r in list_runs(conn)):
                from .workspace import release_finished
                release_finished(conn)
                return overview(conn)
            time.sleep(min(0.035, max(0, deadline - time.monotonic())))
        result = overview(conn)
        result["driver"] = "time_limit"
        return result
    finally:
        for driver, _ in drivers.values():
            driver.close()
