"""An exhausted submission line still permits review of computations already paid for."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gx_core import budgets
from gx_core.errors import BudgetExceeded, DependencyUnmet
from gx_core.jobs.geometry_workflow import submit_quality
from gx_core.jobs.service import JobService
from gx_core.native_release import native_state
from gx_core.orchestration import reactor, service
from gx_core.p0 import cli
from gx_core.p0.driver import context, project
from gx_core.persistence import records, store
from tests.test_tl1a_geometry import OfflineIngress, submit_peer
from workflows.tl1a_geometry import domain


class ResourceDimensions(unittest.TestCase):
    def test_specific_job_refusal_survives_resume_until_the_project_phase_changes(self):
        with tempfile.TemporaryDirectory() as tmp, native_state(Path(tmp)) as state, patch(
                "gx_core.p0.roots.tempfile.gettempdir", return_value=tmp):
            with closing(store.open_store(state.db_path)) as db, patch.object(cli, "Driver", OfflineIngress):
                run = cli.start(db, Path(tmp), cli.geometry_spec())["run_id"]
                first = db.execute("SELECT job_id FROM jobs WHERE run_id=?", (run,)).fetchone()[0]
                JobService(db).wait(first, timeout_seconds=15)
                reactor.lazy(context(db), run)
                reactor.drive_science(context(db), run)
                planner = db.execute("SELECT * FROM tasks WHERE run_id=? AND status='READY' AND stage='proposal'",
                                     (run,)).fetchone()
                self.assertIsNotNone(planner)
                submit_peer(db, tmp, run, planner)
                caps = {"job_submissions":0, "cpu_seconds":2000, "worker_tasks":20, "gpu_jobs":0, "gpu_minutes":0}
                budgets.activate_project_budget(db, project(db)["project_ref"], caps, phase_id="no-new-computations")
                reactor.lazy(context(db), run)
                reactor.drive_science(context(db), run)
                row = project(db)
                self.assertEqual(row["status"], "PAUSED")
                needs = store.loads(row, "paused_json")["needs"]
                self.assertEqual([(n["kind"], n["subject"]["id"], n["subject"]["required"]) for n in needs],
                                 [("budget_line", "job_submissions", 1)])
            with closing(store.open_store(state.db_path)) as db:
                with self.assertRaises(DependencyUnmet):
                    cli.change_state(db, project(db), "continue")
                self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE run_id=?", (run,)).fetchone()[0], 1)
                budgets.activate_project_budget(db, project(db)["project_ref"], {**caps, "job_submissions":2},
                                                phase_id="two-new-computations")
                cli.change_state(db, project(db), "continue")
                jobs = db.execute("SELECT job_id FROM jobs WHERE run_id=?", (run,)).fetchall()
                self.assertEqual(len(jobs), 3)
                for (jid,) in jobs:
                    self.assertEqual(JobService(db).wait(jid, timeout_seconds=15)["status"], "SUCCEEDED")

    def test_non_p0_legacy_global_pause_and_other_p0_pause_still_require_their_recorded_need(self):
        with tempfile.TemporaryDirectory() as tmp, closing(store.open_store(Path(tmp) / "gx.db")) as db:
            for p0 in (False, True):
                run = records.create_run(db, objective="budget fixture", project_ref="local", actor="test",
                                         inputs={"inputs":{"p0":p0}})
                ref = budgets.create_root(db, run_id=run, actor="test", limits={"job_submissions":1})
                db.execute("UPDATE runs SET status='ACTIVE',budget_ref=? WHERE run_id=?", (ref,run))
                budgets.spend(db, budget_ref=ref, subject_kind="job", subject_id="original:" + run,
                              amounts={"job_submissions":1}, actor="test")
                paused = {"exhausted":["job_submissions"], "paused_by":"backend",
                          "reason":"budget exhausted: job_submissions", "continue_from":{"node":"experiment"}}
                receipt = {"exhausted":["job_submissions"], "budget":budgets.state(db,ref), "node":"experiment"}
                with store.transaction(db):
                    store.transition(db, machine="run", subject_id=run, to_state="PAUSED", actor="test",
                                     extra_updates={"paused_json": paused}, payload={"action":"pause"} if p0 else receipt)
                observed = db.execute("SELECT * FROM runs WHERE run_id=?", (run,)).fetchone()
                self.assertEqual([n["line"] for n in service._pause_needs(db, observed)], ["job_submissions"])

    def test_cold_resume_after_four_real_jobs_at_the_original_limit(self):
        with tempfile.TemporaryDirectory() as tmp, native_state(Path(tmp)) as state, patch(
                "gx_core.p0.roots.tempfile.gettempdir", return_value=tmp):
            spec = {**cli.geometry_spec(), "budget_profile": "p0_extended"}
            with closing(store.open_store(state.db_path)) as db, patch.object(cli, "Driver", OfflineIngress):
                run = cli.start(db, Path(tmp), spec)["run_id"]
            original_jobs = []
            refused_fifth = False
            for _ in range(16):
                with closing(store.open_store(state.db_path)) as db:
                    row = project(db)
                    owner = JobService(db)
                    jobs = [r[0] for r in db.execute("SELECT job_id FROM jobs WHERE run_id=? ORDER BY rowid", (run,))]
                    for jid in jobs:
                        self.assertEqual(owner.wait(jid, timeout_seconds=15)["status"], "SUCCEEDED")
                    if not original_jobs and len(jobs) == 4:
                        # Produce the actual checkpoint and transition written by the former
                        # unconditional pre-node check, after the last paid Job has returned.
                        original_jobs = jobs
                        engine = service._engine(context(db), run)
                        with store.transaction(db):
                            begun = engine.begin_resume(db, run_id=run, interrupt_ref=row["interrupt_ref"])
                        engine._pause_for_budget(run_id=run, workflow=begun["workflow"], state=begun["state"],
                                                 actor="backend", node="experiment", keys=["job_submissions"],
                                                 budget=engine._budget_snapshot(run))
                        continue
                    if row["status"] == "PAUSED":
                        self.assertFalse(refused_fifth, "resuming returned to the obsolete broad budget pause")
                        cli.change_state(db, row, "continue")
                        self.assertNotEqual(project(db)["status"], "PAUSED")
                        with self.assertRaises(BudgetExceeded) as refusal:
                            submit_quality(db, run_id=run, operation_id="a-new-fifth-operation",
                                           structure_ref=domain.source_artifact(db, run),
                                           binding={"workflow_id":"tl1a_geometry", "node_id":"quality"})
                        details = refusal.exception.details
                        self.assertIn("job_submissions", [r["key"] for r in details.get("shortfalls", [details])])
                        refused_fifth = True
                    reactor.lazy(context(db), run)
                    reactor.drive_science(context(db), run)
                    if project(db)["status"] == "COMPLETED":
                        break
                    for task in db.execute("SELECT * FROM tasks WHERE run_id=? AND status='READY'", (run,)).fetchall():
                        submit_peer(db, tmp, run, task)
            with closing(store.open_store(state.db_path)) as db:
                self.assertTrue(refused_fifth)
                self.assertEqual(project(db)["status"], "COMPLETED")
                self.assertEqual(len(cli.snapshot(db)["rounds"]), 2)
                self.assertEqual([r[0] for r in db.execute("SELECT job_id FROM jobs WHERE run_id=? ORDER BY rowid", (run,))],
                                 original_jobs)
                ledger = budgets.state(db, project(db)["budget_ref"])
                self.assertEqual((ledger["limits"]["job_submissions"], ledger["spent"]["job_submissions"]), (4, 4))
                self.assertEqual(db.execute("SELECT count(*) FROM tasks WHERE run_id=? AND stage IN ('assessment','decision')"
                                            " AND status='ACCEPTED'", (run,)).fetchone()[0], 4)
