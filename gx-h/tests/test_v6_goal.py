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


class GoalCapabilityAdmission(unittest.TestCase):
    """Existing registry states are fixtures; exercise the normal Goal and Job owners."""

    def setUp(self):
        from pathlib import Path
        import tempfile
        from gx_core.native_release import native_state

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.state = self.directory / "state"
        self.db_path = self.state / "gx.db"
        self.input = self.directory / "points.json"
        self.input.write_text(
            json.dumps(
                {
                    "fixture_version": "1",
                    "synthetic": True,
                    "method_id": "fixture-grid",
                    "method_version": "1",
                    "candidates": [{"candidate_id": "two", "x": 2}],
                }
            )
        )
        with native_state(self.state), closing(store.open_store(self.db_path)) as conn:
            from gx_core.capabilities import shipped

            shipped.ensure(context(conn), shipped.GRID)

    def register_fixture(self, name, *, active, package_owned):
        from gx_core.capabilities import registry, shipped

        with closing(store.open_store(self.db_path)) as conn:
            bundle = registry.candidate(conn, shipped.GRID)["bundle"]
            bundle["manifest"]["capability_id"] = name
            digest = shipped.portable_digest(bundle)
            package = (
                {"id": "org.fixture.range", "version": "1", "sha256": "a" * 64, "portable_sha256": digest}
                if package_owned
                else None
            )
            registered = registry.register_candidate(context(conn), bundle, installed_package=package)
            # The registration owner starts at DRAFT. Seed the existing ACTIVE asset state
            # separately from admission; these tests do not exercise governance approval.
            if active:
                conn.execute(
                    "UPDATE assets SET status='ACTIVE' WHERE kind='TOOL_ADAPTER' AND asset_id=? AND version='1'",
                    (name,),
                )
            return digest, registered

    def start(self, name, **kwargs):
        from tests.test_v6_operator_recovery import OperatorRecovery

        return OperatorRecovery.cli(
            self,
            "start",
            "--run-key",
            name,
            "--goal",
            "Compute one grid observation.",
            "--capability",
            name + "@1",
            "--input",
            str(self.input),
            **kwargs,
        )

    def test_default_registry_state_and_package_composition_are_checked_before_goal_creation(self):
        self.register_fixture("pending-range", active=False, package_owned=False)
        self.assertIn("not ACTIVE", self.start("pending-range", expected=1))
        self.register_fixture("packaged-range", active=True, package_owned=True)
        self.assertIn("outside this Run composition", self.start("packaged-range", expected=1))
        with closing(store.open_store(self.db_path)) as conn:
            for table in ("runs", "v6_goals", "v6_decision_tasks", "jobs", "budget_reservations"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_approved_workspace_capability_keeps_exact_pin_through_real_job(self):
        import time
        from gx_core.p0 import v6_jobs
        from tests import test_v6_operator_recovery as fixture

        name = "workspace-range"
        digest, registered = self.register_fixture(name, active=True, package_owned=False)
        result = self.start(name)
        self.run_id = result["run_id"]
        self.assertEqual(result["capabilities"], {name + "@1": digest})
        with closing(store.open_store(self.db_path)) as conn:
            lead = fixture.OperatorRecovery.claim(self, conn)
            op = fixture.node("local:measure", name + "@1", result["materials"])
            op["parameters"] = {"operation": "grid-comparison"}
            v6_decisions.adopt(conn, **lead, decision=fixture.decision(fixture.branch("local:counts"), op))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                v6_jobs.advance(conn, self.run_id)
                row = conn.execute(
                    "SELECT job_id,status,result_ref FROM v6_nodes WHERE run_id=?", (self.run_id,)
                ).fetchone()
                if row["status"] in v6_jobs.NODE_TERMINAL:
                    break
                time.sleep(0.05)
            self.assertEqual(row["status"], "SUCCEEDED")
            job = conn.execute(
                "SELECT capability_id,capability_version,input_manifest_ref FROM jobs WHERE job_id=?",
                (row["job_id"],),
            ).fetchone()
            self.assertEqual((job["capability_id"], job["capability_version"]), (name, "1"))
            raw = ArtifactService(conn).read_json(row["result_ref"])
            self.assertEqual(raw["observations"][0]["score"], 0)
            execution = conn.execute(
                "SELECT identity_json FROM v6_job_attempts WHERE run_id=?", (self.run_id,)
            ).fetchone()
            self.assertEqual(json.loads(execution[0])["manifest_sha256"], registered["manifest_sha256"])
