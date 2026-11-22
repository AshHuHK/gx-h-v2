"""Local detached Jobs survive loss of their submitter, launch ACK and terminal notice."""
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from gx_core import budgets
from gx_core.capabilities import storage
from gx_core.context import Ctx, Principal
from gx_core.errors import IdempotencyConflict
from gx_core.jobs import executor
from gx_core.jobs.grid_workflow import observe_grid, submit_grid
from gx_core.jobs.service import JobService
from gx_core.persistence import records, store


def crash_after_fork(db, request, launch_log):
    """Crash after a real launcher response, before GX receives its runner identity."""
    launch = executor.launch
    def fork_and_exit(directory):
        identity = launch(directory)
        Path(launch_log).write_text(json.dumps({"job_id": directory.name, **identity}))
        os._exit(73)

    with patch.object(executor, "launch", side_effect=fork_and_exit):
        submit_grid(store.open_store(db), **request)


class GridJobRecovery(unittest.TestCase):
    def test_reconnect_to_project_jobs_under_lost_receipts(self):
        from examples.p01_fixture import activate_grid

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            db = directory / "gx.db"
            conn = store.open_store(db)
            actor = "offline-recovery-test"
            try:
                ctx = Ctx(conn=conn, tool="tests", principal=Principal(subject=actor, scope="operator"))
                activate_grid(ctx)
                budget = budgets.create_root(conn, run_id=None, actor=actor)
                with store.transaction(conn) as tx:
                    run_id = records.create_run(tx, objective="Compare grid routes", project_ref="p05",
                                                actor=actor, budget_ref=budget)
                    for state in ("AUDITING", "PLANNING", "ACTIVE"):
                        store.transition(tx, machine="run", subject_id=run_id, to_state=state, actor=actor)
                grid = {"fixture_version": "1", "synthetic": True, "method_id": "fixture-grid",
                        "method_version": "1", "delay_seconds": 1.5,
                        "candidates": [{"candidate_id": "near-two", "x": 2}]}
                input_ref = storage.artifact_service(conn).publish_bytes(
                    namespace=f"{run_id}/original", rel_path="grid.json", kind="job_input",
                    data=json.dumps(grid), run_id=run_id, media_type="application/json",
                ).artifact_ref
                request = {"run_id": run_id, "input_ref": input_ref, "operation_id": "round1-near-two"}
            finally:
                conn.close()

            launch_log = directory / "external_launch.json"
            submitter = multiprocessing.get_context("spawn").Process(
                target=crash_after_fork, args=(db, request, launch_log))
            submitter.start()
            submitter.join(20)
            self.assertEqual(submitter.exitcode, 73)
            receipt = json.loads(launch_log.read_text())
            job_id = receipt["job_id"]
            conn = store.open_store(db)
            try:
                job_dir = storage.jobs_root(conn) / job_id
                runner = executor.observe(job_dir)
                self.assertIn(runner["state"], (executor.STARTING, executor.RUNNING))
                self.assertEqual(runner["runner_pid"], receipt["runner_pid"])
                # No observer consumes the finishing notification.
                deadline = time.monotonic() + 10
                while not (job_dir / "outcome.json").exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue((job_dir / "outcome.json").exists())
                row = conn.execute("SELECT status, scheduler_ref FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                self.assertEqual(tuple(row), ("SUBMITTING", None))
                with patch.object(executor, "launch", side_effect=AssertionError("duplicate launch")):
                    adopted = submit_grid(conn, **request)
                    self.assertTrue(adopted["replayed"])
                    self.assertEqual(adopted["job_id"], job_id)
                    self.assertEqual(adopted["scheduler_ref"],
                                     f"runner:{receipt['runner_pid']}:{receipt['runner_started']}")
                    first = observe_grid(conn, job_id)
                    second = observe_grid(conn, job_id)
                    self.assertEqual(first["status"], "SUCCEEDED", first["failure"])
                    self.assertEqual(first["observations_ref"], second["observations_ref"])
                    self.assertEqual(first["observations"]["observations"][0]["score"], 0)
                    self.assertEqual(conn.execute(
                        "SELECT count(*) FROM outbox WHERE idempotency_key=?",
                        (f"job.terminal:{job_id}",)).fetchone()[0], 1)
                    other = storage.artifact_service(conn).publish_bytes(
                        namespace=f"{run_id}/original", rel_path="other.json", kind="job_input",
                        data=json.dumps({**grid, "delay_seconds": 0}), run_id=run_id,
                    ).artifact_ref
                    with self.assertRaises(IdempotencyConflict):
                        submit_grid(conn, **{**request, "input_ref": other})

                # Simulate loss of the launcher response at the adapter boundary, with no
                # observable runner record. The reservation remains held and replays never launch.
                uncertain = {**request, "operation_id": "ambiguous"}
                with patch.object(executor, "launch", side_effect=executor.LaunchReceiptUnknown("lost ACK")) as launch:
                    first = submit_grid(conn, **uncertain)
                    repeated = submit_grid(conn, **uncertain)
                self.assertEqual(launch.call_count, 1)
                self.assertEqual((first["job_id"], first["status"]),
                                 (repeated["job_id"], "OUTCOME_UNKNOWN"))
                self.assertEqual(JobService(conn).status(first["job_id"])["status"], "OUTCOME_UNKNOWN")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
