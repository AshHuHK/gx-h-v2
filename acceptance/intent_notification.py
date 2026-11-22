"""Lose a real Task Root notification acknowledgement and recover it through Driver.deliveries."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import traceback
from unittest.mock import patch

from native_probe import PACKAGE, Peer, find_binary
from native_probe_roots import ReceiptLost
from e2e_grid import fixture_env
from gx_core import budgets
from gx_core.capabilities.shipped import ensure_grid
from gx_core.jobs.service import JobService, PURPOSE_WORKFLOW
from gx_core.p0 import intent
from gx_core.p0.driver import Driver, enqueue
from gx_core.persistence import records, store


def setup(conn):
    with store.transaction(conn) as tx:
        budget = budgets.create_root(tx, run_id=None, profile_name="p0", actor="probe")
        rid = records.create_run(tx, objective="Test routing of a changed research preference",
                                 project_ref="intent-delivery", actor="probe", budget_ref=budget,
                                 inputs={"inputs": {"p0": True, "p0_spec": {"turn_token_budget": 2000}}})
        for status in ("AUDITING", "PLANNING", "ACTIVE"):
            store.transition(tx, machine="run", subject_id=rid, to_state=status, actor="probe")
        task = records.create_task(tx, run_id=rid, kind="research_round", role="evidence_reviewer", actor="probe")
        store.transition(tx, machine="task", subject_id=task, to_state="READY", actor="probe")
    return rid, task


def change(driver, version, value):
    mid = "preference-" + str(version + 1)
    action = {"kind": "change", "items": [{"item_type": "soft_preference", "item_id": "reporting",
                                          "expected_version": version, "value": value}]}
    enqueue(driver.conn, driver.rid, action, "Change the reporting preference to " + value, mid)
    driver.messages()
    row = driver.conn.execute("SELECT result_json FROM p0_operator_messages WHERE message_id=?", (mid,)).fetchone()
    result = json.loads(row[0])
    assert result["status"] == "committed", result
    return mid, result


def await_terminal(native, thread, turn):
    native.client.run_until(lambda f: f.message.get("method") == "turn/completed"
                            and f.message["params"].get("threadId") == thread
                            and f.message["params"]["turn"]["id"] == turn, timeout=15)


def idle_on_job(driver, first):
    """An idle task can retain a real lease while waiting for a detached Job."""
    ensure_grid(driver.ctx)
    jobs = JobService(driver.conn, actor=first["thread_id"])
    grid = {"fixture_version": "1", "synthetic": True, "method_id": "fixture-grid", "method_version": "1",
            "candidates": [{"candidate_id": "one", "x": 2}], "delay_seconds": 1}
    raw = jobs.artifacts.publish_bytes(namespace=driver.rid + "/inputs", rel_path="grid.json", kind="job_input",
                                      data=json.dumps(grid), run_id=driver.rid, media_type="application/json")
    inputs = jobs.artifacts.make_input_manifest(namespace=driver.rid + "/grid/inputs", run_id=driver.rid,
                                                entries=[{"role": "grid", "artifact_ref": raw.artifact_ref}])
    job = jobs.submit(run_id=driver.rid, task_id=first["task_id"], lease_ref=first["lease_id"],
                      purpose=PURPOSE_WORKFLOW, capability_version="fixture-grid@1",
                      input_manifest_ref=inputs.artifact_ref, parameters={"operation": "idle-task"},
                      idempotency_key="idle-task", binding={"task_id": first["task_id"], "attempt_id": first["root_key"]})
    driver.native.wait_job(first["task_id"], job["job_id"])
    return job["job_id"]


def state(db, rid, oid):
    with closing(store.connect(db)) as read:
        read.execute("BEGIN")
        item = intent.read_item(read, rid, "reporting")
        change_row = read.execute("SELECT status,change_id,committed_at FROM p0_intent_changes WHERE change_id=?",
                                  (item["change_id"],)).fetchone()
        outbox = read.execute("SELECT * FROM outbox WHERE run_id=? AND outbox_id=?", (rid, oid)).fetchone()
        deliveries = list(read.execute("SELECT * FROM p0_root_deliveries WHERE run_id=? AND event_id=?",
                                       (rid, str(oid))))
        return {"item": item, "change": dict(change_row), "outbox": dict(outbox),
                "deliveries": [dict(r) for r in deliveries]}


def run_case(work, peer, mode, report):
    db = work / "gx.sqlite"
    trace = report["protocol"] = []
    with closing(store.open_store(db)) as conn:
        rid, task = setup(conn)
        with closing(Driver(conn, work, rid)) as driver:
            mid, _ = change(driver, 0, "list observations")
            driver.finish_main(mid)
            intent.bind_dependencies(conn, rid, "task", task, {"reporting": "context"})
            native = driver.native
            peer.gate.clear()
            first = native.dispatch(task)
            thread, original = first["thread_id"], first["turn_id"]
            if mode == "idle":
                report["waiting_job"] = idle_on_job(driver, first)
                peer.gate.set()
                await_terminal(native, thread, original)
            else:
                native._request("thread/goal/set", {"threadId": thread, "status": "paused"})
            mid, changed = change(driver, 1, "include uncertainty")
            if mode == "idle":
                driver.finish_main(mid)
            impact, = changed["impact"]
            oid = impact["outbox_id"]
            assert impact["action"] == "notify" and impact["lease_id"] == first["lease_id"]
            prior = report["after_commit"] = state(db, rid, oid)
            assert prior["item"]["version"] == 2 and prior["change"]["status"] == "committed"
            assert not prior["deliveries"] and prior["outbox"]["consumed_at"] is None
            peer.gate.clear()
            request = native._request

            def lose_ack(method, params):
                entry = {"direction": "before_request", "method": method, "thread": params.get("threadId"),
                         "target_turn": params.get("expectedTurnId"), "client_id": params.get("clientUserMessageId")}
                trace.append(entry)
                if method in ("turn/steer", "turn/start"):
                    seen = state(db, rid, oid)
                    assert seen["change"] == prior["change"] and seen["outbox"]["consumed_at"] is None
                    entry["change_id_visible_before_rpc"] = seen["change"]["change_id"]
                response = request(method, params)
                if method not in ("turn/start", "turn/steer"):
                    return response
                turn = (response.get("turn") or {}).get("id") or response["turnId"]
                trace.append({"direction": "withheld_reply", "method": method, "thread": thread, "turn": turn})
                report["oracle_turn"] = turn
                peer.gate.set()
                await_terminal(native, thread, turn)
                proc = native.client.transport.process
                proc.kill()
                report["terminated_app_server"] = {"pid": proc.pid, "returncode": proc.wait(5)}
                raise ReceiptLost("official App Server accepted the task change; inbound reply and frames lost")

            with patch.object(native, "_request", lose_ack), patch.object(native.client, "on_notification", lambda _: None):
                driver.deliveries()
            failed = report["failed_delivery"] = state(db, rid, oid)
            delivery, = failed["deliveries"]
            assert delivery["state"] == "sending" and delivery["turn_id"] is None, delivery
            assert delivery["method"] == ("turn/steer" if mode == "active" else "turn/start"), delivery
            assert delivery["target_turn"] == (original if mode == "active" else None), delivery
            assert failed["outbox"]["consumed_at"] is None and failed["outbox"]["attempts"] == 1
            assert "ReceiptLost" in failed["outbox"]["last_error"], failed["outbox"]
            assert failed["change"] == prior["change"]
    with closing(store.open_store(db)) as conn, closing(Driver(conn, work, rid)) as restarted:
        native = restarted.native
        request = native._request

        def recover(method, params):
            trace.append({"direction": "recovery_request", "method": method, "thread": params.get("threadId")})
            return request(method, params)

        with patch.object(native, "_request", recover):
            restarted.deliveries()
            recovered = report["recovered"] = state(db, rid, oid)
            receipt, = recovered["deliveries"]
            assert receipt["state"] == "acknowledged" and receipt["turn_id"] == report["oracle_turn"], recovered
            assert recovered["outbox"]["consumed_at"] and recovered["outbox"]["consumer"] == "gx-p0"
            assert recovered["outbox"]["last_error"] == failed["outbox"]["last_error"]
            assert recovered["outbox"]["attempts"] == failed["outbox"]["attempts"]
            assert recovered["change"] == prior["change"]
            assert not any(e["method"] in ("turn/start", "turn/steer") and e["direction"] == "recovery_request"
                           for e in trace)
            history = native._request("thread/turns/list", {"threadId": thread, "itemsView": "full", "limit": 32})
            messages = [{"turn": t["id"], "client_id": it.get("clientId"),
                         "text_sha256": hashlib.sha256(it["content"][0]["text"].encode()).hexdigest()}
                        for t in history["data"] for it in t["items"] if it["type"] == "userMessage"]
            report["official_user_messages"] = messages
            exact, = [m for m in messages if m["client_id"] == receipt["client_id"]]
            assert exact["turn"] == receipt["turn_id"] and exact["text_sha256"] == receipt["text_sha256"]
            n_calls, n_inferences = len(trace), len(peer.rows)
            restarted.deliveries()
            assert state(db, rid, oid) == recovered and len(trace) == n_calls and len(peer.rows) == n_inferences
            report["replay_new_native_calls"] = len(trace) - n_calls
            if mode == "idle":
                completed = JobService(conn).wait(report["waiting_job"], timeout_seconds=10)
                assert completed["status"] == "SUCCEEDED"
                report["job_backend"] = completed["scheduler_ref"]
            report.update(thread=thread, task=task, lease=first["lease_id"], run=rid, original_turn=original)


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--binary", type=Path)
    cli.add_argument("--output", type=Path, default=PACKAGE / "acceptance/runs/intent-notification")
    args = cli.parse_args()
    binary = args.binary or find_binary()
    args.output.mkdir(parents=True, exist_ok=True)
    failed = 0
    for mode in ("idle", "active"):
        peer = Peer()
        threading.Thread(target=peer.serve_forever, daemon=True).start()
        report = {"scenario": mode, "model_provider": "scripted loopback Responses", "human_intervention": False,
                  "gpu_seconds": 0, "external_api_cost": 0}
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="gx-notification-") as temp:
                work = Path(temp)
                env = fixture_env(work, peer, str(binary))
                env["GX_P0_MODEL"] = "gpt-6.1-sol"
                with patch.dict(os.environ, env, clear=True):
                    version = subprocess.check_output([binary, "--version"], text=True, timeout=5).strip()
                    assert version == "codex-cli 0.169.0", version
                    report["codex_version"] = version
                    run_case(work, peer, mode, report)
            report["status"] = "PASS"
        except Exception:
            failed += 1
            report.update(status="FAIL", traceback=traceback.format_exc())
        finally:
            peer.gate.set()
            peer.shutdown()
            peer.server_close()
            report.update(elapsed_seconds=time.monotonic()-started, loopback_inferences=len(peer.rows),
                          fixture_tokens=sum(r["usage"]["total_tokens"] for r in peer.rows))
            (args.output / (mode + ".json")).write_text(json.dumps(report, indent=2) + "\n")
        print(mode, report["status"], report.get("traceback", ""), flush=True)
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())
