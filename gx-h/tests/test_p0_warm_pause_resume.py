"""An operator pause may outlive a worker turn without ending its scientific attempt."""
import io
import json
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from gx_core.errors import HoldActive, OutcomeUnknown
from gx_core.native_release import native_state
from gx_core.p0 import cli, driver, roots, v6_goal
from gx_core.p0.terminal_products import TerminalProducts
from gx_core.persistence import records, store
from tests import test_p0_wait_leases


class WarmPauseResume(unittest.TestCase):
    def setUp(self):
        f = self.f = test_p0_wait_leases.WaitLeases()
        self.addCleanup(f.doCleanups)
        open_store = store.open_store

        def open_cli_state(path):
            with native_state(Path(path).parent) as state:
                return open_store(state.db_path)

        with patch.object(store, "open_store", side_effect=open_cli_state):
            f.setUp()
        f.db.execute("UPDATE runs SET inputs_json=json_set(inputs_json,'$.inputs.p0_spec.max_parallel_roots',1)"
                     " WHERE run_id=?", (f.run,))
        self.now = 10
        self.enterContext(patch.object(driver.time, "monotonic", side_effect=lambda: self.now))
        # This test drives the task dispatcher itself. The fake App Server has no socket;
        # unrelated workflow advancement and usage refresh do not drive its notifications.
        self.enterContext(patch.object(f.roots, "poll", return_value=False))
        for name in ("lazy", "drive_science"):
            self.enterContext(patch.object(driver.reactor, name))
        self.worker = self.enterContext(closing(driver.Driver(f.db, f.directory, f.run)))
        self.worker._native = f.roots
        self.worker.offline = False
        for name in ("deliveries", "reconcile_costs"):
            self.enterContext(patch.object(self.worker, name, return_value=False))
        self.passes = self.enterContext(closing(self.worker.passes(100, shared=True)))
        next(self.passes)

    def tick(self):
        self.now += 1
        next(self.passes)

    def control(self, action):
        f = self.f
        out, err = io.StringIO(), io.StringIO()
        command = ["operator", "resume"] if action == "resume" else [action]
        argv = [*command, "--state", str(f.directory), "--run-id", f.run]
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        self.assertEqual(code, 0, err.getvalue())
        receipt = json.loads(out.getvalue())
        return receipt.get("result", receipt)

    def start_and_stop(self):
        self.tick()
        f = self.f
        original, = f.peer.turns
        tid = original["id"]
        f.roots.delivery.observe("turn/started", {"threadId": f.thread, "turn": {"id": tid}})
        f.roots.delivery.observe("rawResponse/completed", {"threadId": f.thread, "turnId": tid,
            "responseId": "official-before-pause", "usage": {"totalTokens": 11}})
        f.roots.delivery.observe("thread/tokenUsage/updated", {"threadId": f.thread, "turnId": tid,
            "tokenUsage": {"total": {"totalTokens": 11}}})
        state = self.control("stop")
        self.assertEqual(state["status"], "PAUSED")
        return original

    def finish(self, turn):
        f = self.f
        turn["status"] = "completed"
        f.roots.delivery.observe("turn/completed", {"threadId": f.thread, "turn": turn})
        f.roots._ended_turn(f.thread, turn)

    def run_after_resume(self):
        self.assertNotEqual(self.control("resume")["status"], "PAUSED")
        for _ in range(3):
            self.tick()

    def test_stopped_worker_resumes_once_on_same_driver_and_job_is_reused(self):
        f = self.f
        sibling = records.create_task(f.db, run_id=f.run, kind="research_round", role="planner", actor="test")
        store.transition(f.db, machine="task", subject_id=sibling, to_state="READY", actor="test", reason="fixture")
        original = self.start_and_stop()
        self.finish(original)
        self.run_after_resume()
        self.assertEqual(len(f.peer.turns), 2, "completed, pause-bound turn must yield one native continuation")
        resumed = f.peer.turns[-1]["id"]
        saved = f.roots.delivery._get("resume:" + original["id"])
        self.assertEqual((saved["thread_id"], saved["turn_id"], saved["state"]), (f.thread, resumed, "acknowledged"))
        f.assert_owned()
        self.assertEqual(f.db.execute("SELECT status FROM tasks WHERE task_id=?", (sibling,)).fetchone()[0], "READY")
        self.assertEqual(tuple(f.db.execute("SELECT count(*),count(DISTINCT lease_id) FROM p0_roots "
                                           "WHERE task_id=?", (f.task,)).fetchone()), (1, 1))
        self.assertEqual(f.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        # Cold recovery after the resume receipt must select the same in-progress turn.
        # No worker Job is created by redispatch. Once legitimately parked, its result is reused.
        f.roots.delivery.observe("turn/started", {"threadId": f.thread, "turn": {"id": resumed}})
        with closing(store.open_store(f.directory / "gx.db")) as reopened:
            cold = roots.Roots(reopened, run_id=f.run, cwd=f.directory)
            cold.loaded.add(f.thread)
            cold._request = f.roots._request
            self.assertEqual(cold.dispatch(f.task)["turn_id"], resumed)
        self.assertEqual(len(f.peer.sent), 2)
        f.job()
        f.finish_job()
        for _ in range(2):
            self.tick()
        f.assert_wake_once()
        self.assertEqual(len(f.peer.sent), 3)
        with self.assertRaises(HoldActive):
            f.roots._claim(sibling)
        with store.transaction(f.db):
            v6_goal.cancel_decision(f.db, f.run, f.task, reason="No further work on this route")
        self.finish(f.peer.turns[-1])
        self.assertEqual(f.roots._claim(sibling)["attempt"], 1)
        f.assert_result()

    def test_resume_keeps_active_turn_then_honors_revocation(self):
        f = self.f
        original = self.start_and_stop()
        self.run_after_resume()
        self.assertEqual(f.peer.turns, [original])
        self.assertIsNone(f.roots.delivery._get("resume:" + original["id"]))
        self.control("stop")
        self.finish(original)
        with store.transaction(f.db):
            v6_goal.cancel_decision(f.db, f.run, f.task, reason="PI superseded task during pause")
        self.run_after_resume()
        self.assertEqual(len(f.peer.sent), 1)
        row = f.db.execute("SELECT status,attempts_consumed FROM tasks WHERE task_id=?", (f.task,)).fetchone()
        self.assertEqual(tuple(row), ("CANCELLED", 0))
        self.assertIsNone(f.roots.delivery._get("resume:" + original["id"]))

    def test_unacknowledged_continuation_uses_original_host_receipt(self):
        f = self.f
        original = self.start_and_stop()
        self.finish(original)
        self.control("resume")
        key = "resume:" + original["id"]
        f.peer.drop = True
        with self.assertRaises(EOFError):
            f.roots.dispatch(f.task)
        f.peer.drop = False
        self.assertEqual(f.roots.delivery._get(key)["state"], "sending")
        resumed = f.peer.turns[-1]
        content = resumed["items"]
        resumed["items"] = []
        with self.assertRaises(OutcomeUnknown):
            f.roots.dispatch(f.task)
        self.assertEqual(len(f.peer.sent), 2)
        resumed["items"] = content
        for _ in range(3):
            self.tick()
        saved = f.roots.delivery._get(key)
        self.assertEqual((saved["turn_id"], saved["state"]), (resumed["id"], "acknowledged"))
        self.assertEqual(len(f.peer.sent), 2)
        f.assert_owned()

    def test_second_stop_at_host_read_preserves_the_same_driver_and_attempt(self):
        f = self.f
        original = self.start_and_stop()
        self.finish(original)
        self.control("resume")
        resumed_key = "resume:" + original["id"]
        request = f.peer._request
        second_stop = []

        def stop_after_read(method, params):
            response = request(method, params)
            if method == "thread/read" and not second_stop:
                self.assertFalse(f.db.in_transaction)
                self.assertEqual(params["threadId"], f.thread)
                second_stop.append(self.control("stop")["status"])
            return response

        with patch.object(f.peer, "_request", side_effect=stop_after_read):
            for _ in range(3):
                self.tick()
                if second_stop:
                    break
        self.assertEqual(second_stop, ["PAUSED"])
        self.assertIsNone(f.roots.delivery._get(resumed_key))
        self.assertEqual(len(f.peer.sent), 1)
        f.assert_owned()
        self.run_after_resume()
        receipt = f.roots.delivery._get(resumed_key)
        self.assertEqual((receipt["state"], receipt["turn_id"]), ("acknowledged", f.peer.turns[-1]["id"]))
        self.assertEqual(len(f.peer.sent), 2)
        f.assert_owned()

    def test_recorded_terminal_product_suppresses_continuation(self):
        f = self.f
        original = self.start_and_stop()
        # Preserve the observed terminal product while its task is still selected
        # by the Driver, as can happen across separate receipt and poll boundaries.
        with store.transaction(f.db):
            TerminalProducts(f.roots).record(f.thread, original["id"], "accepted-call", "GX_ACCEPTED")
        self.finish(original)
        self.run_after_resume()
        self.assertEqual(len(f.peer.sent), 1)
        self.assertIsNone(f.roots.delivery._get("resume:" + original["id"]))
