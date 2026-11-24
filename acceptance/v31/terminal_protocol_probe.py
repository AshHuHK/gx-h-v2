"""Exercise pending tool cancellation on the packaged official Codex using a loopback model.

The protocol driver controls timing and faults. GX owns real Task/Lease/submission
transactions, per-Turn accounting and descendant reconciliation. No model credentials.
"""
import argparse
from collections import Counter, deque
from contextlib import closing, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PACKAGE / "acceptance"))
from native_probe import Probe, _proxy_preflight
from native_probe_peer import CHILD, Peer, tool
from gx_core import budgets
from gx_core.artifacts import ArtifactService
from gx_core.hostobs.app_server_client import AppServerTimeout, NO_TOOL_RESPONSE
from gx_core.orchestration.service import default_registry
from gx_core.p0 import schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(extra_sources=(), *, runtime=PACKAGE):
    assets = PACKAGE / "release-assets"
    lock = json.loads((assets / "lock.json").read_text())
    binary = runtime / "vendor/codex-runtime/package" / lock["linux_x64"]["binary"]
    archive = assets / lock["source"]["file"]
    source_paths = [
        "codex-rs/app-server/src/bespoke_event_handling.rs",
        "codex-rs/app-server/src/dynamic_tools.rs",
        "codex-rs/app-server/src/outgoing_message.rs",
        "codex-rs/app-server/src/request_processors/turn_processor.rs",
        "codex-rs/core/src/tools/handlers/dynamic.rs",
        *extra_sources,
    ]
    checked = {}
    with tarfile.open(archive, "r|gz") as tar:
        for member in tar:
            if member.name in source_paths:
                actual = digest(runtime / "vendor/codex" / member.name)
                assert actual == hashlib.sha256(tar.extractfile(member).read()).hexdigest()
                checked[member.name] = actual
    assert set(checked) == set(source_paths)
    receipt = {
        "source_commit_in_release_lock": lock["source"]["commit"],
        "source_tag": lock["source"]["tag"], "source_archive_sha256": digest(archive),
        "verified_source_files": checked, "binary_sha256": digest(binary),
        "binary_package_sha256": digest(runtime / "release-assets" / lock["linux_x64"]["file"]),
    }
    assert receipt["source_archive_sha256"] == lock["source"]["sha256"]
    assert receipt["binary_sha256"] == lock["linux_x64"]["binary_sha256"]
    assert receipt["binary_package_sha256"] == lock["linux_x64"]["sha256"]
    return binary, receipt


class ScriptedPeer(Peer):
    def __init__(self):
        super().__init__()
        self.trials = {}
        self.child_started = threading.Event()
        self.release_child = threading.Event()

    def record(self, body):
        items = body["input"]
        child = any(i.get("recipient") == "/root/" + CHILD for i in items)
        markers = set(re.findall(r"GX_ABORT_[0-9]+", json.dumps(items) + str(body.get("instructions", ""))))
        assert len(markers) == 1, markers
        marker = markers.pop()
        with self.lock:
            trial = self.trials[marker]
            ident = f"probe-response-{len(self.rows) + 1}"
            count = sum(row["child"] == child for row in trial["model_requests"]) + 1
            usage = {"input_tokens": 170 if child else 30, "output_tokens": 13 if child else 5}
            usage["total_tokens"] = sum(usage.values())
            row = {"id": ident, "child": child, "model": body["model"], "t": time.monotonic(),
                   "trial": marker, "usage": usage}
            self.rows.append(row)
            trial["model_requests"].append(row)
        return row, count

    def item(self, row, n):
        trial = self.trials[row["trial"]]
        assert self.gate.wait(20), "fixture did not release the first response"
        call_id = "call-" + row["id"]
        if trial["fault"] == "running_child":
            if row["child"]:
                self.child_started.set()
                assert self.release_child.wait(20), "child did not reach release"
                return self.message(row["id"], "Child protocol observation complete")
            if n == 1:
                return tool("spawn_agent", {"task_name": CHILD, "fork_turns": "none",
                    "message": "Observe the completion barrier in " + row["trial"]}, call_id, "collaboration")
            assert self.child_started.wait(10), "expected an independently running native child"
            n -= 1
        if n == 1:
            return tool("gx_submit_product", {"product": trial["product"]}, call_id)
        return self.message(row["id"], "The fixture received another request")


