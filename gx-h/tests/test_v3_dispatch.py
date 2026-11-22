"""Admission, scientific refresh and per-task retry at the native Root boundary."""
from dataclasses import replace
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gx_core.errors import HoldActive
from gx_core.orchestration import service
from gx_core.orchestration.dispatch_preparation import DispatchPreparation
from gx_core.orchestration.hooks import DEFAULT_HOOKS
from gx_core.p0 import activity, driver, intent, roots
from gx_core.persistence import records, store
from gx_core.plugins.evaluator import plugin_ref
from tests import test_p0_review_sources, test_p0_roots, test_v21_teams
from workflows.compat_v20 import grid_dispatch


class ClaimAdmission(unittest.TestCase):
    def setUp(self):
        self.f = test_p0_roots.DurableRoots()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def test_denials_never_invoke_scientific_preparation_or_consume_an_attempt(self):
        f = self.f
        denied = HoldActive("capacity is in use")
        with patch.object(roots, "hooks_for_run", side_effect=AssertionError("ineligible task invoked a hook")):
            for owner in ("gx_core.p0.activity.require_provider_capacity", "gx_core.p0.team_admission.claim_capacity"):
                with self.subTest(owner=owner), patch(owner, side_effect=denied), self.assertRaises(HoldActive):
                    f.roots._claim(f.task)
            with patch.object(roots.scheduler, "admit_claim", return_value=SimpleNamespace(admitted=False, reason="capacity")), self.assertRaises(HoldActive):
                f.roots._claim(f.task)
            dependency = records.create_task(f.conn, run_id=f.run, actor="backend", kind="research_round", role="planner")
            f.conn.execute("INSERT INTO task_deps VALUES(?,?)", (f.task, dependency))
            self.assertIsNone(f.roots._claim(f.task))
        self.assertEqual(tuple(f.conn.execute("SELECT status,attempt,attempts_consumed,lease_id FROM tasks WHERE task_id=?",
                                              (f.task,)).fetchone()), ("READY",0,0,None))
        self.assertEqual(f.conn.execute("SELECT COUNT(*) FROM p0_roots").fetchone()[0], 0)

    def test_expired_attempt_settles_once_while_the_next_claim_waits(self):
        f = self.f
        f.roots._request = f.fake
        first = f.bind()
        f.conn.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00.000Z' WHERE lease_id=?",
                       (first["lease_id"],))
        denied = SimpleNamespace(admitted=False, reason="capacity")
        with patch.object(roots.scheduler, "admit_claim", return_value=denied), \
                patch.object(roots, "hooks_for_run", side_effect=AssertionError("unadmitted successor prepared")):
            self.assertIsNone(f.roots._claim(f.task))
            with self.assertRaises(HoldActive):
                f.roots._claim(f.task)
        task = f.conn.execute("SELECT status,attempt,attempts_consumed,lease_id FROM tasks WHERE task_id=?",
                              (f.task,)).fetchone()
        self.assertEqual(tuple(task), ("READY", 1, 1, None))
        self.assertIsNotNone(f.conn.execute("SELECT released_at FROM leases WHERE lease_id=?",
                                            (first["lease_id"],)).fetchone()[0])
        self.assertEqual(f.conn.execute("SELECT count(*) FROM leases WHERE task_id=?", (f.task,)).fetchone()[0], 1)

    def test_outside_preparation_loses_race_and_fresh_attempt_rechecks_eligibility(self):
        f = self.f
        calls = []
        second = store.open_store(f.conn.execute("PRAGMA database_list").fetchone()["file"])
        self.addCleanup(second.close)
        def hook(conn, run, task):
            self.assertTrue(conn.in_transaction)
            def prepare():
                self.assertFalse(conn.in_transaction)
                calls.append("prepare")
                if len(calls) == 1:
                    with store.transaction(second):
                        second.execute("UPDATE tasks SET input_json=? WHERE task_id=?", ('{"instruction":"updated"}', f.task))
                return task["input_json"]
            def apply(data):
                self.assertTrue(conn.in_transaction)
                self.assertEqual(task["input_json"], data)
                calls.append("apply")
                return True
            return DispatchPreparation((task["input_json"],), prepare, apply)
        hooks = replace(DEFAULT_HOOKS, prepare_dispatch=hook)
        with patch.object(roots, "hooks_for_run", return_value=hooks):
            self.assertIsNone(f.roots._claim(f.task))
            self.assertEqual(f.conn.execute("SELECT count(*) FROM leases WHERE task_id=?", (f.task,)).fetchone()[0], 0)
            binding = f.roots._claim(f.task)
        self.assertEqual(calls, ["prepare","prepare","apply"])
        self.assertEqual(binding["attempt"], 1)
        self.assertEqual(json.loads(binding["brief"].split("\n", 1)[1])["input"]["instruction"], "updated")

    def test_driver_retries_waiting_tasks_individually(self):
        f = self.f
        other = records.create_task(f.conn, run_id=f.run, actor="backend", kind="research_round", role="planner")
        store.transition(f.conn, machine="task", subject_id=other, to_state="READY", actor="backend", reason="fixture")
        attempted = []
        def dispatch(task):
            attempted.append(task)
            if task == f.task:
                raise HoldActive("waiting for a team slot")
            return {"turn_id":"original"}
        native = SimpleNamespace(dispatch=dispatch, poll=lambda delay: False, active_turns={})
        run_driver = driver.Driver(f.conn, f.tmp.name, f.run)
        run_driver._native = native
        for path in ("gx_core.p0.metering.maintain", "gx_core.p0.compute.drain", "gx_core.orchestration.reactor.lazy",
                     "gx_core.orchestration.reactor.drive_science"):
            self.enterContext(patch(path))
        for name in ("messages", "reconcile_costs", "deliveries"):
            self.enterContext(patch.object(run_driver, name))
        run = f.conn.execute("SELECT * FROM runs WHERE run_id=?", (f.run,)).fetchone()
        self.enterContext(patch.object(driver, "project", return_value=run))
        now = [50.0]
        self.enterContext(patch.object(driver.time, "monotonic", side_effect=lambda: now[0]))
        passes = run_driver.passes(55, shared=True)
        next(passes)
        next(passes)
        self.assertCountEqual(attempted, [f.task, other])
        for tick in range(1, 12):
            now[0] = 50 + 0.035 * tick
            next(passes)
        self.assertEqual(attempted.count(f.task), 1)
        now[0] = 50.51
        next(passes)
        self.assertEqual(attempted.count(f.task), 2)
        self.assertEqual(attempted.count(other), 1)
        passes.close()


