"""Original ledger receipts remain authoritative before and after a module closes."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from gx_core import budgets
from gx_core.errors import BudgetExceeded
from gx_core.p0 import schema, v62_budget
from gx_core.p0.meter_limits import subtree_balances
from gx_core.persistence import records, store


class ModuleFunding(unittest.TestCase):
    def setUp(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.conn = self.enterContext(closing(store.open_store(state / "gx.db")))
        schema.install(self.conn)
        self.limits = {"api_calls": 3, "tokens": 100, "job_submissions": 2, "cpu_seconds": 60}
        self.parent = budgets.create_root(self.conn, run_id=None, profile_name="p0",
                                          actor="pi", limits=self.limits)
        self.run = records.create_run(self.conn, objective="Inventory public local data",
                                      project_ref="module-funding", actor="pi", budget_ref=self.parent)

    def open(self, name, **limits):
        with store.transaction(self.conn, transaction_owner="Test", operation="open", run_id=self.run):
            return v62_budget.open_child(self.conn, self.run, name, limits, "lead")

    def test_concurrent_obligations_cannot_overallocate_and_uncertain_work_stays_held(self):
        cid = self.open("a", api_calls=1, tokens=60, job_submissions=1, cpu_seconds=35)
        with self.assertRaises(BudgetExceeded):
            self.open("b", tokens=41)
        peer = self.open("b", api_calls=1, tokens=40, job_submissions=1, cpu_seconds=25)
        self.assertEqual(v62_budget.remaining(self.conn, self.parent)["tokens"], 0)
        with self.assertRaises(BudgetExceeded):
            budgets.reserve(self.conn, budget_ref=self.parent, subject_kind="native_turn",
                            subject_id="parent-call", amounts={"tokens": 1}, actor="lead")
        r = budgets.reserve(self.conn, budget_ref=cid, subject_kind="native_turn",
                            subject_id="turn", amounts={"tokens": 60, "api_calls": 1}, actor="host")
        mod = {"run_id": self.run, "budget_ref": cid, "branch_id": "a"}
        self.assertIsNone(v62_budget.exhausted(self.conn, mod))
        self.assertFalse(v62_budget.close_if_settled(self.conn, cid))
        _, spent, held = subtree_balances(self.conn, self.parent, self.limits)
        self.assertEqual((spent["tokens"], held["tokens"], held["api_calls"]), (0, 60, 1))
        budgets.settle(self.conn, r["reservation_ref"], actual={"tokens": 54, "api_calls": 1},
                       actor="host")
        self.assertIn("api_calls", v62_budget.exhausted(self.conn, mod))
        self.assertTrue(v62_budget.close_if_settled(self.conn, cid))
        self.assertTrue(v62_budget.close_if_settled(self.conn, cid))
        self.assertEqual(budgets.state(self.conn, self.parent)["spent"]["tokens"], 54)
        self.assertEqual(v62_budget.remaining(self.conn, self.parent)["tokens"], 6)
        # An official correction after closure has the same original reservation and propagates once.
        for _ in range(2):
            budgets.adjust_settled_tokens(self.conn, r["reservation_ref"], delta=8,
                                          adjustment_id="official-after-close", actor="meter")
        self.assertEqual(budgets.state(self.conn, cid)["spent"]["tokens"], 62)
        _, spent, held = subtree_balances(self.conn, self.parent, self.limits)
        self.assertEqual((spent["tokens"], spent["api_calls"], held["tokens"]), (62, 1, 0))
        self.assertEqual(budgets.state(self.conn, peer)["spent"], {})

    def test_open_child_preserves_unused_peer_room_when_a_sibling_has_an_unrolled_overrun(self):
        first = self.open("first", api_calls=1, tokens=40)
        second = self.open("second", api_calls=1, tokens=40)
        turn = budgets.reserve(self.conn, budget_ref=first, subject_kind="native_turn", subject_id="overshoot",
                               amounts={"api_calls": 1, "tokens": 40}, actor="host")
        budgets.settle(self.conn, turn["reservation_ref"], actual={"api_calls": 1, "tokens": 58},
                       actor="provider")
        self.assertEqual(v62_budget.remaining(self.conn, self.parent)["tokens"], 20)
        # Actual 58 + the second module's reserved 40 leaves only two unallocated.
        with self.assertRaises(BudgetExceeded):
            self.open("third", tokens=3)
        self.open("third", tokens=2)
        self.assertEqual(v62_budget.remaining(self.conn, second)["tokens"], 40)

    def test_zero_remaining_native_calls_can_still_close_with_measured_job_receipts(self):
        child = self.open("partial", api_calls=0, tokens=0, job_submissions=1, cpu_seconds=30)
        job = budgets.reserve(self.conn, budget_ref=child, subject_kind="job", subject_id="j",
                              amounts={"job_submissions": 1, "cpu_seconds": 30}, actor="jobs")
        mod = {"run_id": self.run, "budget_ref": child, "branch_id": "partial"}
        self.assertIsNone(v62_budget.exhausted(self.conn, mod))
        budgets.settle(self.conn, job["reservation_ref"], actual={"job_submissions": 1, "cpu_seconds": 1},
                       actor="jobs")
        self.assertIn("api_calls", v62_budget.exhausted(self.conn, mod))
        self.assertTrue(v62_budget.close_if_settled(self.conn, child))
        self.assertEqual(budgets.state(self.conn, self.parent)["spent"],
                         {"job_submissions": 1, "cpu_seconds": 1})


class ColdEvidenceDispatch(unittest.TestCase):
    def test_previously_admitted_job_waits_when_its_observation_is_damaged_then_launches_once(self):
        import json
        from unittest.mock import patch
        from gx_core.artifacts import ArtifactService
        from gx_core.jobs import executor
        from gx_core.jobs.service import JobService
        from gx_core.p0 import v6_jobs
        from tests.test_v6_jobs import DynamicJobs

        jobs = DynamicJobs()
        self.addCleanup(jobs.doCleanups)
        jobs.setUp()
        jobs.new("measurement")
        result_ref = jobs.settle("measurement")["result_ref"]
        artifacts = ArtifactService(jobs.conn)
        result = artifacts.read_json(result_ref)
        key, value = next((k, v) for k, v in result.items() if isinstance(v, str))
        candidate = jobs.new("conditional", operation="followup")
        candidate["depends_on"] = ["measurement"]
        candidate["evidence_gate"] = {"producer_node": "measurement", "producer_version": 1,
                                      "source_ref": jobs.art, "pointer": "/" + key,
                                      "equals": value, "require_lead_review": False}
        jobs.conn.execute("UPDATE v6_nodes SET spec_json=? WHERE node_id='conditional'", (json.dumps(candidate),))
        with patch.object(JobService, "_start", return_value={"replayed": False}):
            v6_jobs.advance(jobs.conn, jobs.run_id)
        assigned = jobs.state("conditional")["job_id"]
        self.assertIsNotNone(assigned)
        meta = artifacts.describe(result_ref)
        path = artifacts.resolve_path(meta["namespace"], meta["rel_path"])
        original = path.read_bytes()
        jobs.conn.close()
        jobs.connect()
        path.write_bytes(b'{}')
        with patch.object(executor, "launch", side_effect=AssertionError("stale observation launched a Job")):
            v6_jobs.advance(jobs.conn, jobs.run_id)
        self.assertEqual(jobs.conn.execute("SELECT status FROM jobs WHERE job_id=?", (assigned,)).fetchone()[0],
                         "CREATED")
        path.write_bytes(original)
        self.assertEqual(jobs.settle("conditional")["job_id"], assigned)
        self.assertEqual(jobs.state("conditional")["status"], "SUCCEEDED")
        self.assertEqual(jobs.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 2)
