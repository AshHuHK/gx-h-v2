"""Inject lost inbound frames at the real GX Roots client/official Codex boundary."""

import json
import os
import time
from unittest.mock import patch

from gx_core import budgets
from gx_core.p0 import schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store


class ReceiptLost(OSError):
    """The fault controller takes the production client's reply away."""


def snapshot(conn, result, name):
    tables = (
        "p0_roots",
        "p0_root_creations",
        "p0_root_deliveries",
        "p0_root_turn_usage",
        "budget_reservations",
        "tasks",
        "leases",
        "p0_root_calls",
        "jobs",
    )
    view = {}
    for table in tables:
        view[table] = [
            {k: r[k] for k in r.keys() if k not in ("brief", "input_json", "acceptance_json")}
            for r in conn.execute("SELECT * FROM " + table)
        ]
    result.setdefault("sqlite", {})[name] = view
    return view


def run(p, result, case):
    db = p.directory / "gx.sqlite3"
    conn = store.open_store(db)
    schema.install(conn)
    with store.transaction(conn) as tx:
        budget = budgets.create_root(tx, run_id=None, profile_name="p0", actor="probe")
        rid = records.create_run(
            tx,
            objective="Exercise native session recovery",
            project_ref="native-probe",
            actor="probe",
            budget_ref=budget,
        )
        for status in ("AUDITING", "PLANNING", "ACTIVE"):
            store.transition(tx, machine="run", subject_id=rid, to_state=status, actor="probe")
        task = records.create_task(
            tx, run_id=rid, kind="research_round", role="evidence_reviewer", actor="probe"
        )
        store.transition(tx, machine="task", subject_id=task, to_state="READY", actor="probe")
    result.update(run=rid, task=task)
    argv = [str(p.binary), "app-server", "--listen", "stdio://"]
    knobs = {
        "GX_P0_TEST": "1",
        "GX_P0_TEST_PROVIDER": p.provider,
        "GX_P0_MODEL": "gpt-6.1-sol",
        "GX_P0_CODEX": str(p.binary),
        "CODEX_HOME": str(p.home),
    }
    row = lambda: dict(conn.execute("SELECT * FROM p0_roots ORDER BY rowid DESC LIMIT 1").fetchone())

    def connect():
        return Roots(conn, run_id=rid, cwd=p.cwd, argv=argv, env={**p.env, **knobs}, token_budget=2000)

    def kill(root):
        proc = root.client.transport.process
        proc.kill()
        p.record(direction="fault", method="SIGKILL", pid=proc.pid, exit=proc.wait(5))

    def observe(root, drop=False):
        original = root.client.on_notification

        def listener(frame):
            p.notification(frame)
            if not drop:
                original(frame)

        root.client.on_notification = listener

    def request_trace(root, intercept=None):
        req = root._request

        def send(method, params):
            p.record(direction="request", method=method, thread=params.get("threadId"), keys=list(params))
            response = req(method, params)
            if isinstance(response, dict):
                p.record(
                    direction="result",
                    method=method,
                    requested_goal=params.get("status"),
                    returned_goal=(response.get("goal") or {}).get("status"),
                    thread_status=(response.get("thread") or {}).get("status"),
                    listed_turns=[(v.get("id"), v.get("status")) for v in response.get("data") or []],
                )
            if intercept:
                intercept(method, response)
            return response

        root._request = send

    def completion(root, tid, turn):
        msg = root.client.run_until(
            lambda f: f.message.get("method") == "turn/completed"
            and f.message["params"]["threadId"] == tid
            and f.message["params"]["turn"]["id"] == turn,
            timeout=12,
        )
        return msg.message["params"]["turn"]

    try:
        with patch.dict(os.environ, knobs):
            if case == "gx_create_loss":
                with connect() as root:
                    observe(root, drop=True)

                    def cut(method, response):
                        if method == "thread/start":
                            result["lost_ack_oracle_thread"] = response["thread"]["id"]
                            kill(root)
                            raise ReceiptLost

                    request_trace(root, cut)
                    try:
                        root.ensure_main()
                    except ReceiptLost:
                        pass
                    else:
                        raise AssertionError("creation fault was not reached")
                pre = snapshot(conn, result, "before_recovery")
                assert row()["thread_id"] is None and row()["state"] == "creating"
                assert not pre["p0_root_deliveries"] and not len(p.peer.rows)
                with connect() as root:
                    observe(root)
                    request_trace(root)
                    tid = root.ensure_main()
                    turn = root.human_turn(
                        "Single actual instruction after the original empty create", message_id="operator-1"
                    )
                    completion(root, tid, turn)
                post = snapshot(conn, result, "after_recovery")
                assert (
                    len(post["p0_roots"])
                    == len(post["p0_root_deliveries"])
                    == len(post["budget_reservations"])
                    == 1
                )
                assert len(p.peer.rows) == 1
                result.update(recovered_thread=tid, first_turn=turn)
            elif case == "gx_delivery_loss":
                with connect() as root:
                    tid = root.ensure_main()
                    observe(root, drop=True)

                    def cut(method, response):
                        if method == "turn/start":
                            turn = response["turn"]["id"]
                            result["lost_turn_ack_oracle"] = turn
                            completion(root, tid, turn)
                            kill(root)
                            raise ReceiptLost

                    request_trace(root, cut)
                    try:
                        root.human_turn(
                            "One recoverable official input", message_id="original-operator-message"
                        )
                    except ReceiptLost:
                        pass
                    else:
                        raise AssertionError("turn fault was not reached")
                pre = snapshot(conn, result, "after_lost_frames")
                assert pre["p0_root_deliveries"][0]["state"] == "sending" and len(p.peer.rows) == 1
                with connect() as root:
                    observe(root)
                    request_trace(root)
                    replay = root.human_turn(
                        "One recoverable official input", message_id="original-operator-message"
                    )
                    assert replay == result["lost_turn_ack_oracle"]
                    history = root._request(
                        "thread/turns/list", {"threadId": tid, "itemsView": "full", "limit": 10}
                    )["data"]
                    result["history_client_ids"] = [
                        it.get("clientId") for t in history for it in t["items"] if it["type"] == "userMessage"
                    ]
                    assert result["history_client_ids"] == ["original-operator-message"]
                    # A second replay is idempotent even on a restored live root.
                    assert (
                        root.human_turn(
                            "One recoverable official input", message_id="original-operator-message"
                        )
                        == replay
                    )
                post = snapshot(conn, result, "after_original_history_reconciled")
                assert post["p0_root_deliveries"][0]["state"] == "acknowledged" and len(p.peer.rows) == 1
                assert len(post["budget_reservations"]) == 1
                result.update(thread=tid, recovered_turn=replay)
            elif case == "gx_job_wait":
                from gx_core.capabilities.shipped import ensure_grid
                from gx_core.jobs.service import JobService, PURPOSE_WORKFLOW
                from gx_core.jobs.grid_workflow import observe_grid

                ensure_grid(JobService(conn).ctx)
                with connect() as root:
                    observe(root)
                    p.peer.gate.clear()
                    work = root.dispatch(task)
                    tid, turn = work["thread_id"], work["turn_id"]
                    jobs = JobService(conn, actor=tid)
                    grid = {
                        "fixture_version": "1",
                        "synthetic": True,
                        "method_id": "fixture-grid",
                        "method_version": "1",
                        "candidates": [{"candidate_id": "candidate", "x": 2}],
                        "delay_seconds": 2,
                    }
                    raw = jobs.artifacts.publish_bytes(
                        namespace=rid + "/inputs",
                        rel_path="grid.json",
                        kind="job_input",
                        data=json.dumps(grid),
                        run_id=rid,
                        media_type="application/json",
                    )
                    inputs = jobs.artifacts.make_input_manifest(
                        namespace=rid + "/grid/inputs",
                        run_id=rid,
                        entries=[{"role": "grid", "artifact_ref": raw.artifact_ref}],
                    )
                    job = jobs.submit(
                        run_id=rid,
                        task_id=task,
                        lease_ref=work["lease_id"],
                        purpose=PURPOSE_WORKFLOW,
                        capability_version="fixture-grid@1",
                        input_manifest_ref=inputs.artifact_ref,
                        parameters={"operation": "wait-probe"},
                        idempotency_key="wait-probe",
                        binding={"task_id": task, "attempt_id": work["root_key"]},
                    )
                    jid = job["job_id"]
                    root.wait_job(task, jid)
                    paused = root._request("thread/goal/get", {"threadId": tid})["goal"]
                    assert paused["status"] == "paused"
                    p.peer.gate.set()
                    completion(root, tid, turn)
                    count = len(p.peer.rows)
                    # Cut the native process while the detached Job is still running.
                    kill(root)
                snapshot(conn, result, "waiting")
                before = time.monotonic()
                deadline = before + 18
                while time.monotonic() < deadline:
                    finished = observe_grid(conn, jid)
                    if finished["status"] == "SUCCEEDED":
                        break
                    time.sleep(0.08)
                assert finished["status"] == "SUCCEEDED", finished["status"]
                assert len(p.peer.rows) == count
                with connect() as root:
                    observe(root)
                    request_trace(root)
                    p.peer.scenario("finish_goal")
                    p.peer.gate.clear()
                    receipt = root.wake_job(task, jid)
                    deadline = time.monotonic() + 5
                    while len(p.peer.rows) == count and time.monotonic() < deadline:
                        root.poll(0.03)
                    assert len(p.peer.rows) > count, "wake did not reach inference"
                    waking = root._request("thread/goal/get", {"threadId": tid})["goal"]
                    replay = root.wake_job(task, jid)
                    assert replay["turn_id"] == receipt["turn_id"]
                    p.peer.gate.set()
                    completion(root, tid, receipt["turn_id"])
                    from gx_core.errors import GxError

                    try:
                        late = root.wake_job(task, jid)
                    except GxError as exc:
                        late = {"error": str(exc)}
                    result.update(late_wake_after_task_lease_ended=late)
                snapshot(conn, result, "woken")
                result.update(
                    thread=tid,
                    initial_turn=turn,
                    job=jid,
                    backend_job=finished["scheduler_ref"],
                    waiting_goal=paused,
                    model_requests_while_waiting=0,
                    detached_wait_s=time.monotonic() - before,
                    wake=receipt,
                    wake_goal=waking,
                    replayed_wake=replay,
                )
                assert waking["status"] == "active", waking
                assert late.get("turn_id") == receipt["turn_id"], late
            elif case == "gx_fence":
                with connect() as root:
                    observe(root)
                    p.peer.gate.clear()
                    work = root.dispatch(task)
                    tid, turn = work["thread_id"], work["turn_id"]
                    # Let the initial turn complete without indigenous continuation.
                    root._request("thread/goal/set", {"threadId": tid, "status": "paused"})
                    p.peer.gate.set()
                    completion(root, tid, turn)
                    kill(root)
                with store.transaction(conn) as tx:
                    store.release_lease(tx, work["lease_id"])
                snapshot(conn, result, "before_old_resume")
                p.peer.scenario("old_submit")
                with connect() as root:
                    observe(root)
                    root._resume(tid)
                    # The old independently resumed Codex thread still has the tool.
                    # Drive an actual stale callback. GX must deny it on receipt.
                    old = root._request(
                        "turn/start",
                        {"threadId": tid, "input": [{"type": "text", "text": "Try stale submission"}]},
                    )["turn"]["id"]
                    completion(root, tid, old)
                post = snapshot(conn, result, "after_stale_submission")
                denied = [
                    o
                    for r in p.peer.rows
                    if r["scenario"] == "old_submit"
                    for o in r["tool_outputs"]
                    if "lease" in o.lower() or "owns the current" in o.lower()
                ]
                assert denied, [r["tool_outputs"] for r in p.peer.rows]
                assert not post["p0_root_calls"]
                assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
                result.update(
                    thread=tid, original_turn=turn, old_resumed_turn=old, rejected_tool_response=denied[-1]
                )
    finally:
        conn.close()