class CapacityWake(unittest.TestCase):
    def setUp(self):
        self.f = test_v21_teams.WorkspaceTeams()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.now = 50.0
        self.attempts = []

    def drive(self, rid, waiting):
        f = self.f
        owner = f.roots(rid)
        def dispatch(tid):
            if tid not in waiting:
                return {"turn_id": "existing-" + tid}
            try:
                row = owner._claim(tid)
            except HoldActive as exc:
                self.attempts.append((tid, self.now, exc.details.get("scope", exc.details["reason"])))
                raise
            self.attempts.append((tid, self.now, "claimed"))
            return {"turn_id": row["lease_id"]}
        run_driver = driver.Driver(f.db, f.home, rid)
        run_driver._native = SimpleNamespace(dispatch=dispatch, poll=lambda delay: False, active_turns={})
        for path in ("gx_core.p0.metering.maintain", "gx_core.p0.compute.drain", "gx_core.orchestration.reactor.lazy",
                     "gx_core.orchestration.reactor.drive_science"):
            self.enterContext(patch(path))
        for name in ("messages", "reconcile_costs", "deliveries"):
            self.enterContext(patch.object(run_driver, name))
        self.enterContext(patch.object(driver.time, "monotonic", side_effect=lambda: self.now))
        self.passes = run_driver.passes(self.now + 5, shared=True)
        self.addCleanup(self.passes.close)
        next(self.passes)
        self.tick(50.0)

    def tick(self, at):
        self.now = at
        next(self.passes)

    def paid_turn(self, rid, tid):
        f = self.f
        root = f.roots(rid)
        row = f.occupy(root, tid, "root")
        peer = test_v21_teams.Peer(f.db, rid)
        root._request = peer._request
        root._goal_for_budget = lambda *args: None
        root.loaded.add("root")
        turn = root.dispatch(tid)["turn_id"]
        root.delivery.observe("turn/started", {"threadId":"root", "turn":{"id":turn}})
        def finish():
            peer.turns[0]["status"] = "completed"
            root.delivery.observe("turn/completed", {"threadId":"root", "turn":peer.turns[0]})
        return row, finish

    def test_only_the_freed_team_wakes_and_an_unfinished_turn_still_holds_its_slot(self):
        f = self.f
        rid, _ = f.run_case("teams", (test_v21_teams.group("reading", ["evidence_reviewer"]),
                                      test_v21_teams.group("analysis", ["planner"])), maximum=4)
        holder_a, waiting_a = [f.create(rid)["task_id"] for _ in range(2)]
        holder_b, waiting_b = [f.create(rid, role="planner")["task_id"] for _ in range(2)]
        _, finish = self.paid_turn(rid, holder_a)
        f.roots(rid)._claim(holder_b)
        self.drive(rid, {waiting_a, waiting_b})
        self.assertCountEqual(self.attempts, [(waiting_a,50.0,"team"), (waiting_b,50.0,"team")])
        f.yield_task(holder_a)
        self.tick(50.035)
        self.assertEqual(len(self.attempts), 2)
        f.yield_task(holder_b)
        self.tick(50.070)
        self.assertEqual(self.attempts[2:], [(waiting_b,50.070,"claimed")])
        self.tick(50.105)
        self.assertEqual(len(self.attempts), 3)
        finish()
        self.tick(50.140)
        self.assertEqual(self.attempts[3:], [(waiting_a,50.140,"claimed")])

    def test_other_run_releases_workspace_slot_before_retry_deadline(self):
        f = self.f
        first, _ = f.run_case("first", maximum=1)
        second, _ = f.run_case("next", maximum=1)
        holder = f.roots(first)._claim(f.create(first)["task_id"])
        waiting = f.create(second)["task_id"]
        self.drive(second, {waiting})
        self.tick(50.035)
        self.assertEqual(self.attempts, [(waiting,50.0,"workspace")])
        peer = store.open_store(f.home / "gx.db")
        self.addCleanup(peer.close)
        with store.transaction(peer):
            store.release_lease(peer, holder["lease_id"])
        self.tick(50.070)
        self.assertEqual(self.attempts[-1], (waiting,50.070,"claimed"))

    def test_other_run_finishes_provider_turn_before_retry_deadline(self):
        f = self.f
        self.enterContext(patch.object(activity, "_policy", return_value={"parallel_turns":{"openai":1}}))
        first, _ = f.run_case("first", maximum=3)
        second, _ = f.run_case("next", maximum=3)
        holder, finish = self.paid_turn(first, f.create(first)["task_id"])
        waiting = f.create(second)["task_id"]
        self.drive(second, {waiting})
        f.db.execute("INSERT INTO p0_root_turn_usage(thread_id,turn_id,terminal_status) VALUES('other','other','completed')")
        self.tick(50.035)
        self.assertEqual(self.attempts, [(waiting,50.0,"provider_backoff")])
        finish()
        self.tick(50.070)
        self.assertEqual(self.attempts[-1], (waiting,50.070,"claimed"))
        self.assertIsNone(f.db.execute("SELECT released_at FROM leases WHERE lease_id=?",
                                       (holder["lease_id"],)).fetchone()[0])


