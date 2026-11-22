"""Public P0 starts, atomic Intent projection and hierarchical workspace accounting."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from gx_core import budgets
from gx_core.errors import BudgetExceeded, IdempotencyConflict, SchemaInvalid
from gx_core.jobs.service import JobService
from gx_core.p0 import cli, intent, operator, schema, workspace
from gx_core.persistence import records, store


class WorkspaceTests(unittest.TestCase):
    project = "river-surveys"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.path = self.directory / "gx.db"
        self.conn = store.open_store(self.path)
        self.addCleanup(self.conn.close)

    def evidence(self, name, data):
        directory = os.environ.get("GX_P0_WORKSPACE_EVIDENCE")
        if directory:
            target = Path(directory)
            target.mkdir(exist_ok=True, parents=True)
            (target / (name + ".json")).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def member(self, key=None, *, conn=None, project=None, **spec):
        db, project = conn or self.conn, project or self.project
        with store.transaction(db):
            run = records.create_run(db, objective=f"Survey {key}", project_ref=project, actor="test",
                                     inputs={"inputs": {"p0": True, "p0_spec": spec}})
            ref = workspace.attach_run(db, run, project_ref=project, run_key=key, profile_name="p0",
                                       spec=spec, actor="test")
            db.execute("UPDATE runs SET budget_ref=? WHERE run_id=?", (ref, run))
        return run, ref

    def local(self, run, mid, *edits):
        action = {"kind": "change", "items": [{"item_id": key, "item_type": kind, "expected_version": version,
                                                "value": value} for key, kind, version, value in edits]}
        return operator.submit(operator.local_context(self.conn), run, action, mid)["result"]

    def wait_for_jobs(self):
        jobs = JobService(self.conn)
        for row in self.conn.execute("SELECT job_id FROM jobs").fetchall():
            self.assertTrue(jobs.wait(row["job_id"], 30)["terminal"])

    def test_public_keyed_starts_and_cold_replay(self):
        self.addCleanup(self.wait_for_jobs)
        spec = {**cli.demo_spec(), "project": self.project, "workspace": {"max_parallel_roots": 3}}
        spec_b = {**spec, "workflow_version": "1", "objective": "Check the second sampling grid independently.",
                  "proposal": {**spec["proposal"], "threshold": -0.5}}
        first = cli.start(self.conn, self.directory, spec, run_key="A")
        second = cli.start(self.conn, self.directory, spec_b, run_key="B")
        a, b = first["run_id"], second["run_id"]
        self.assertNotEqual(a, b)
        self.assertEqual(len(workspace.list_runs(self.conn)), 2)
        self.assertEqual(workspace.find_run(self.conn, run_key="B")["objective"], spec_b["objective"])
        self.assertEqual(workspace.max_parallel_roots(self.conn), 3)
        self.assertEqual(intent.read_item(self.conn, b, "objective")["value"], spec["objective"])
        self.assertEqual(intent.read_item(self.conn, a, "evaluation")["value"]["threshold"], -0.25)
        self.assertEqual(intent.read_item(self.conn, b, "evaluation")["value"]["threshold"], -0.5)
        with self.assertRaisesRegex(ValueError, "--run-id"):
            workspace.find_run(self.conn)
        with self.assertRaisesRegex(ValueError, "different"):
            cli.start(self.conn, self.directory, spec, run_key="B")
        before = self.conn.execute("SELECT count(*) FROM budget_reservations").fetchone()[0]
        with closing(store.open_store(self.path)) as cold:
            self.assertEqual(cli.start(cold, self.directory, spec_b, run_key="B")["run_id"], b)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM budget_reservations").fetchone()[0], before)
        rows = [dict(r) for r in self.conn.execute(
            "SELECT w.run_key,r.run_id,r.status,r.workflow_id,r.workflow_version,r.workflow_sha256,r.budget_ref,"
            "r.objective,b.parent_ref FROM runs r JOIN p0_workspace_runs w USING(run_id) "
            "JOIN budgets b ON b.budget_ref=r.budget_ref ORDER BY r.rowid")]
        self.assertEqual(rows[0]["parent_ref"], rows[1]["parent_ref"])
        self.assertNotEqual(rows[0]["budget_ref"], rows[1]["budget_ref"])
        self.assertEqual([r["workflow_version"] for r in rows], ["2", "1"])
        self.assertNotEqual(rows[0]["workflow_sha256"], rows[1]["workflow_sha256"])
        self.evidence("public-start", {"runs": rows, "cold_replay": b, "concurrency_limit": 3,
                    "objective_change_id": intent.read_item(self.conn, b, "objective")["change_id"],
                    "bootstrap_messages": self.conn.execute("SELECT COUNT(*) FROM p0_operator_messages").fetchone()[0]})

    def test_formal_project_changes_propagate_once_with_dependency_history(self):
        a, _ = self.member("A")
        initial = self.local(a, "establish", ("purpose", "objective", 0, "Measure rainfall."),
                             ("preference", "soft_preference", 0, "Show uncertainty."))
        b, _ = self.member("B")
        self.assertIsNone(self.conn.execute("SELECT thread_id FROM p0_intent_main WHERE run_id=?", (b,)).fetchone())
        original = intent.read_item(self.conn, b, "preference")
        self.assertEqual(original["change_id"], initial["change_id"])
        for run in (a, b):
            intent.bind_dependencies(self.conn, run, "workflow", run, {"preference": "context"})
        updated = self.local(b, "member-edit", ("preference", "soft_preference", 1, "Show prediction intervals."),
                             ("cutoff", "evaluation_spec", 0, {"threshold": 2}))
        self.assertEqual(updated["status"], "committed")
        self.assertEqual({v["object_id"] for v in updated["impact"]}, {a, b})
        for run in (a, b):
            item = intent.read_item(self.conn, run, "preference")
            self.assertEqual((item["version"], item["change_id"]), (2, updated["change_id"]))
            self.assertEqual(intent.read_item(self.conn, run, "preference", 1)["sha256"], original["sha256"])
            self.assertEqual(intent.snapshot(self.conn, run, "workflow", run)["preference"]["version"], 2)
        c, _ = self.member("C")
        self.assertEqual(intent.read_item(self.conn, c, "preference", 1)["sha256"], original["sha256"])
        self.assertNotIn("cutoff", intent.active_items(self.conn, c))
        self.assertNotIn("cutoff", intent.active_items(self.conn, a))
        self.assertIn("cutoff", intent.active_items(self.conn, b))
        changes = self.conn.execute("SELECT count(*) FROM p0_intent_changes WHERE change_id=?",
                                    (updated["change_id"],)).fetchone()[0]
        self.assertEqual(changes, 1)
        self.evidence("intent", {"origin": b, "late_member": c, "source_change_count": changes,
                      "change_id": updated["change_id"], "impact": updated["impact"],
                      "late_member_versions": [intent.read_item(self.conn, c, "preference", i) for i in (1, 2)]})

    def test_aggregate_reservations_overruns_and_terminal_release(self):
        a, ref_a = self.member("A", workspace_budget={"cpu_seconds": 12}, run_budget={"cpu_seconds": 8})
        b, ref_b = self.member("B", run_budget={"cpu_seconds": 4})
        root = budgets.state(self.conn, ref_a)["parent_ref"]
        raw = budgets.state(self.conn, root)
        self.assertEqual(raw["reserved"]["cpu_seconds"], 12)
        opening = workspace.budget_summary(self.conn)
        self.assertEqual((opening["available"]["cpu_seconds"], opening["unallocated"]["cpu_seconds"]), (12, 0))
        used, held = budgets._project_usage(self.conn, self.project)
        self.assertEqual((used["cpu_seconds"], held["cpu_seconds"]), (0, 0))
        with self.assertRaises(BudgetExceeded):
            self.member("C", run_budget={"cpu_seconds": 1})
        claim = budgets.reserve(self.conn, budget_ref=ref_a, subject_kind="work", subject_id="sampling",
                                amounts={"cpu_seconds": 7}, actor="test")
        self.conn.execute("UPDATE runs SET status='COMPLETED' WHERE run_id=?", (a,))
        workspace.release_finished(self.conn)
        self.assertTrue(budgets.state(self.conn, ref_a)["open"])
        budgets.settle(self.conn, claim["reservation_ref"], actual={"cpu_seconds": 9}, actor="test")
        with self.assertRaises(BudgetExceeded):
            budgets.reserve(self.conn, budget_ref=ref_b, subject_kind="work", subject_id="too-much",
                            amounts={"cpu_seconds": 4}, actor="test")
        budgets.spend(self.conn, budget_ref=ref_b, subject_kind="work", subject_id="comparison",
                      amounts={"cpu_seconds": 3}, actor="test")
        before_close = budgets._project_usage(self.conn, self.project)
        workspace.release_finished(self.conn)
        workspace.release_finished(self.conn)
        after_close = budgets._project_usage(self.conn, self.project)
        self.assertEqual(before_close, after_close)
        self.assertEqual((after_close[0]["cpu_seconds"], after_close[1]["cpu_seconds"]), (12, 0))
        self.assertFalse(budgets.state(self.conn, ref_a)["open"])
        self.assertEqual(budgets.state(self.conn, root)["spent"]["cpu_seconds"], 9)
        self.assertEqual(len(workspace.list_runs(self.conn)), 2)
        self.evidence("accounting", {"runs": [a, b], "workspace_budget": root, "subbudgets": [ref_a, ref_b],
                      "workspace_cpu_limit": 12, "internal_allocation": 12, "initial_usage": 0,
                      "initial_work_holds": 0, "first_actual": 9, "second_actual": 3,
                      "after_first_terminal_closed": {"spent": after_close[0], "reserved": after_close[1]},
                      "parent_rollup_cpu": budgets.state(self.conn, root)["spent"]["cpu_seconds"]})

    def test_conflicting_project_intent_and_limits_roll_back_the_candidate(self):
        a, _ = self.member("A", workspace={"max_parallel_roots": 2})
        cap = {"resource": "cpu_seconds", "max": 7}
        self.local(a, "limit", ("compute", "resource_limit", 0, cap))
        edit = {"item_id": "compute", "item_type": "resource_limit", "value": {**cap, "max": 9}}
        with self.assertRaisesRegex(SchemaInvalid, "conflicts with current project Intent"):
            self.member("B", intent_items=[edit])
        with self.assertRaisesRegex(SchemaInvalid, "existing limit"):
            self.member("B", workspace={"max_parallel_roots": 8})
        with self.assertRaises(IdempotencyConflict):
            self.member("A")
        self.assertEqual(len(workspace.list_runs(self.conn)), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM budgets").fetchone()[0], 2)
        self.assertEqual(intent.read_item(self.conn, a, "compute")["value"], cap)

    def test_a_project_intent_limit_applies_to_other_members_actual_holds(self):
        a, ref_a = self.member("A", workspace_budget={"cpu_seconds": 12}, run_budget={"cpu_seconds": 6})
        b, ref_b = self.member("B", run_budget={"cpu_seconds": 6})
        updated = self.local(b, "shared-limit", ("compute", "resource_limit", 0, {"resource": "cpu_seconds", "max": 5}))
        self.assertEqual(intent.read_item(self.conn, a, "compute")["change_id"], updated["change_id"])
        hold = budgets.reserve(self.conn, budget_ref=ref_a, subject_kind="job", subject_id="sample",
                               amounts={"cpu_seconds": 3}, actor="test")
        with self.assertRaises(BudgetExceeded) as refused:
            budgets.reserve(self.conn, budget_ref=ref_b, subject_kind="job", subject_id="independent",
                            amounts={"cpu_seconds": 3}, actor="test")
        self.assertEqual((refused.exception.details["reason"], refused.exception.details["reserved"],
                          refused.exception.details["available"]), ("current_intent", 3, 2))
        budgets.settle(self.conn, hold["reservation_ref"], actual={"cpu_seconds": 2}, actor="test")
        budgets.spend(self.conn, budget_ref=ref_b, subject_kind="job", subject_id="independent",
                      amounts={"cpu_seconds": 3}, actor="test")
        self.assertEqual(budgets._project_usage(self.conn, self.project)[0]["cpu_seconds"], 5)

    def test_competing_allocations_commit_only_one_run(self):
        self.member("anchor", workspace_budget={"job_submissions": 5}, run_budget={"job_submissions": 0})
        gate = threading.Barrier(2)
        def allocate(key):
            with closing(store.open_store(self.path)) as db:
                gate.wait(timeout=10)
                try:
                    self.member(key, conn=db, run_budget={"job_submissions": 4})
                    return "admitted"
                except BudgetExceeded:
                    return "refused"
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(allocate, ("A", "B"))), ["admitted", "refused"])
        self.assertEqual(len(workspace.list_runs(self.conn)), 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM budgets").fetchone()[0], 3)
        with self.assertRaises(SchemaInvalid):
            self.member("other-project", project="different")
        self.assertEqual(len(workspace.list_runs(self.conn)), 2)

    def test_legacy_read_only_and_adoption_preserve_spent_and_ids(self):
        old, original_ref = self.member()
        self.local(old, "old-intent", ("objective", "objective", 0, "Review both measurements."))
        budgets.spend(self.conn, budget_ref=original_ref, subject_kind="work", subject_id="existing",
                      amounts={"cpu_seconds": 2}, actor="test")
        self.conn.execute("DROP TABLE p0_workspace_runs")
        self.conn.execute("DROP TABLE p0_workspace")
        self.conn.execute("DELETE FROM schema_meta WHERE key='p0.migration.workspace.sql'")
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as readonly:
            readonly.row_factory = sqlite3.Row
            self.assertEqual(workspace.find_run(readonly)["run_id"], old)
            self.assertIsNone(workspace.find_run(readonly, run_key="B"))
            self.assertIsNone(workspace.key_for_run(readonly, old))
            self.assertEqual(workspace.max_parallel_roots(readonly), 10)
        schema.install(self.conn)
        b, child = self.member("B", run_budget={"cpu_seconds": 4})
        self.assertEqual(workspace.find_run(self.conn, run_id=old)["budget_ref"], original_ref)
        self.assertEqual(budgets.state(self.conn, child)["parent_ref"], original_ref)
        self.assertEqual(budgets._project_usage(self.conn, self.project)[0]["cpu_seconds"], 2)
        self.assertEqual(intent.read_item(self.conn, b, "objective")["source_message_id"], "old-intent")
        self.assertEqual(workspace.start_key(self.project), "p0-start:" + self.project)


if __name__ == "__main__":
    unittest.main()
