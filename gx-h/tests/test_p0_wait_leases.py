"""System waits protect the scientific attempt while GX retains its original fenced lease."""
import json
import unittest
from pathlib import Path

from gx_core import budgets
from gx_core.domain import clock
from gx_core.errors import HoldActive, IllegalTransition, LeaseExpired, ScopeDenied
from gx_core.jobs.service import JobService
from gx_core.orchestration import reactor
from gx_core.p0 import intent, waits
from gx_core.p0.cli import change_state
from gx_core.p0.driver import context, project
from gx_core.p0.roots import Roots, require_binding
from gx_core.persistence import store
from gx_core.teams.service import default_lease_seconds
from tests import test_p0_roots, test_p0_root_delivery


class WaitLeases(unittest.TestCase):
    def setUp(self):
        fixture = test_p0_roots.DurableRoots()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(clock.reset)
        fixture.roots._request = fixture.fake
        self.binding = fixture.bind()
        self.db, self.run, self.task, self.roots = fixture.conn, fixture.run, fixture.task, fixture.roots
        self.directory = Path(fixture.tmp.name)
        self.thread = self.binding["thread_id"]
        self.db.execute("UPDATE runs SET inputs_json=?,status='WAITING_EXTERNAL_JOB' WHERE run_id=?",
                        (json.dumps({"inputs":{"p0": True}}), self.run))
        self.budget = budgets.create_root(self.db, run_id=self.run, profile_name="p0", actor="test",
                                          limits={"tokens": 100, "api_calls": 5, "job_submissions":2, "cpu_seconds":20})
        self.db.execute("UPDATE runs SET budget_ref=? WHERE run_id=?", (self.budget, self.run))
        self.peer = test_p0_root_delivery.Peer(self.db, self.run)
        self.attach()

    def attach(self):
        def exchange(method, params):
            result = self.peer._request(method, params)
            if method == "thread/read":
                result["thread"]["id"] = self.thread
            return result
        self.roots.loaded.add(self.thread)
        self.roots._request = exchange
        self.roots._goal_for_budget = lambda thread, amount: None

    def reopen(self):
        self.db.close()
        self.db = store.open_store(self.directory / "test.db")
        self.addCleanup(self.db.close)
        self.peer.conn = self.db
        self.roots = Roots(self.db, run_id=self.run, cwd=self.directory)
        self.attach()

    def job(self):
        self.db.execute(
            "INSERT INTO jobs(job_id,run_id,task_id,lease_ref,capability_id,capability_version,status,"
            "idempotency_key,submit_intent_json,input_manifest_ref,created_at) "
            "VALUES('external-1',?,?,?,'measurement','1','RUNNING','external-key','{}','original-input',?)",
            (self.run, self.task, self.binding["lease_id"], clock.now_iso()))
        with store.transaction(self.db):
            self.roots._park_job(self.binding, "external-1")

    def finish_job(self):
        jobs = JobService(self.db)
        with store.transaction(self.db) as tx:
            job = jobs._row("external-1")
            jobs._end(tx, job, "SUCCEEDED", reason="detached result observed",
                      extra_updates={"finished_at": clock.now_iso(), "outcome_json": {"result": "saved"}})
            if job["reservation_ref"]:
                resources = {"started":True, "cpu_user_seconds":1.4}
                budgets.settle(tx, job["reservation_ref"], actual=jobs._actual(resources, {}), actor="backend")
        self.assertEqual(jobs.status("external-1")["status"], "SUCCEEDED")

    def over_lease(self):
        clock.advance(default_lease_seconds() + 3601)

    def expiry(self):
        return self.db.execute("SELECT expires_at FROM leases WHERE lease_id=?",
                               (self.binding["lease_id"],)).fetchone()[0]

    def assert_owned(self):
        row = self.db.execute("SELECT status,attempt,lease_id,attempts_consumed FROM tasks WHERE task_id=?",
                              (self.task,)).fetchone()
        self.assertEqual(tuple(row), ("CLAIMED", self.binding["attempt"], self.binding["lease_id"], 0))

    def assert_result(self):
        job = self.db.execute("SELECT job_id,outcome_json FROM jobs").fetchall()
        self.assertEqual([(row[0], json.loads(row[1])) for row in job], [("external-1", {"result": "saved"})])
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox WHERE topic='job.terminal'").fetchone()[0], 1)

    def assert_wake_once(self):
        self.assert_owned()
        event = f"wake:{self.binding['lease_id']}:external-1"
        delivery = self.roots.delivery._get(event)
        self.assertEqual((delivery["thread_id"], delivery["state"]), (self.thread, "acknowledged"))
        submitted = len(self.peer.sent)
        self.assertEqual(self.roots.wake_job(self.task, "external-1")["turn_id"], delivery["turn_id"])
        self.assertEqual(len(self.peer.sent), submitted)
        self.assert_result()
        self.assertEqual(require_binding(self.db, self.run, self.thread)["attempt"], self.binding["attempt"])

    def test_terminal_handoff_is_atomic_and_repeated_observation_does_not_extend_it(self):
        self.job()
        self.over_lease()
        self.finish_job()
        deadline = self.expiry()
        self.assertFalse(clock.is_past(deadline))
        for _ in range(2):
            clock.advance(1)
            JobService(self.db).status("external-1")
            reactor.lazy(context(self.db), self.run)
            self.assert_owned()
            self.assertEqual(self.expiry(), deadline)
        self.assert_result()

    def test_cold_terminal_handoff_after_second_deadline_only_renews_at_admitted_wake(self):
        self.job()
        self.over_lease()
        self.finish_job()
        deadline = self.expiry()
        self.over_lease()
        self.reopen()
        for _ in range(2):
            reactor.lazy(context(self.db), self.run)
            self.assert_owned()
            self.assertEqual(self.roots._claim(self.task)["root_key"], self.binding["root_key"])
            self.assertEqual(self.expiry(), deadline)
            with self.assertRaises(LeaseExpired):
                require_binding(self.db, self.run, self.thread)
        self.assertEqual(self.peer.sent, [])
        self.roots.dispatch(self.task)
        self.assert_wake_once()
        self.assertEqual(len(self.peer.sent), 1)
        events = self.db.execute("SELECT payload_json FROM events WHERE kind='task.wait_handoff' ORDER BY event_id")
        handoffs = [json.loads(row[0]) for row in events]
        self.assertEqual([row["phase"] for row in handoffs], ["job_terminal", "wake_after_downtime"])
        self.assertTrue(all(row["lease_id"] == self.binding["lease_id"] and row["job_id"] == "external-1"
                            for row in handoffs))

    def test_budget_contention_keeps_overdue_terminal_result_until_wake_can_be_paid(self):
        self.job()
        held = budgets.reserve(self.db, budget_ref=self.budget, subject_kind="native_turn", subject_id="other-turn",
                               amounts={"api_calls":1, "tokens":100}, actor="test")["reservation_ref"]
        self.over_lease()
        self.finish_job()
        for _ in range(2):
            with self.assertRaises(HoldActive) as blocked:
                self.roots.dispatch(self.task)
            self.assertEqual(blocked.exception.details["reason"], "budget_reserved")
            self.over_lease()
            deadline = self.expiry()
            self.reopen()
            reactor.lazy(context(self.db), self.run)
            self.assert_owned()
            self.assert_result()
            self.assertEqual(self.expiry(), deadline)
            self.assertEqual(len(self.peer.sent), 0)
        budgets.settle(self.db, held, actual={"tokens":35, "api_calls":1}, actor="test")
        self.roots.dispatch(self.task)
        self.assert_wake_once()
        ledger = budgets.state(self.db, self.budget)
        self.assertEqual(ledger["spent"], {"tokens":35, "api_calls":1})
        self.assertEqual(ledger["reserved"], {"tokens":65, "api_calls":1})

    def test_operator_pause_with_meter_gap_and_terminal_job_survives_cold_resume(self):
        # Exercise the lease handoff while the original receipt is still within its grace period.
        self.db.execute("UPDATE runs SET inputs_json=json_set(inputs_json,'$.inputs.p0_spec.metering.grace_seconds',604800) "
                        "WHERE run_id=?", (self.run,))
        turn = self.roots.dispatch(self.task)["turn_id"]
        self.roots.delivery.observe("turn/started", {"threadId": self.thread, "turn":{"id":turn}})
        self.roots.delivery.observe("rawResponse/completed", {"threadId":self.thread, "turnId": turn,
                                    "responseId": "provider-1", "usage":{"totalTokens":30}})
        self.job()
        change_state(self.db, project(self.db), "pause")
        with self.assertRaises(ScopeDenied):
            require_binding(self.db, self.run, self.thread)
        self.peer.turns[0]["status"] = "completed"
        self.roots.delivery.observe("turn/completed", {"threadId": self.thread, "turn":self.peer.turns[0]})
        self.over_lease()
        self.finish_job()
        self.over_lease()
        self.reopen()
        reactor.lazy(context(self.db), self.run)
        self.assert_owned()
        self.assertEqual({row["reason"] for row in waits.pending(self.db, self.run)},
                         {"job", "operator_pause", "metering"})
        change_state(self.db, project(self.db), "continue")
        with self.assertRaises(HoldActive) as blocked:
            self.roots.dispatch(self.task)
        self.assertEqual(blocked.exception.details["reason"], "native_usage_pending")
        self.assertEqual(len(self.peer.sent), 1)
        self.over_lease()
        reactor.lazy(context(self.db), self.run)
        self.assert_owned()
        for _ in range(2):
            self.roots.delivery.observe("thread/tokenUsage/updated", {"threadId": self.thread, "turnId":turn,
                                        "tokenUsage":{"total":{"totalTokens":30}}})
        self.roots.dispatch(self.task)
        self.assert_wake_once()
        self.assertEqual(len(self.peer.sent), 2)
        self.assertEqual(budgets.state(self.db, self.budget)["spent"], {"tokens":30, "api_calls":1})
        self.assertEqual(waits.pending(self.db, self.run), [])

    def test_meter_wait_precedes_first_retry_and_authorizes_only_after_exact_charge(self):
        self.db.execute("UPDATE runs SET inputs_json=json_set(inputs_json,'$.inputs.p0_spec.metering.grace_seconds',604800) "
                        "WHERE run_id=?", (self.run,))
        turn = self.roots.dispatch(self.task)["turn_id"]
        self.roots.delivery.observe("turn/started", {"threadId": self.thread, "turn":{"id":turn}})
        self.roots.delivery.observe("rawResponse/completed", {"threadId":self.thread, "turnId":turn,
                                    "responseId":"paid-response", "usage":{"totalTokens":22}})
        self.peer.turns[0]["status"] = "interrupted"
        self.roots.delivery.observe("turn/completed", {"threadId":self.thread, "turn":self.peer.turns[0]})
        self.over_lease()
        self.reopen()
        reactor.lazy(context(self.db), self.run)
        with self.assertRaises(HoldActive) as blocked:
            self.roots.dispatch(self.task)
        self.assertEqual(blocked.exception.details["reason"], "native_usage_pending")
        self.assertEqual(len(self.peer.sent), 1)
        self.assert_owned()
        with self.assertRaises(LeaseExpired):
            require_binding(self.db, self.run, self.thread)
        self.roots.delivery.observe("thread/tokenUsage/updated", {"threadId":self.thread, "turnId":turn,
                                    "tokenUsage":{"total":{"totalTokens":22}}})
        self.roots.dispatch(self.task)
        self.assert_owned()
        self.assertEqual(len(self.peer.sent), 2)
        self.assertEqual(budgets.state(self.db, self.budget)["spent"], {"tokens":22, "api_calls":1})
        self.assertEqual(waits.pending(self.db, self.run), [])

    def test_turn_completed_during_operator_pause_resumes_once_without_job_or_failure(self):
        turn = self.roots.dispatch(self.task)["turn_id"]
        self.roots.delivery.observe("turn/started", {"threadId":self.thread, "turn":{"id":turn}})
        self.roots.delivery.observe("rawResponse/completed", {"threadId":self.thread, "turnId":turn,
                                    "responseId":"original-response", "usage":{"totalTokens":11}})
        self.roots.delivery.observe("thread/tokenUsage/updated", {"threadId":self.thread, "turnId":turn,
                                    "tokenUsage":{"total":{"totalTokens":11}}})
        change_state(self.db, project(self.db), "pause")
        self.peer.turns[0]["status"] = "completed"
        self.roots.delivery.observe("turn/completed", {"threadId":self.thread, "turn":self.peer.turns[0]})
        self.roots._ended_turn(self.thread, self.peer.turns[0])
        self.over_lease()
        self.reopen()
        change_state(self.db, project(self.db), "continue")
        reactor.lazy(context(self.db), self.run)
        self.assert_owned()
        receipt = self.roots.dispatch(self.task)
        self.assertNotEqual(receipt["turn_id"], turn)
        self.assertEqual(self.roots.delivery._get("resume:" + turn)["turn_id"], receipt["turn_id"])
        self.assertEqual(len(self.peer.sent), 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(budgets.state(self.db, self.budget)["spent"], {"tokens":11, "api_calls":1})

    def test_operator_stop_preserves_a_job_already_queued_for_external_execution(self):
        self.job()
        self.db.execute("UPDATE jobs SET status='QUEUED' WHERE job_id='external-1'")
        change_state(self.db, project(self.db), "pause")
        self.over_lease()
        self.reopen()
        self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], "QUEUED")
        self.assert_owned()
        change_state(self.db, project(self.db), "continue")
        self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], "QUEUED")
        self.assertEqual(self.peer.sent, [])

    def test_operator_stop_before_native_ack_recovers_the_same_interrupted_turn(self):
        self.peer.drop = True
        with self.assertRaises(EOFError):
            self.roots.dispatch(self.task)
        self.peer.drop = False
        initial = self.roots.delivery._get("start:" + self.binding["root_key"])
        self.assertEqual((initial["state"], initial["turn_id"]), ("sending", None))
        change_state(self.db, project(self.db), "pause")
        original_turn = self.peer.turns[0]
        original_turn["status"] = "completed"
        self.over_lease()
        self.reopen()
        self.roots.delivery.reconcile(self.thread)
        self.roots._ended_turn(self.thread, original_turn)
        self.assert_owned()
        self.assertTrue(waits.deferred_turn(self.db, self.binding["root_key"], original_turn["id"]))
        self.assertEqual(self.roots.delivery._get(initial["event_id"])["turn_id"], original_turn["id"])
        self.assertEqual(len(self.peer.sent), 1)

    def test_workflow_pause_uses_the_same_wait_even_when_no_job_or_operator_command_exists(self):
        with store.transaction(self.db):
            store.transition(self.db, machine="run", subject_id=self.run, to_state="PAUSED", actor="backend",
                             reason="the workflow needs the replacement criterion")
        self.over_lease()
        self.reopen()
        reactor.lazy(context(self.db), self.run)
        self.assert_owned()
        with self.assertRaises(ScopeDenied):
            require_binding(self.db, self.run, self.thread)
        change_state(self.db, project(self.db), "continue")
        self.assertEqual(require_binding(self.db, self.run, self.thread)["lease_id"], self.binding["lease_id"])
        self.assertEqual(waits.pending(self.db, self.run), [])
        self.assertEqual(self.peer.sent, [])

    def legacy_wait_schema(self):
        # A copy of the same business rows in their pre-migration representation.
        with store.transaction(self.db):
            self.db.execute("DROP TABLE p0_attempt_waits")
            self.db.execute("DELETE FROM schema_meta WHERE key='p0.migration.waits.sql'")

    def test_legacy_stopped_state_is_protected_before_reactor_and_reuses_original_lease(self):
        self.legacy_wait_schema()
        change_state(self.db, project(self.db), "pause")
        self.over_lease()
        self.reopen()
        reactor.lazy(context(self.db), self.run)
        self.assert_owned()
        change_state(self.db, project(self.db), "continue")
        self.assertEqual(require_binding(self.db, self.run, self.thread)["root_key"], self.binding["root_key"])
        self.assert_owned()

    def test_legacy_parked_terminal_keeps_reference_and_recovers_without_fabricating_start_time(self):
        self.job()
        self.legacy_wait_schema()
        self.over_lease()
        self.finish_job()
        self.reopen()
        self.assertEqual(waits.pending(self.db, self.run)[0]["wait_key"], "external-1")
        self.assertIsNone(waits.pending(self.db, self.run)[0]["started_at"])
        reactor.lazy(context(self.db), self.run)
        self.assert_owned()
        self.roots.dispatch(self.task)
        self.assert_wake_once()

    def test_terminal_transaction_rollback_leaves_original_wait_lease_and_job_untouched(self):
        self.job()
        self.over_lease()
        original_deadline = self.expiry()
        jobs = JobService(self.db)
        with self.assertRaisesRegex(RuntimeError, "discard transaction"):
            with store.transaction(self.db) as tx:
                jobs._end(tx, jobs._row("external-1"), "SUCCEEDED", reason="observed result",
                          extra_updates={"finished_at":clock.now_iso()})
                raise RuntimeError("discard transaction")
        self.assertEqual(self.expiry(), original_deadline)
        self.assertEqual(self.db.execute("SELECT status FROM jobs").fetchone()[0], "RUNNING")
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox WHERE topic='job.terminal'").fetchone()[0], 0)
        self.assertIsNone(waits.pending(self.db, self.run)[0]["ready_at"])
        self.finish_job()
        self.assert_result()

    def replace_objective(self):
        intent.bind_main(self.db, self.run, "operator")
        def submit(number, action):
            mid = f"human-{number}"
            intent.stage_input(self.db, self.run, mid, action)
            return intent.accept_input(self.db, self.run, "operator", f"human-turn-{number}", mid)
        def edit(version, value):
            return {"kind": "change", "items": [{"item_id": "target", "item_type": "objective",
                                                  "expected_version": version, "value": value}]}
        submit(1, edit(0, "measure the original observation"))
        intent.bind_dependencies(self.db, self.run, "task", self.task, {"target":"execution"})
        preview = submit(2, edit(1, "investigate the replacement observation"))
        confirmed = submit(3, {"kind": "confirm", "change_id": preview["change_id"],
                               "preview_sha256": preview["preview_sha256"]})
        self.assertEqual(confirmed["status"], "committed")

    def fenced_handoff(self, *, terminal_first):
        self.job()
        held = budgets.reserve(self.db, budget_ref=self.budget, subject_kind="job", subject_id="external-1",
                               amounts={"job_submissions":1,"cpu_seconds":5}, actor="test")
        self.db.execute("UPDATE jobs SET reservation_ref=? WHERE job_id='external-1'", (held["reservation_ref"],))
        self.over_lease()
        if terminal_first:
            self.finish_job()
        self.replace_objective()
        if not terminal_first:
            self.finish_job()
        original_expiry = self.expiry()
        self.over_lease()
        self.reopen()
        for _ in range(2):
            reactor.lazy(context(self.db), self.run)
            self.assertIsNone(waits.job_waiter(self.db, self.run, self.task, "external-1"))
            self.assertFalse(waits.is_waiting(self.db, self.binding["root_key"]))
            with self.assertRaises(IllegalTransition):
                self.roots.wake_job(self.task, "external-1")
            with self.assertRaises(ScopeDenied):
                require_binding(self.db, self.run, self.thread)
            self.assertEqual(self.expiry(), original_expiry)
        self.assert_result()
        self.assertEqual(self.peer.sent, [])
        ledger = budgets.state(self.db, self.budget)
        self.assertEqual(ledger["spent"], {"job_submissions":1, "cpu_seconds":2})
        self.assertEqual(ledger["reserved"], {})
        state = self.db.execute("SELECT status,lease_id,attempts_consumed FROM tasks WHERE task_id=?",
                                (self.task,)).fetchone()
        self.assertEqual(tuple(state), ("READY", None, 0))

    def test_intent_fence_commits_before_job_completion(self):
        self.fenced_handoff(terminal_first=False)

    def test_job_completion_commits_before_intent_fence(self):
        self.fenced_handoff(terminal_first=True)

    def test_silent_lease_lapse_without_system_wait_consumes_one_failure(self):
        self.over_lease()
        for _ in range(2):
            reactor.lazy(context(self.db), self.run)
        row = self.db.execute("SELECT status,lease_id,attempts_consumed FROM tasks WHERE task_id=?",
                              (self.task,)).fetchone()
        self.assertEqual(tuple(row), ("READY", None, 1))
        with self.assertRaises(ScopeDenied):
            require_binding(self.db, self.run, self.thread)