class GridDispatchPreparation(unittest.TestCase):
    def setUp(self):
        self.f = test_p0_review_sources.SourceDuringReview("test_source_commits_before_old_review_is_handed_in")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        def hooks(conn, run):
            return replace(service.hooks_for_run(conn, run), prepare_dispatch=grid_dispatch.prepare_dispatch)
        self.enterContext(patch.object(roots, "hooks_for_run", side_effect=hooks))

    def test_changed_criterion_runs_scorer_outside_writer_and_reuses_the_original_jobs(self):
        f = self.f
        task = f.task("evidence_reviewer")
        earlier = store.loads(task, "input_json")
        change = f.change("evaluation_spec", "evaluation", 1, {"threshold": 0.25, "evaluator": plugin_ref("1")})
        self.assertEqual(change["status"], "committed")
        scorer_calls = []
        real_run = subprocess.run
        def checked_run(command, *args, **kwargs):
            if command[0] == "node":
                self.assertFalse(f.conn.in_transaction)
                scorer_calls.append(json.loads(kwargs["input"])["attempt_id"])
            return real_run(command, *args, **kwargs)
        gateway = grid_dispatch.prepare_dispatch
        def interrupted(conn, run, row):
            request = gateway(conn, run, row)
            def prepare():
                request.prepare()
                raise SystemExit("lost coordinator before claim")
            return replace(request, prepare=prepare)
        with patch.object(subprocess, "run", side_effect=checked_run):
            with patch.object(grid_dispatch, "prepare_dispatch", side_effect=interrupted), \
                    self.assertRaisesRegex(SystemExit, "lost coordinator"):
                f.root._claim(task["task_id"])
            self.assertEqual(f.conn.execute("SELECT count(*) FROM leases WHERE task_id=?",
                                            (task["task_id"],)).fetchone()[0], 0)
            first_calls = tuple(scorer_calls)
            f.restart()
            binding = f.claim(f.task("evidence_reviewer"))
        self.assertEqual(tuple(scorer_calls), first_calls)
        packet = f.packet(binding)
        self.assertEqual(packet["intent"]["evaluation"]["version"], 2)
        self.assertNotEqual(packet["input"]["proposal_ref"], earlier["proposal_ref"])
        self.assertGreater(len(scorer_calls), 3)
        self.assertEqual(len(set(scorer_calls)), len(scorer_calls))
        self.assertEqual(f.count("jobs"), f.original_jobs)
        self.assertEqual(binding["attempt"], 1)

    def test_supplement_during_preparation_keeps_task_ready_for_current_context(self):
        f = self.f
        task = f.task("evidence_reviewer")
        first = grid_dispatch.prepare_dispatch
        second = store.open_store(f.state.db_path)
        self.addCleanup(second.close)
        source = []
        def changing(conn, run, row):
            result = first(conn, run, row)
            def prepare():
                updated = result.prepare()
                if not source:
                    intent.stage_input(second, run, "interleave", {"kind":"supplement","text":"Updated independent source"})
                    receipt = intent.accept_input(second, run, "main", "new-turn", "interleave")
                    source.append(receipt["source_id"])
                return updated
            return replace(result, prepare=prepare)
        with patch.object(grid_dispatch, "prepare_dispatch", side_effect=changing):
            self.assertIsNone(f.root._claim(task["task_id"]))
        saved = f.conn.execute("SELECT status,attempt,attempts_consumed,lease_id FROM tasks WHERE task_id=?",
                               (task["task_id"],)).fetchone()
        self.assertEqual(tuple(saved), ("READY",0,0,None))
        binding = f.claim(task)
        self.assertEqual([s["source_id"] for s in f.packet(binding)["input"]["supplement_sources"]], source)
        self.assertEqual(f.count("jobs"), f.original_jobs)
