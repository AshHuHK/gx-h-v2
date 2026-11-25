"""Decision wakeups, input provenance and checkpoint resolution on a real GX store."""

import hashlib
import json
from contextlib import closing
from unittest.mock import patch
from types import SimpleNamespace
import unittest

from gx_core import budgets
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import IdempotencyConflict, SchemaInvalid, VersionImmutable
from gx_core.orchestration import service
from gx_core.orchestration.engine import StoreCheckpointer
from gx_core.p0 import v6_decisions, v6_goal
from gx_core.p0.driver import context
from gx_core.persistence import store
from tests import test_v6_native


class GoalRuntime(unittest.TestCase):
    def setUp(self):
        self.native = test_v6_native.ScientificLead()
        self.addCleanup(self.native.doCleanups)
        self.native.setUp()
        self.db, self.rid = self.native.conn, self.native.run

    def decide(self, body):
        return v6_decisions.adopt(
            self.db,
            run_id=self.rid,
            thread_id=self.native.thread,
            turn_id=self.native.turn,
            task_id=self.native.task,
            lease_id=self.native.row["lease_id"],
            call_id="test-call",
            decision=body,
        )

    def publish(self, label):
        return (
            ArtifactService(self.db)
            .put(
                run_id=self.rid,
                kind="synthetic_observation",
                content=json.dumps({"measurement": label}),
                media_type="application/json",
                origin="HOST_PUT",
                put_by="researcher",
            )
            .artifact_ref
        )

    def test_resolve_then_two_sources_freeze_once_with_real_snapshot_and_aliases(self):
        receipt = self.decide(self.native.body)
        self.assertIsNone(receipt["result_head"])
        self.assertIsNone(v6_goal.ensure_task(self.db, self.rid))
        a, b = self.publish(7), self.publish(9)
        first = v6_goal.record_observation(self.db, self.rid, a, source="source-a")
        self.assertEqual(first, v6_goal.record_observation(self.db, self.rid, a, source="source-a"))
        with self.assertRaises(IdempotencyConflict):
            v6_goal.record_observation(self.db, self.rid, b, source="source-a")
        with self.assertRaises(SchemaInvalid):
            v6_goal.record_observation(
                self.db, self.rid, b, source="unverified", observation={"measurement": 55}
            )
        second = v6_goal.record_observation(self.db, self.rid, b, source="source-b")
        task = v6_goal.ensure_task(self.db, self.rid)
        self.assertIsNotNone(task)
        self.assertIsNone(v6_goal.ensure_task(self.db, self.rid))
        context_row = self.db.execute(
            "SELECT c.* FROM research_contexts c JOIN v6_decision_tasks d "
            "USING(context_ref) WHERE d.task_id=?",
            (task,),
        ).fetchone()
        brief = store.loads(context_row, "snapshot_json")
        self.assertEqual([x["observation"]["measurement"] for x in brief["observations"]], [7, 9])
        self.assertEqual(set(brief["evidence_refs"]), {a, b, "source-a", "source-b"})
        self.assertEqual(context_row["sha256"], ids.sha256_of_obj(brief))
        self.assertEqual(context_row["event_cursor"], second["event_id"])
        self.assertIsNone(store.latest_checkpoint(self.db, self.rid))
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM jobs WHERE run_id=?", (self.rid,)).fetchone()[0], 0
        )

    def test_evidence_arriving_during_capture_stays_for_a_later_decision(self):
        self.decide(self.native.body)
        a, b = self.publish(31), self.publish(32)
        v6_goal.record_observation(self.db, self.rid, a, source="earlier")
        capture = v6_goal._brief
        filename = self.db.execute("PRAGMA database_list").fetchone()["file"]
        with closing(store.open_store(filename)) as writer:

            def simultaneously(*args):
                v6_goal.record_observation(writer, self.rid, b, source="during-capture")
                return capture(*args)

            with patch.object(v6_goal, "_brief", side_effect=simultaneously):
                task = v6_goal.ensure_task(self.db, self.rid)
        data = store.loads(
            self.db.execute("SELECT input_json FROM tasks WHERE task_id=?", (task,)).fetchone(), "input_json"
        )
        self.assertEqual(data["brief"]["evidence_refs"], [a, "earlier"])
        roots = self.native.roots
        row = roots._claim(task)
        thread = roots._bind(row, {"id": "second-thread", "cwd": row["cwd"], "source": "appServer"})
        turn = "second-turn"
        roots.active_turns[thread] = turn
        self.db.execute(
            """INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,
            human,interrupt,state,method,turn_id) VALUES(?,'second-start',?,'two',?,0,0,'acknowledged','turn/start',?)""",
            (self.rid, thread, hashlib.sha256(row["brief"].encode()).hexdigest(), turn),
        )
        v6_decisions.adopt(
            self.db,
            run_id=self.rid,
            task_id=task,
            lease_id=row["lease_id"],
            thread_id=thread,
            turn_id=turn,
            call_id="second-decision",
            decision={**self.native.body, "evidence_refs": [a]},
        )
        successor = v6_goal.ensure_task(self.db, self.rid)
        fresh = store.loads(
            self.db.execute("SELECT input_json FROM tasks WHERE task_id=?", (successor,)).fetchone(),
            "input_json",
        )["brief"]
        self.assertEqual([o["id"] for o in fresh["observations"]], ["during-capture"])
        self.assertEqual(set(fresh["evidence_refs"]), {a, b, "earlier", "during-capture"})

    def test_budget_wakes_on_actual_limit_change_not_spending_or_unrelated_account(self):
        body = {**self.native.body, "reconsider_when": [{"event": "budget_changed", "subject_ref": "run"}]}
        self.decide(body)
        a = self.publish(5)
        v6_goal.record_observation(self.db, self.rid, a, source="neutral")
        with store.transaction(self.db):
            other = budgets.create_root(self.db, run_id=None, profile_name="p0", actor="fixture")
            store.append_event(
                self.db,
                kind="budget.tightened",
                actor="fixture",
                subject_kind="budget",
                subject_id=other,
                payload={"limits": {"tokens": 1}},
            )
        self.assertIsNone(v6_goal.ensure_task(self.db, self.rid))
        run = self.db.execute("SELECT * FROM runs WHERE run_id=?", (self.rid,)).fetchone()
        account = budgets.state(self.db, run["budget_ref"])
        old = account["limits"]["tokens"]
        budgets.tighten(
            self.db, run["budget_ref"], limits={"tokens": old - 1}, actor="operator", reason="new budget"
        )
        task = v6_goal.ensure_task(self.db, self.rid)
        self.assertIsNotNone(task)
        brief = store.loads(
            self.db.execute("SELECT input_json FROM tasks WHERE task_id=?", (task,)).fetchone(), "input_json"
        )["brief"]
        self.assertEqual(brief["budget"]["limits"]["tokens"], old - 1)
        self.assertIsNone(v6_goal.ensure_task(self.db, self.rid))

    def test_amended_execution_graph_resolves_from_checkout_independent_data(self):
        operation = {
            "op": "open_branch",
            "branch": "local:analysis",
            "hypothesis": "The two measures agree",
            "method_family": "numerical comparison",
            "alternative": "They differ",
            "discriminating_observation": "difference in the measured value",
            "budget_limits": {"job_submissions": 1},
            "stop_condition": "A comparison is recorded",
        }
        receipt = self.decide({**self.native.body, "mode": "amend", "ops": [operation]})
        cp = StoreCheckpointer().latest(self.db, self.rid)
        self.assertEqual(cp.state["head"], receipt["result_head"])
        branch = receipt["aliases"]["local:analysis"]
        self.assertIn(branch, v6_goal.instance(self.db, self.rid, cp)["branches"])
        invalid = SimpleNamespace(
            workflow_id=cp.workflow_id,
            workflow_version=cp.workflow_version,
            state={**cp.state, "head": {"version": 1, "sha256": "0" * 64}},
        )
        with self.assertRaises(VersionImmutable):
            v6_goal.instance(self.db, self.rid, invalid)
        engine = service._engine(context(self.db), self.rid)
        self.assertEqual(engine.state(self.rid)["workflow_id"], "gx.v6.instance")
        engine.advance(run_id=self.rid)
        self.assertEqual(store.latest_checkpoint(self.db, self.rid)["workflow_version"], "1")
