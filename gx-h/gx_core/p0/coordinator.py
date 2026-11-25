"""Interleave durable P0 runs while the process owns the workspace native lock."""
from __future__ import annotations

import time

from ..domain import machines
from ..errors import GxError, OutcomeUnknown
from ..hostobs.app_server_client import AppServerDisconnected, AppServerTimeout
from ..persistence import store
from .driver import Driver, snapshot
from .idle import WorkspaceWait
from ..orchestration import run_mode


TRANSIENT = (AppServerDisconnected, AppServerTimeout, OutcomeUnknown)
START_TOPIC = "p0.workspace.start"


def _backoff(attempt):
    return min(5, 0.2 * 2 ** min(attempt, 5))


def _start_key(run_key, run_id=None):
    if run_key is None:
        return "p0.workspace.recover:" + run_id
    return START_TOPIC + ":" + run_key


def queue_start(conn, run_key, spec):
    """A second CLI can request a start while the workspace driver owns native_state."""
    from . import workspace
    if not isinstance(run_key, str) or not run_key.strip():
        raise ValueError("Starting another run in an active workspace needs --run-key")
    key = _start_key(run_key)
    with store.transaction(conn):
        previous = conn.execute("SELECT * FROM outbox WHERE idempotency_key=?", (key,)).fetchone()
        if previous and store.loads(previous, "payload_json")["spec"] != spec:
            raise ValueError("This run key already names a different specification")
        if previous and not previous["consumed_at"]:
            return {"status": "queued_for_active_driver", "run_key": run_key, "outbox_id": previous["outbox_id"],
                    **({"last_error": previous["last_error"], "attempts": previous["attempts"]}
                       if previous["last_error"] else {})}
        if previous and previous["last_error"]:
            raise ValueError(previous["last_error"])
        run = workspace.find_run(conn, run_key=run_key)
        if run:
            if store.loads(run, "inputs_json")["inputs"]["p0_spec"] != spec:
                raise ValueError("This run key already names a different specification")
            return snapshot(conn, run["run_id"])
        oid = store.enqueue_outbox(conn, topic=START_TOPIC,
                                   payload={"run_key":run_key, "spec":spec}, idempotency_key=key)
        return {"status":"queued_for_active_driver", "run_key":run_key, "outbox_id":oid}


def drain_starts(conn, directory, retry_at=None):
    from .cli import start
    from .workspace import find_run
    if retry_at is None:
        retry_at = {}
    for row in conn.execute("SELECT * FROM outbox WHERE topic='p0.workspace.start' "
                            "AND consumed_at IS NULL ORDER BY outbox_id").fetchall():
        oid = row["outbox_id"]
        if time.monotonic() < retry_at.get(oid, 0):
            continue
        data = store.loads(row, "payload_json")
        try:
            rid = data.get("run_id")
            if rid is None:
                existing = find_run(conn, run_key=data["run_key"])
                rid = existing["run_id"] if existing else None
            if rid is None:
                start(conn, directory, data["spec"], run_key=data["run_key"])
            else:
                start(conn, directory, data["spec"], run_id=rid)
        except TRANSIENT as exc:
            with store.transaction(conn):
                store.fail_outbox(conn, oid, f"{type(exc).__name__}: {exc}"[:500])
            retry_at[oid] = time.monotonic() + _backoff(row["attempts"] + 1)
            continue
        except (ValueError, GxError) as exc:
            with store.transaction(conn):
                store.fail_outbox(conn, oid, str(exc)[:500])
                store.complete_outbox(conn, oid, "gx-p0-start")
        else:
            with store.transaction(conn):
                conn.execute("UPDATE outbox SET last_error=NULL WHERE outbox_id=? AND last_error IS NOT NULL", (oid,))
                store.complete_outbox(conn, oid, "gx-p0-start")
        retry_at.pop(oid, None)


