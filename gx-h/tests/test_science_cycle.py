"""Durable scientist handoffs use GX submissions, original local Jobs and cold checkpoints."""
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import closing

from examples.science_cycle import OfflinePeer
from gx_core.artifacts import ArtifactService
from gx_core.native_release import native_state
from gx_core.orchestration import reactor
from gx_core.persistence import store
from gx_core.project_continuation import run_project
from gx_core.project_work import next_project_work
from gx_core.teams.service import backend_ctx

CLI = Path(__file__).resolve().parents[1] / "bin/gx-science"


class ScienceCycle(unittest.TestCase):
    def test_project_driver_reconciles_without_a_model_turn(self):
        with tempfile.TemporaryDirectory() as root:
            started = subprocess.run([str(CLI), "start", "--state", root], text=True, capture_output=True, timeout=60)
            self.assertEqual(started.returncode, 0, started.stderr)
            run = json.loads(started.stdout)["run_id"]
            directory = Path(root) / "offline_native"
            class ReviewDispatch(Exception):
                pass

            original_popen = subprocess.Popen
            def intercept(command, **kwargs):
                if Path(command[0]).name != "gx-native":
                    return original_popen(command, **kwargs)
                with closing(sqlite3.connect((directory/"gx.db").as_uri()+"?mode=ro", uri=True)) as conn:
                    self.assertEqual(conn.execute("SELECT stage,status FROM tasks WHERE run_id=?", (run,)).fetchall(),
                                     [("assessment", "READY")])
                    self.assertEqual(dict(conn.execute("SELECT status,count(*) FROM jobs WHERE run_id=? "
                                                      "GROUP BY status", (run,))), {"SUCCEEDED": 2})
                raise ReviewDispatch
            with patch("gx_core.project_continuation.subprocess.Popen", side_effect=intercept):
                with self.assertRaises(ReviewDispatch):
                    run_project(directory, "fixture-science-001", run_id=run, max_wall_seconds=15)

    def test_lost_handin_then_redelivery_preserves_each_original(self):
        with tempfile.TemporaryDirectory() as root:
            def invoke(action, *args):
                result = subprocess.run([str(CLI), action, "--state", root, *args],
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            first = invoke("start")
            self.assertEqual(first["status"], "WAITING_EXTERNAL_JOB")
            directory = Path(root) / "offline_native"
            run = first["run_id"]
            original_jobs = {job["job_id"] for job in first["jobs"]}
            self.assertEqual(len(original_jobs), 2)
            fault = invoke("start", "--instance", "sibling", "--fail-route", "path-b")
            self.assertNotEqual(fault["run_id"], run)
            # The start process exited. The detached runners record their own receipts.
            with native_state(directory) as state, closing(store.open_store(state.db_path)) as conn:
                ctx = backend_ctx(conn)
                job_dir = directory / "jobs"
                deadline = time.monotonic() + 10
                while not all((job_dir / job / "outcome.json").exists() for job in original_jobs):
                    if time.monotonic() > deadline:
                        self.fail("missing detached Job receipt")
                    time.sleep(.05)
                work = next_project_work(conn, "fixture-science-001", run_id=run)
                self.assertEqual(work["reactor_runs"], [run])
                self.assertEqual(work["actions"], [])
                reactor.lazy(ctx, run)
                reactor.drive_science(ctx, run)
                task = conn.execute("SELECT * FROM tasks WHERE run_id=? AND stage='assessment'", (run,)).fetchone()
                peer = OfflinePeer(conn, directory, first["instance"])
                try:
                    peer.work(task, {})
                finally:
                    peer.authority.close()
                row = conn.execute("SELECT * FROM submissions WHERE task_id=?", (task["task_id"],)).fetchone()
                self.assertEqual(row["result"], "PENDING_VALIDATION")
                self.assertEqual(next_project_work(conn, "fixture-science-001", run_id=run)["reactor_runs"], [run])
                submission_id = row["submission_id"]
                # Simulate a supervisor losing the reply after finish committed. Close
                # the database and leave the recorded outbox entirely untouched.
            final = invoke("resume")
            self.assertEqual(final["status"], "COMPLETED")
            self.assertEqual(len(final["jobs"]), 3)
            self.assertTrue(original_jobs <= {job["job_id"] for job in final["jobs"]})
            self.assertEqual([t["status"] for t in final["tasks"]], ["ACCEPTED"] * 4)
            with native_state(directory) as state, closing(store.open_store(state.db_path)) as conn:
                assets = ArtifactService(conn)
                rounds = assets.read_json(final["report_ref"])["rounds"]
                self.assertEqual(len(rounds), 2)
                decision = assets.read_json(rounds[0]["artifact_ref"])
                self.assertEqual(decision["continue_paths"], ["path-a"])
                assessment = assets.read_json(rounds[0]["assessment_ref"])
                self.assertEqual([f["supported"] for f in assessment["findings"]], [True, False])
                proposal = assets.read_json(rounds[1]["proposal_ref"])
                self.assertEqual(proposal["previous_decision_ref"], rounds[0]["artifact_ref"])
                self.assertEqual([p["id"] for p in proposal["paths"]], ["path-a"])
                self.assertEqual(conn.execute("SELECT result FROM submissions WHERE submission_id=?",
                                              (submission_id,)).fetchone()[0], "ACCEPTED")
                rows = conn.execute("SELECT topic,consumed_at FROM outbox WHERE run_id=?", (run,)).fetchall()
                self.assertTrue(all(r["consumed_at"] for r in rows))
                # Force repeated delivery of a durable submission and terminal Job
                # fact. Business effects and accepted originals must remain single.
                conn.execute("UPDATE outbox SET consumed_at=NULL WHERE run_id=? AND topic IN "
                             "('submission.recorded','job.terminal')", (run,))
                self.assertTrue(all(r["outcome"] == "reacted" for r in reactor.react(backend_ctx(conn), run)))
                self.assertIsNone(reactor.drive_science(backend_ctx(conn), run))
            sibling = invoke("resume", "--instance", "sibling")
            self.assertEqual(sibling["status"], "COMPLETED")
            self.assertEqual([j["status"] for j in sibling["jobs"]], ["SUCCEEDED", "FAILED", "SUCCEEDED"])
            self.assertEqual(invoke("resume"), final)
            with closing(sqlite3.connect((directory / "gx.db").as_uri() + "?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT count(*) FROM submissions s JOIN tasks t ON t.task_id=s.task_id "
                                    "WHERE t.run_id=?", (run,)).fetchone()
                self.assertEqual(rows[0], 4)
                assets = ArtifactService(conn)
                other = assets.read_json(sibling["report_ref"])["rounds"]
                findings = assets.read_json(other[0]["assessment_ref"])["findings"]
                self.assertEqual([f["supported"] for f in findings], [True, None])
