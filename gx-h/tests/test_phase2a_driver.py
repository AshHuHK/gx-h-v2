"""Native transport loss holds only its own work across cold Driver opens."""
from argparse import Namespace
from contextlib import closing
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gx_core.domain import clock
from gx_core.hostobs.app_server_client import AppServerDisconnected
from gx_core.orchestration import branches
from gx_core.p0 import cli, driver, roots
from gx_core.persistence import store
from gx_core.teams.service import default_lease_seconds
from tests import test_p0_local_operator, test_p0_roots


class NativeOutage(unittest.TestCase):
    def test_public_status_shows_waiting_failed_and_completed_siblings(self):
        f = test_p0_local_operator.LocalOperator()
        self.addCleanup(f.doCleanups)
        f.setUp()
        since = clock.now_iso()
        decision = branches.wait_record("choose", {"kind": "client_work", "reason": "Choose an observation",
            "tasks": ["planner"], "payload": {"wait_for": "decision"}}, "choice-ref", since, 0, {})
        job = branches.wait_record("measure", {"kind": "external_job", "reason": "Await original Job",
            "job_refs": ["saved-job"]}, "job-ref", since, 0, {})
        failed = {"read_input": {"reason": "missing row"}}
        with store.transaction(f.conn):
            store.save_checkpoint(f.conn, run_id=f.rid, workflow_id="science_grid", workflow_version="1",
                node="choose", interrupt_ref="choice-ref", state={"completed": ["calibrate"],
                    "node_waits": {"choose": decision, "measure": job}, "failed_nodes": failed})
        for selector in (["--run-id", f.rid], []):
            status = f.cli("status", *selector)
            self.assertEqual(status["completed_nodes"], ["calibrate"])
            self.assertEqual(status["failed_nodes"], failed)
            for saved in (decision, job):
                public = status["node_waits"][saved["node"]]
                self.assertEqual(public, {key: saved[key] for key in
                    ("status", "owner", "reason", "since", "interrupt_ref", "references", "next_retry_at")})

    def test_two_cold_drivers_share_and_retire_one_wait_for_a_human_message(self):
        f = test_p0_local_operator.LocalOperator()
        self.addCleanup(f.doCleanups)
        f.setUp()
        text = "Review the saved measurements."
        mid = driver.enqueue(f.conn, f.rid, {"kind": "text", "text": text}, text, "review")
        with closing(store.open_store(f.state / "gx.db")) as second:
            resumed = driver.Driver(f.conn, f.state, f.rid)
            self.addCleanup(resumed.close)
            with patch.dict("os.environ", {driver.OFFLINE: "1"}):
                drivers = [driver.Driver(db, f.state, f.rid) for db in (f.conn, second)]
            earlier_inbox = drivers[0].messages(local_only=True)
            for worker in drivers:
                self.addCleanup(worker.close)
                worker.messages()
            wait, = driver.native_waits(f.conn, f.rid)
            self.assertEqual((wait["owner"], wait["reference"]), ("message", mid))
            def human_turn(text, *, before_start, on_started, message_id):
                before_start("main", message_id)
                on_started("main", "recovered-turn", message_id)
            resumed._native = SimpleNamespace(ensure_main=lambda: "main", human_turn=human_turn)
            resumed.messages()
            drivers[0].messages(pending=earlier_inbox)
            self.assertEqual(driver.native_waits(second, f.rid), [])
            self.assertIsNotNone(f.conn.execute("SELECT result_json FROM p0_operator_messages WHERE message_id=?",
                                                (mid,)).fetchone()[0])
            self.assertEqual(f.conn.execute("SELECT count(*) FROM outbox WHERE topic=?", (driver.NATIVE_WAIT,)).fetchone()[0], 1)

    def test_disconnected_dispatch_resumes_exact_expired_attempt_after_cold_backoff(self):
        f = test_p0_roots.DurableRoots()
        self.addCleanup(f.doCleanups)
        f.setUp()
        f.conn.execute("UPDATE runs SET inputs_json=? WHERE run_id=?", (
            json.dumps({"inputs": {"p0": True, "p0_spec": {}}}), f.run))
        f.roots._request = f.fake
        original = f.bind()
        fixed = clock.now()
        self.enterContext(patch.object(clock, "_source", return_value=fixed))
        self.addCleanup(clock.reset)
        attempted = []
        def dispatch(task_id):
            attempted.append(task_id)
            self.assertFalse(f.conn.in_transaction)
            if len(attempted) == 1:
                raise AppServerDisconnected("test transport closed")
            current = roots.require_binding(f.conn, f.run, original["thread_id"])
            self.assertEqual((current["root_key"], current["lease_id"]), (original["root_key"], original["lease_id"]))
            return {"turn_id": "same-turn"}
        peer = SimpleNamespace(dispatch=dispatch, active_turns={}, poll=lambda delay: False)
        def connect(worker):
            worker._native = peer
            return peer
        self.enterContext(patch.object(driver.Driver, "native", property(connect)))
        def tick():
            with closing(driver.Driver(f.conn, f.tmp.name, f.run)) as worker:
                with closing(worker.passes(time.monotonic()+10, shared=True)) as cycle:
                    next(cycle)
                    next(cycle)
        tick()
        wait, = driver.native_waits(f.conn, f.run)
        self.assertEqual((wait["owner"], wait["reference"], wait["lease_id"], wait["reason"]),
                         ("task", f.task, original["lease_id"], "AppServerDisconnected"))
        self.assertGreater(wait["next_retry_at"], wait["since"])
        tick()
        self.assertEqual(attempted, [f.task])
        self.assertEqual(driver.native_waits(f.conn, f.run), [wait])
        clock.advance(default_lease_seconds() + 10)
        tick()
        self.assertEqual(attempted, [f.task, f.task])
        self.assertEqual(driver.native_waits(f.conn, f.run), [])
        self.assertEqual(f.conn.execute("SELECT count(*) FROM leases WHERE task_id=?", (f.task,)).fetchone()[0], 1)
        self.assertEqual(f.conn.execute("SELECT attempts_consumed FROM tasks WHERE task_id=?", (f.task,)).fetchone()[0], 0)

    def test_offline_unkeyed_bootstrap_stays_unauthorized_in_both_cli_run_modes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {driver.OFFLINE: "1"}), patch.object(
                roots.Roots, "__enter__", side_effect=AssertionError("offline bootstrap opened Codex")):
            directory = Path(tmp)
            request = directory / "study.json"
            request.write_text(json.dumps(cli.demo_spec()))
            initial = cli.execute(Namespace(command="start", state=directory, spec=request, run_key=None, example=None))
            self.assertEqual(initial["driver"], "initial_intent_pending")
            rid = initial["run_id"]
            waiting, = initial["native_waits"]
            self.assertEqual((waiting["owner"], waiting["next_retry_at"]), ("message", None))
            for selector in (rid, None):
                result = cli.execute(Namespace(command="run", state=directory, run_id=selector, timeout=0.15))
                self.assertEqual(result["driver"], "time_limit")
                self.assertEqual(result["native_waits"], [waiting])
            with closing(store.open_store(directory / "gx.db")) as db:
                self.assertIsNone(store.latest_checkpoint(db, rid))
                self.assertIsNone(db.execute("SELECT 1 FROM jobs WHERE run_id=?", (rid,)).fetchone())
                self.assertIsNone(db.execute("SELECT result_json FROM p0_operator_messages WHERE run_id=?", (rid,)).fetchone()[0])