def _recover_partial_start(conn, run):
    """Join a partial creation to its original start key. True after a definitive refusal."""
    key = _start_key(run["run_key"], run["run_id"])
    existing = conn.execute("SELECT consumed_at,last_error FROM outbox WHERE idempotency_key=?", (key,)).fetchone()
    if existing:
        return bool(existing["consumed_at"] and existing["last_error"])
    with store.transaction(conn):
        spec = store.loads(run, "inputs_json")["inputs"]["p0_spec"]
        store.enqueue_outbox(conn, topic=START_TOPIC, idempotency_key=key,
                             payload={"run_id":run["run_id"], "run_key":run["run_key"], "spec":spec})
    return False


def _pending_start(conn):
    return conn.execute("SELECT 1 FROM outbox WHERE topic=? AND consumed_at IS NULL LIMIT 1", (START_TOPIC,)).fetchone()


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
    with WorkspaceWait(conn) as listener:
        return _drive_workspace(conn, directory, timeout, listener)


def _drive_workspace(conn, directory, timeout, listener):
    from .workspace import list_runs
    deadline = time.monotonic() + timeout
    drivers, finished, retry_at, failures = {}, {}, {}, {}
    plugin_waits = {}
    start_retry_at = {}
    try:
        while time.monotonic() < deadline:
            drain_starts(conn, directory, start_retry_at)
            runs = list_runs(conn)
            if not runs and not _pending_start(conn):
                result = overview(conn)
                if result.get("workspace_requests"):
                    return result
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
                    if run_mode.for_run(run).needs_bootstrap(conn, rid):
                        if _recover_partial_start(conn, run):
                            finished[rid] = (run["status"], _inbox_version(conn, rid))
                        continue
                    driver = Driver(conn, directory, rid)
                    drivers[rid] = (driver, driver.passes(deadline, shared=True))
                driver, passes = drivers[rid]
                inbox = _inbox_version(conn, rid)
                try:
                    next(passes)
                    retry_at[rid] = driver.wake_at
                    if driver.made_progress:
                        # A committed result can open another run's workspace/provider slot.
                        for sibling, (other, _) in drivers.items():
                            if sibling != rid:
                                other.wake()
                                retry_at.pop(sibling, None)
                except StopIteration as end:
                    driver.close()
                    del drivers[rid]
                    retry_at.pop(rid, None)
                    if end.value and end.value.get("driver") != "time_limit":
                        finished[rid] = (end.value["status"], inbox)
                        if end.value.get("driver") == "plugin_unavailable":
                            plugin_waits[rid] = end.value["reason"]
                        else:
                            plugin_waits.pop(rid, None)
                        if end.value["status"] in machines.terminal_states("run"):
                            from .workspace import release_finished
                            release_finished(conn)
                except TRANSIENT as exc:
                    drivers.pop(rid)
                    driver.close()
                    failures[rid] = failures.get(rid, 0) + 1
                    with store.transaction(conn):
                        store.append_event(conn, kind="p0.transport_recovery", run_id=rid, actor="gx-p0",
                            subject_kind="run", subject_id=rid, reason=type(exc).__name__,
                            evidence_level="EXECUTION_OBSERVED", payload={"attempt":failures[rid]})
                    retry_at[rid] = time.monotonic() + _backoff(failures[rid])
            if (not drivers and all(finished.get(r["run_id"]) == (r["status"], _inbox_version(conn, r["run_id"]))
                                    for r in list_runs(conn)) and not _pending_start(conn)):
                from .workspace import release_finished
                release_finished(conn)
                return {**overview(conn), **({"plugin_waits": plugin_waits} if plugin_waits else {})}
            now = time.monotonic()
            due = [deadline, *retry_at.values()]
            due.extend(retry_at.get(rid, now) for rid in drivers)
            for row in conn.execute("SELECT outbox_id FROM outbox WHERE topic=? AND consumed_at IS NULL", (START_TOPIC,)):
                due.append(start_retry_at.get(row[0], now))
            ready = listener.wait(min(due) - time.monotonic(), (driver for driver, _ in drivers.values()))
            for rid in drivers if ready is None else ready:
                drivers[rid][0].wake()
                retry_at.pop(rid, None)
        result = overview(conn)
        if plugin_waits:
            result["plugin_waits"] = plugin_waits
        result["driver"] = "time_limit"
        return result
    finally:
        for driver, _ in drivers.values():
            driver.close()