class Wire:
    """Observe wire writes, and optionally lose an interrupt or its reply/notification."""
    def __init__(self, transport, trial):
        self.real, self.trial = transport, trial
        self.pending = {}
        self.withheld = deque()
        self.replay = False

    def write_line(self, line, timeout):
        msg = json.loads(line)
        method = msg.get("method")
        if method is None:
            self.trial["tool_responses_written"].append(msg)
        elif method == "thread/goal/set" and msg["params"].get("status") == "complete":
            self.trial["goal_complete_write"] = {"id": msg["id"], "params": msg["params"], "t": time.monotonic()}
        elif method == "turn/interrupt":
            self.pending[msg["id"]] = method
            writes = self.trial["interrupt_writes"]
            writes.append({"id": msg["id"], "params": msg["params"], "t": time.monotonic(),
                           "dropped": not writes and self.trial["fault"] == "lost_interrupt"})
            if writes[-1]["dropped"]:
                return
        self.real.write_line(line, timeout)

    def read_line(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if self.replay and self.withheld:
                return self.withheld.popleft()
            left = deadline - time.monotonic()
            if left <= 0:
                raise AppServerTimeout("probe wire deadline")
            line = self.real.read_line(left)
            msg = json.loads(line)
            method = msg.get("method")
            interrupt_ack = not method and msg.get("id") in self.pending
            complete = (method == "turn/completed" and msg["params"]["turn"]["id"] == self.trial.get("turn_id"))
            if interrupt_ack:
                self.trial["server_interrupt_replies"].append(msg)
            if (self.trial["fault"] in ("delayed_usage", "lost_usage") and method == "thread/tokenUsage/updated"
                    and msg["params"]["threadId"] == self.trial.get("thread_id")
                    and msg["params"]["turnId"] == self.trial.get("turn_id")):
                self.trial["injected_receipt_faults"].append({"method": method,
                    "turn_id": msg["params"]["turnId"], "tokens": msg["params"]["tokenUsage"]["total"]["totalTokens"]})
                if self.trial["fault"] == "delayed_usage":
                    self.withheld.append(line)
                continue
            if (interrupt_ack or complete) and self.trial["fault"] in ("lost_receipts", "delayed_receipts"):
                self.trial["injected_receipt_faults"].append(
                    {"method": method or "turn/interrupt reply", "id": msg.get("id"),
                     "status": msg.get("params", {}).get("turn", {}).get("status")})
                if self.trial["fault"] == "delayed_receipts":
                    self.withheld.append(line)
                continue
            return line

    def __getattr__(self, name):
        return getattr(self.real, name)


def initial_store(conn, url, provider, *, metering_grace=None):
    schema.install(conn)
    workflow = default_registry().get("science_grid", "2")
    with store.transaction(conn):
        budget = budgets.create_root(conn, run_id=None, actor="protocol-fixture", profile_name="p0")
        inputs = None
        if metering_grace is not None:
            inputs = {"inputs": {"p0_spec": {"metering": {"grace_seconds": metering_grace}}}}
        run = records.create_run(conn, objective="Exercise terminal delivery for synthetic failed measurements",
            project_ref="terminal-protocol", actor="fixture", budget_ref=budget, workflow_id="science_grid",
            workflow_version="2", pins={"workflow_sha256": workflow.digest()}, inputs=inputs,
            host={"policy_selection": {"test_mode": True, "model_provider": provider, "test_base_url": url}})
        for state in ("AUDITING", "PLANNING", "ACTIVE"):
            store.transition(conn, machine="run", subject_id=run, to_state=state, actor="fixture")
        store.acquire_coordinator_lease(conn, run_id=run, host="codex", owner="protocol-coordinator")
        assets = ArtifactService(conn)
        def publish(kind, body):
            return assets.publish_bytes(run_id=run, namespace=run, rel_path=kind + ".json", kind=kind,
                                        media_type="application/json", data=json.dumps(body)).artifact_ref
        proposal = publish("research_proposal", {"paths": [{"id": "sample"}]})
        evidence = publish("research_evidence", {"routes": [{"path_id": "sample", "status": "FAILED"}]})
    return run, proposal, evidence


def submissions(reader, task_id):
    return [dict(row) for row in reader.execute(
        "SELECT t.status,t.attempt,s.submission_id,s.result,s.lease_id,s.records_artifact_ref,"
        "s.artifact_refs_json FROM tasks t JOIN submissions s USING(task_id) WHERE t.task_id=?", (task_id,))]


def meter(reader, thread, turn):
    rows = [dict(row) for row in reader.execute(
        "SELECT reservation_ref,budget_status,confidence,terminal_status,measured_tokens,settled_tokens,"
        "estimated_at,metering_reason FROM p0_meter_native_turns WHERE thread_id=? AND turn_id=?", (thread, turn))]
    assert len(rows) == 1, rows
    return rows[0]


def observe(trial, original, frame):
    msg = frame.message
    m, p = msg["method"], msg.get("params", {})
    item = p.get("item") or {}
    event = None
    if (m == "item/completed" and item.get("type") in ("dynamicToolCall", "subAgentActivity")):
        event = {"item": item}
    elif m == "rawResponseItem/completed" and item.get("type") in ("function_call", "function_call_output"):
        event = {k: item[k] for k in ("type", "name", "call_id") if k in item}
        if item.get("type") == "function_call_output":
            event["output"] = item.get("output")
    elif m == "rawResponse/completed":
        event = {"response_id": p["responseId"], "usage": p.get("usage")}
    elif m == "thread/tokenUsage/updated":
        event = {"tokens": p["tokenUsage"]["total"]["totalTokens"]}
    elif m == "turn/completed":
        event = {"status": p["turn"]["status"], "error": p["turn"].get("error")}
    elif m == "serverRequest/resolved":
        event = {"request_id": p.get("requestId")}
    if event is not None:
        trial["events"].append({"seq": frame.receive_seq, "t": time.monotonic(), "method": m, "thread": p.get("threadId"),
            "turn": p.get("turnId") or (p.get("turn") or {}).get("id"), **event})
    original(frame)


def cold_usage(roots, trial):
    """Read @2's original interrupted turn after its Goal has been verified complete."""
    thread, turn = trial["thread_id"], trial["turn_id"]
    record = {"thread_id": thread, "turn_id": turn, "snapshots": [], "turns_started": []}
    trial["cold_usage"] = record
    prior = roots.client.transport.process
    record["original_unsubscribe"] = roots._request("thread/unsubscribe", {"threadId": thread})["status"]
    prior.stdin.close()
    record["original_host_exit"] = prior.wait(timeout=8)
    assert record["original_host_exit"] == 0
    roots.client.close()
    with Roots(roots.conn, run_id=roots.run_id, cwd=roots.cwd, argv=roots.argv, env=roots.env) as cold:
        binding = cold._binding(thread)
        def check_thread(history):
            assert binding["thread_id"] == history["id"] == thread and cold._matches(binding, history)
            match, = [entry for entry in history["turns"] if entry["id"] == turn]
            assert match["status"] == "interrupted"
        def receive(frame):
            msg = frame.message
            params = msg.get("params", {})
            if msg["method"] == "thread/tokenUsage/updated" and (params["threadId"], params["turnId"]) == (thread, turn):
                record["snapshots"].append(params["tokenUsage"]["total"]["totalTokens"])
            if msg["method"] == "turn/started":
                record["turns_started"].append(params["turn"]["id"])
            cold._observe(frame)
        check_thread(cold._request("thread/read", {"threadId": thread, "includeTurns": True})["thread"])
        goal = cold._request("thread/goal/get", {"threadId": thread})["goal"]
        assert goal["threadId"] == thread and goal["status"] == "complete"
        record["goal_status"] = goal["status"]
        settings = cold._settings(binding, new=False)
        settings["config"]["features.goals"] = False
        record["goals_enabled"] = settings["config"]["features.goals"]
        with patch.object(cold.client, "on_notification", receive):
            resumed = cold._request("thread/resume", {**settings, "threadId": thread})
            cold._verify_settings(binding, resumed)
            check_thread(resumed["thread"])
            cold.delivery.attached(thread, resumed["thread"])
            deadline = time.monotonic() + 5
            while not record["snapshots"] and time.monotonic() < deadline:
                cold.client.run_until(lambda _: bool(record["snapshots"]), timeout=1, poll_interval=0.15)
            cold.delivery.reconcile(thread)
        record["new_model_requests"] = [r["id"] for r in trial["model_requests"] if r["t"] > trial["committed_at"]]
        assert not record["turns_started"] and not record["new_model_requests"], record


def trial_run(roots, reader, peer, number, expected, fault, proposal, evidence, *, missing_usage=None):
    marker = f"GX_ABORT_{number:04d}"
    condition = None if expected == "ACCEPTED" else False
    product = {"proposal_ref": proposal, "evidence_refs": [evidence], "findings": [{"path_id": "sample",
               "reason": "The fixture measurement failed.", "evaluation_ref": None, "supported": condition}]}
    delay_ms = (0, 1, 3, 7, 15, 27, 50)[number % 7]
    trial = {"id": marker, "run_id": roots.run_id, "fault": fault, "expected": expected, "delay_ms": delay_ms, "product": product,
             "model_requests": [], "events": [], "interrupt_writes": [], "tool_responses_written": [],
             "injected_receipt_faults": [], "server_interrupt_replies": [], "fallback_observations": []}
    peer.trials[marker] = trial
    peer.gate.clear()
    conn = roots.conn
    task = records.create_task(conn, run_id=roots.run_id, kind="research_round", role="evidence_reviewer",
        actor="protocol-fixture", required_artifacts=["science_assessment"],
        acceptance={"validate_workflow_product": True}, task_input={"output_kind": "science_assessment",
        "round": number, "proposal_ref": proposal, "evidence_refs": [evidence], "fixture": marker})
    store.transition(conn, machine="task", subject_id=task, to_state="READY", actor="fixture")
    client = roots.client
    with ExitStack() as stack:
        # The wire is the final stdio boundary. Its log includes every bare JSON-RPC result/error.
        wire = Wire(client.transport, trial)
        stack.enter_context(patch.object(client, "_transport", wire))
        original = client.on_notification
        stack.enter_context(patch.object(client, "on_notification", lambda frame: observe(trial, original, frame)))
        def callback(frame):
            p = frame.message["params"]
            assert p["tool"] == "gx_submit_product" and frame.message["method"] == "item/tool/call"
            roots._call(p)
            verdicts = submissions(reader, task)
            assert len(verdicts) == 1 and verdicts[0]["result"] == verdicts[0]["status"] == expected, verdicts
            receipt = reader.execute("SELECT result_json FROM p0_root_calls WHERE thread_id=? AND turn_id=? "
                                     "AND call_id=?", (p["threadId"], p["turnId"], p["callId"])).fetchone()
            assert receipt and json.loads(receipt[0])["submission_id"] == verdicts[0]["submission_id"]
            trial.update(committed_at=time.monotonic(), committed=verdicts[0], request_id=frame.message["id"],
                         call_id=p["callId"], callback_seq=frame.receive_seq)
            return NO_TOOL_RESPONSE
        stack.enter_context(patch.object(client, "on_server_request", callback))
        work = roots.dispatch(task)
        thread, turn = work["thread_id"], work["turn_id"]
        trial.update(thread_id=thread, turn_id=turn, task_id=task)
        peer.gate.set()
        client.run_until(lambda f: f.message.get("method") == "item/tool/call"
                         and f.message["params"]["threadId"] == thread, timeout=15)
        if fault == "running_child":
            assert peer.child_started.is_set() and not peer.release_child.is_set()
            roots.delivery.verify_descendants()
        params = {"threadId": thread, "turnId": turn}
        goal = client.request("thread/goal/set", {"threadId": thread, "origin": "automatic", "status": "complete"})
        assert goal["goal"]["status"] == "complete"
        trial["goal_complete_at"] = time.monotonic()
        time.sleep(delay_ms / 1000)
        def read_turn():
            turns = client.request("thread/turns/list", {"threadId": thread, "sortDirection": "desc",
                                   "itemsView": "full", "limit": 2})["data"]
            return next(item for item in turns if item["id"] == turn)
        def terminal(frame):
            msg = frame.message
            return (msg.get("method") == "turn/completed" and msg["params"]["threadId"] == thread
                    and msg["params"]["turn"]["id"] == turn)
        interrupt = client.send("turn/interrupt", params)
        try:
            client.run_until(terminal, timeout=0.18 if fault in ("lost_interrupt", "lost_receipts",
                                                                 "delayed_receipts") else 8)
        except AppServerTimeout:
            history = read_turn()
            assert submissions(reader, task) == [trial["committed"]]
            trial["fallback_observations"].append({"turn_id": history["id"], "status": history["status"],
                "items": [{"id": i["id"], "status": i.get("status")} for i in history["items"]
                          if i["type"] == "dynamicToolCall"], "submission_id": trial["committed"]["submission_id"]})
            if history["status"] == "inProgress":
                interrupt = client.send("turn/interrupt", params)
                client.run_until(terminal, timeout=8)
            else:
                assert history["status"] == "interrupted", history
        if fault == "delayed_receipts":
            wire.replay = True
            client.run_until(terminal, timeout=5)
            client.wait(interrupt, timeout=2)
        elif fault != "lost_receipts":
            client.wait(interrupt, timeout=2)
        history = read_turn()
        cancelled = [i for i in history["items"] if i["type"] == "dynamicToolCall" and i["id"] == trial["call_id"]]
        assert history["status"] == "interrupted" and len(cancelled) == 1
        assert cancelled[0]["status"] == "failed" and cancelled[0]["success"] is False, cancelled
        trial["turn_history"] = {"id": history["id"], "status": history["status"], "call": cancelled[0]}
        roots.reconcile_meter(thread)
        if fault == "running_child":
            trial["while_child_running"] = meter(reader, thread, turn)
            assert trial["while_child_running"]["budget_status"] == "HELD", trial["while_child_running"]
            peer.release_child.set()
        first_meter = meter(reader, thread, turn)
        if fault == "delayed_usage":
            assert first_meter["terminal_status"] == "interrupted" and first_meter["budget_status"] == "HELD"
            wire.replay = True
        if first_meter["budget_status"] == "HELD":
            trial["meter_before_drain"] = first_meter
            # Turn completion and its cumulative usage are distinct official notifications.
            deadline = time.monotonic() + 10
            while meter(reader, thread, turn)["budget_status"] == "HELD" and time.monotonic() < deadline:
                client.run_until(lambda f: f.message.get("method") == "thread/tokenUsage/updated"
                                 and f.message["params"].get("threadId") == thread
                                 and f.message["params"].get("turnId") == turn,
                                 timeout=1, poll_interval=0.15)
                if fault == "running_child":
                    roots.delivery.verify_descendants()
                roots.reconcile_meter(thread)
            unsettled = meter(reader, thread, turn)
            if (unsettled["budget_status"] == "HELD" and unsettled["terminal_status"] == "interrupted"
                    and unsettled["metering_reason"] == "missing cumulative usage for this turn"):
                cold_usage(roots, trial)
                if not trial["cold_usage"]["snapshots"]:
                    if missing_usage is None or fault != "none":
                        raise AssertionError((marker, unsettled, trial["cold_usage"], trial["events"]))
                    trial["official_usage_missing"] = missing_usage(roots, trial)
        if fault == "running_child":
            trial["children"] = [dict(x) for x in reader.execute(
                "SELECT m.child_id,m.parent_id,m.root_id,m.metadata_verified,u.turn_id,u.owner_turn,u.terminal,"
                "u.measured_tokens FROM p0_native_child_meters m JOIN p0_native_child_usage u USING(child_id) "
                "WHERE m.root_id=?", (thread,))]
            assert len(trial["children"]) == 1 and trial["children"][0]["metadata_verified"]
            assert trial["children"][0]["terminal"] and trial["children"][0]["measured_tokens"] == 183
        trial["final_meter"] = meter(reader, thread, turn)
        if fault == "lost_usage":
            assert trial["injected_receipt_faults"] and trial["cold_usage"]["snapshots"][-1] == (
                trial["injected_receipt_faults"][-1]["tokens"])
        if fault == "delayed_usage":
            interrupted = next(e["seq"] for e in trial["events"] if e["method"] == "turn/completed" and e["turn"] == turn)
            delivered = [e["tokens"] for e in trial["events"] if e["method"] == "thread/tokenUsage/updated"
                         and e["thread"] == thread and e["turn"] == turn and e["seq"] > interrupted]
            assert delivered and delivered == [r["tokens"] for r in trial["injected_receipt_faults"]]
        assert trial["final_meter"]["budget_status"] == "SETTLED", (marker, fault, trial["final_meter"], trial["events"])
        if "official_usage_missing" in trial:
            assert trial["final_meter"]["estimated_at"] and trial["final_meter"]["confidence"] == "ESTIMATED"
        else:
            assert not trial["final_meter"]["estimated_at"], (marker, fault, trial["final_meter"], trial["events"])
            assert trial["final_meter"]["settled_tokens"] == sum(r["usage"]["total_tokens"] for r in trial["model_requests"])
        assert trial["committed_at"] <= trial["goal_complete_write"]["t"] <= trial["goal_complete_at"]
        assert trial["goal_complete_at"] <= trial["interrupt_writes"][0]["t"] and not trial["tool_responses_written"]
        assert any(event.get("type") == "function_call_output" and event.get("call_id") == trial["call_id"]
                   and event.get("output") == "dynamic tool call was cancelled before receiving a response"
                   for event in trial["events"]), trial["events"]
        assert submissions(reader, task) == [trial["committed"]]
        trial["budget_settlements"] = [dict(row) for row in reader.execute(
            "SELECT event_id,json_extract(payload_json,'$.actual.tokens') AS tokens FROM events "
            "WHERE kind='budget.settled' AND json_extract(payload_json,'$.reservation_ref')=?",
            (trial["final_meter"]["reservation_ref"],))]
        assert len(trial["budget_settlements"]) == 1
        trial["root_failures"] = [dict(row) for row in reader.execute(
            "SELECT settlement_id,kind FROM science_settlements WHERE task_id=? AND kind IN "
            "('infrastructure_failure','agent_execution_failure')", (task,))]
        assert not trial["root_failures"]
        if fault in ("lost_interrupt", "lost_receipts", "delayed_receipts"):
            assert len(trial["fallback_observations"]) == 1
        assert len(trial["interrupt_writes"]) == (2 if fault == "lost_interrupt" else 1)
        if "cold_usage" not in trial:
            client.request("thread/unsubscribe", {"threadId": thread})
    return trial


def run(repetitions, only=None, *, closure_usage=False, runtime=PACKAGE):
    _proxy_preflight()
    binary, version = identity(runtime=runtime)
    if closure_usage:
        from closure.official_usage import GRACE_SECONDS, reconcile_natural
    started = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix="gx-terminal-wire-") as tmp, ExitStack() as stack:
        folder = Path(tmp)
        peer = ScriptedPeer()
        threading.Thread(target=peer.serve_forever, daemon=True).start()
        stack.callback(peer.server_close)
        stack.callback(peer.shutdown)
        stack.callback(peer.release_child.set)
        stack.callback(peer.gate.set)
        probe = Probe(folder / "peer", binary, peer)
        version["binary_cli_version"] = subprocess.check_output([binary, "--version"], env=probe.env, text=True).strip()
        assert version["binary_cli_version"] == "codex-cli 0.169.0"
        knobs = {"GX_P0_TEST": "1", "GX_P0_TEST_PROVIDER": probe.provider, "GX_P0_MODEL": "gpt-6.1-sol",
                 "GX_P0_CODEX": str(binary), "CODEX_HOME": str(probe.home)}
        stack.enter_context(patch.dict(os.environ, knobs))
        stack.enter_context(patch("gx_core.p0.roots.tempfile.gettempdir", return_value=tmp))
        db = folder / "gx.db"
        conn = stack.enter_context(closing(store.open_store(db)))
        reader = stack.enter_context(closing(sqlite3.connect(db)))
        reader.row_factory = sqlite3.Row
        sequence = [(status, "none") for _ in range(repetitions) for status in ("ACCEPTED", "REJECTED")]
        sequence.extend([("ACCEPTED", "lost_interrupt"), ("REJECTED", "lost_receipts"),
                         ("ACCEPTED", "delayed_receipts"), ("ACCEPTED", "running_child")])
        if only in ("delayed_usage", "lost_usage"):
            sequence = [("ACCEPTED", only)]
        elif only:
            sequence = [case for case in sequence if case[1] == only]
        results, run_ids = [], []
        # Each sample is an independent task. Partition test cases within one GX Run's
        # existing worker-task and API-call budgets without increasing either ceiling.
        limits = budgets.profile("p0")["limits"]
        batch_size = min(limits["worker_tasks"], limits["api_calls"])
        for offset in range(0, len(sequence), batch_size):
            run_id, proposal, evidence = initial_store(conn, f"http://127.0.0.1:{peer.server_port}/v1", probe.provider,
                                                      metering_grace=GRACE_SECONDS if closure_usage else None)
            run_ids.append(run_id)
            index = offset
            end = min(offset + batch_size, len(sequence))
            while index < end:
                with Roots(conn, run_id=run_id, cwd=probe.cwd, argv=[str(binary), "app-server",
                           "--listen", "stdio://"], env=probe.env, token_budget=800) as roots:
                    version["app_server_initialize_user_agent"] = roots.client.response_received(1).message["result"]["userAgent"]
                    while index < end:
                        expected, fault = sequence[index]
                        index += 1
                        trial = trial_run(roots, reader, peer, index, expected, fault, proposal, evidence,
                                          missing_usage=reconcile_natural if closure_usage else None)
                        results.append(trial)
                        if "cold_usage" in trial:
                            break  # This host released its writer for a cold receipt.
                    else:
                        roots.client.run_until(lambda _: False, timeout=1, poll_interval=0.3)
        for result in results:
            post_terminal = [r["id"] for r in result["model_requests"] if r["t"] > result["committed_at"]]
            assert not post_terminal, (result["id"], post_terminal)
            assert len(result["model_requests"]) == (3 if result["fault"] == "running_child" else 1)
            result["post_terminal_model_requests"] = post_terminal
        return {"started_utc": started, "finished_utc": datetime.now(timezone.utc).isoformat(), "identity": version,
            "run_ids": run_ids, "instrumentation": "B0 controlled interrupt timing and receipt faults; real Roots._call, "
            "TeamService, AppServerClient, GX budget ledger and official Codex with loopback scripted model",
            "trials": results, "trial_count": len(results), "counts": dict(Counter(r["expected"] for r in results)),
            "faults": dict(Counter(r["fault"] for r in results)), "post_terminal_requests": 0,
            "normal_terminal_tool_responses": 0, "estimated_settlements": sum(
                bool(r["final_meter"]["estimated_at"]) for r in results), "passed": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=100, help="repetitions per accepted/rejected outcome")
    parser.add_argument("--only", choices=("none", "lost_interrupt", "lost_receipts",
                                          "delayed_receipts", "running_child", "delayed_usage", "lost_usage"))
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("terminal-protocol.json"))
    parser.add_argument("--runtime", type=Path, default=PACKAGE, help="Directory containing the locked unpacked native runtime")
    parser.add_argument("--closure-usage", action="store_true",
                        help="Verify pending and product-maintained estimated billing after a naturally missing cold receipt")
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    result = run(args.repetitions, args.only, closure_usage=args.closure_usage, runtime=args.runtime.resolve())
    args.output.write_text(json.dumps(result, separators=(",", ":")) + "\n")
    print(json.dumps({k: result[k] for k in ("trial_count", "counts", "faults", "passed")}))
