"""Serialize a real local Job completion against a formal Intent change in both orders."""
import hashlib
import json
import multiprocessing
import os
import signal
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from gx_core import budgets
from gx_core.capabilities import storage
from gx_core.capabilities.shipped import ensure_grid
from gx_core.context import Ctx, Principal
from gx_core.errors import SchemaInvalid
from gx_core.jobs.grid_workflow import observe_grid, submit_grid
from gx_core.p0 import intent, manifest
from gx_core.persistence import records, store


MAIN = "test-main"
ITEM = "allowed-methods"
MESSAGE = "block-grid-method"
TURN = "block-grid-turn"
WAIT = 30


def run_writer(db_path, run_id, job_id, operation, rendezvous, hold_commit):
    """Coordinate the two real business transactions at SQLite's lock and commit points."""
    at_boundary, proceed, attempting, finished = rendezvous
    saw_effect = False
    saw_receipt = False

    def trace(statement):
        nonlocal saw_effect, saw_receipt
        sql = statement.strip().upper()
        if sql.startswith("UPDATE P0_HUMAN_INPUTS SET RECEIPT_TURN_ID"):
            saw_receipt = True
        if sql.startswith("INSERT INTO P0_INTENT_VERSIONS") or (
                sql.startswith("INSERT INTO OUTBOX(") and "'JOB.TERMINAL'" in sql):
            saw_effect = True
        if hold_commit and saw_effect and sql == "COMMIT":
            at_boundary.set()
            proceed.wait(WAIT)
        elif not hold_commit and sql == "BEGIN IMMEDIATE" and (operation != "change" or saw_receipt):
            # Let the independent ingress receipt commit first. Park at the lock request
            # for the transaction that computes impact and updates the item.
            at_boundary.set()
            proceed.wait(WAIT)
            attempting.set()

    with closing(store.connect(db_path)) as conn:
        conn.set_trace_callback(trace)
        if operation == "change":
            intent.accept_input(conn, run_id, MAIN, TURN, MESSAGE)
        else:
            observe_grid(conn, job_id)
    finished.set()


class IntentJobRaces(unittest.TestCase):
    def setUp(self):
        self.processes = multiprocessing.get_context("spawn")
        self.child_gates = []
        self.temp = tempfile.TemporaryDirectory(prefix="gx-intent-race-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        report_dir = os.environ.get("GX_P0_RACE_EVIDENCE")
        self.evidence = None
        if report_dir:
            self.evidence = Path(report_dir) / self._testMethodName
            self.evidence.mkdir(parents=True, exist_ok=True)
        self.db = root / "gx.sqlite"
        self.conn = store.open_store(self.db)
        self.addCleanup(self.conn.close)
        ctx = Ctx(conn=self.conn, tool="tests", principal=Principal(subject="race-test", scope="operator"))
        ensure_grid(ctx)
        with store.transaction(self.conn) as tx:
            budget = budgets.create_root(tx, run_id=None, actor="race-test")
            self.run_id = records.create_run(tx, objective="Measure a synthetic grid", project_ref="race-test",
                                             actor="race-test", budget_ref=budget,
                                             workflow_id="science_grid", workflow_version="1")
            for state in ("AUDITING", "PLANNING", "ACTIVE"):
                store.transition(tx, machine="run", subject_id=self.run_id, to_state=state, actor="race-test")
        intent.bind_main(self.conn, self.run_id, MAIN)
        intent.stage_input(self.conn, self.run_id, "initial-methods", self.change(0, []))
        intent.accept_input(self.conn, self.run_id, MAIN, "initial-turn", "initial-methods")
        intent.bind_dependencies(self.conn, self.run_id, "workflow", self.run_id, {ITEM: "execution"})
        with store.transaction(self.conn) as tx:
            self.task_id = records.create_task(tx, run_id=self.run_id, kind="execute_path", role="executor",
                                               actor="race-test", task_input={"capability_version": "fixture-grid@1"})
            store.transition(tx, machine="task", subject_id=self.task_id, to_state="READY", actor="race-test")
            lease = store.issue_lease(tx, task_id=self.task_id, worker_role="executor", owner="race-worker",
                                      seconds=600, attempt=1)
            self.lease_id = lease["lease_id"]
            store.transition(tx, machine="task", subject_id=self.task_id, to_state="CLAIMED", actor="race-test",
                             extra_updates={"attempt": 1, "owner": "race-worker", "lease_id": self.lease_id})
            intent.bind_dependencies(tx, self.run_id, "task", self.task_id, {ITEM: "execution"})

        grid = {"fixture_version": "1", "synthetic": True, "method_id": "fixture-grid", "method_version": "1",
                "candidates": [{"candidate_id": "exact", "x": 2}], "delay_seconds": 0.1}
        raw = storage.artifact_service(self.conn).publish_bytes(
            namespace=f"{self.run_id}/inputs", rel_path="grid.json", kind="job_input",
            data=json.dumps(grid), run_id=self.run_id, media_type="application/json")
        self.job_id = submit_grid(self.conn, run_id=self.run_id, operation_id="race", input_ref=raw.artifact_ref)["job_id"]
        admission = manifest.read_manifest(self.conn, self.job_id)
        self.assertEqual(admission["admission"]["intent_dependencies"][ITEM]["version"], 1)
        self.assertIsNone(admission["completion"])
        # The external backend has finished. Only its GX terminal observation is outstanding.
        outcome = storage.jobs_root(self.conn) / self.job_id / "outcome.json"
        deadline = time.monotonic() + WAIT
        while not outcome.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(outcome.is_file(), "local grid worker produced no backend receipt")
        receipt = json.loads(outcome.read_text())
        self.assertEqual(receipt["exit_code"], 0)
        self.save("backend_outcome", receipt)
        intent.stage_input(self.conn, self.run_id, MESSAGE, self.change(1, ["fixture-grid"]))
        self.before = self.snapshot("before")
        self.assertTrue(self.before["job_current"])
        self.assertNotEqual(self.before["job_status"], "SUCCEEDED")

    @staticmethod
    def change(expected_version, denied):
        return {"kind": "change", "items": [{
            "item_id": ITEM, "item_type": "method_ban", "expected_version": expected_version,
            "value": {"forbidden_capabilities": denied},
        }]}

    def snapshot(self, name):
        # Independent read connection also catches a partially visible multi-row update.
        with closing(store.connect(self.db)) as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT status, runner_pid, scheduler_ref FROM jobs WHERE job_id=?",
                               (self.job_id,)).fetchone()
            frozen = manifest.read_manifest(conn, self.job_id)
            state = {"run_id": self.run_id, "job_id": self.job_id, "job_status": row["status"],
                     "runner_pid": row["runner_pid"], "scheduler_ref": row["scheduler_ref"],
                     "item": intent.read_item(conn, self.run_id, ITEM),
                     "job_current": intent.job_current(conn, self.job_id),
                     "admission_sha256": frozen["admission_hash"], "completion_sha256": frozen["manifest_hash"],
                     "change_records": [dict(r) for r in conn.execute(
                         "SELECT change_id,status,impact_json FROM p0_intent_changes WHERE run_id=? ORDER BY rowid",
                         (self.run_id,))],
                     "task": dict(conn.execute("SELECT task_id,status,attempt,lease_id FROM tasks WHERE task_id=?",
                                               (self.task_id,)).fetchone()),
                     "lease_released_at": conn.execute("SELECT released_at FROM leases WHERE lease_id=?",
                                                       (self.lease_id,)).fetchone()[0],
                     "outbox": [dict(r) for r in conn.execute(
                         "SELECT outbox_id,topic,idempotency_key,attempts,consumed_at FROM outbox "
                         "WHERE run_id=? ORDER BY outbox_id", (self.run_id,))],
                     "job_artifacts": [dict(r) for r in conn.execute(
                         "SELECT artifact_ref,kind,sha256 FROM artifacts WHERE job_id=? ORDER BY artifact_ref",
                         (self.job_id,))]}
        self.save(name, state)
        return state

    def save(self, name, value):
        if self.evidence:
            (self.evidence / (name + ".json")).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

    def writer(self, operation, *, hold_commit):
        gates = tuple(self.processes.Event() for _ in range(4))
        # Spawned children still need named semaphores when opening their connections.
        self.child_gates.append(gates)
        child = self.processes.Process(target=run_writer, args=(
            self.db, self.run_id, self.job_id, operation, gates, hold_commit))
        child.start()

        def close():
            if child.is_alive():
                child.kill()
            child.join(WAIT)
            child.close()
        self.addCleanup(close)
        return child, gates

    def test_killed_change_writer_rolls_back_items_impact_and_outbox(self):
        start = time.monotonic()
        child, (at_commit, _, _, _) = self.writer("change", hold_commit=True)
        self.assertTrue(at_commit.wait(WAIT), "writer never reached its uncommitted outbox boundary")
        child.kill()
        child.join(WAIT)
        self.assertEqual(child.exitcode, -signal.SIGKILL)
        self.assertEqual(self.before, self.snapshot("after_kill"))
        authorization = self.conn.execute("SELECT receipt_turn_id,consumed_at FROM p0_human_inputs "
                                          "WHERE message_id=?", (MESSAGE,)).fetchone()
        self.assertEqual(tuple(authorization), (TURN, None))
        recovered = intent.accept_input(self.conn, self.run_id, MAIN, TURN, MESSAGE)
        after = self.snapshot("after_retry")
        self.assertEqual(after["item"]["version"], 2)
        self.assertFalse(after["job_current"])
        self.assertIsNotNone(after["lease_released_at"])
        self.assertEqual(after["task"]["status"], "READY")
        self.assertEqual(sum(x["topic"] == "p0.intent.notification" for x in after["outbox"]), 1)
        replay = intent.accept_input(self.conn, self.run_id, MAIN, TURN, MESSAGE)
        self.assertEqual(replay["change_id"], recovered["change_id"])
        self.assertEqual(after, self.snapshot("after_replay"))
        self.save("recovery", {"signal": child.exitcode, "elapsed_seconds": time.monotonic() - start,
                               "change": recovered})

    def run_order(self, first, second):
        start = time.monotonic()
        other_child, (ready, compete, attempting, finished) = self.writer(second, hold_commit=False)
        self.assertTrue(ready.wait(WAIT), "second writer never prepared the business transaction")
        first_child, (at_commit, release, _, _) = self.writer(first, hold_commit=True)
        self.assertTrue(at_commit.wait(WAIT), "first writer never reached its uncommitted outbox boundary")
        compete.set()
        self.assertTrue(attempting.wait(WAIT), "second writer never requested the write lock")
        self.assertFalse(finished.wait(0.15), "second writer completed while the first still held the lock")
        release.set()
        for child in (first_child, other_child):
            child.join(WAIT)
            self.assertEqual(child.exitcode, 0)
        change = intent.accept_input(self.conn, self.run_id, MAIN, TURN, MESSAGE)
        impact, = [obj for obj in change["impact"] if obj["object_type"] == "job" and obj["object_id"] == self.job_id]
        self.assertEqual(impact["status"], self.before["job_status"] if first == "change" else "SUCCEEDED")
        self.assertEqual(impact["action"], "historical")
        after = self.snapshot("after")
        self.assertEqual(after["job_status"], "SUCCEEDED")
        self.assertEqual(after["item"]["version"], 2)
        self.assertFalse(after["job_current"])
        self.assertEqual(after["scheduler_ref"], self.before["scheduler_ref"])
        self.assertEqual(after["admission_sha256"], self.before["admission_sha256"])
        completion = manifest.read_manifest(self.conn, self.job_id)["completion"]
        self.assertEqual(completion["current_at_completion"], first == "completion")
        self.save("completion", completion)
        job = observe_grid(self.conn, self.job_id)
        self.assertEqual(job["observations"]["observations"][0]["score"], 0)
        self.save("grid_observations", job["observations"])
        self.assertIn(job["observations_ref"], [a["artifact_ref"] for a in after["job_artifacts"]])
        digest, = [a["sha256"] for a in after["job_artifacts"] if a["artifact_ref"] == job["observations_ref"]]
        artifact_service = storage.artifact_service(self.conn)
        self.assertEqual(hashlib.sha256(artifact_service.read_bytes(job["observations_ref"])).hexdigest(), digest)
        with self.assertRaises(SchemaInvalid) as invalid:
            manifest.validate_evidence(self.conn, run_id=self.run_id, job_id=self.job_id,
                                       artifact_ref=job["observations_ref"])
        self.assertEqual(invalid.exception.details["reason"], "current_intent")
        self.assertEqual(sum(x["topic"] == "job.terminal" for x in after["outbox"]), 1)
        self.assertEqual(sum(x["topic"] == "p0.intent.notification" for x in after["outbox"]), 1)
        self.assertEqual(after, self.snapshot("after_replay"))
        self.save("ordering", {"first_writer": first, "second_writer": second, "first_pid": first_child.pid,
                               "second_pid": other_child.pid, "elapsed_seconds": time.monotonic() - start,
                               "change": change, "original_output": job["observations_ref"]})

    def test_change_wins_job_completion_race(self):
        self.run_order("change", "completion")

    def test_job_completion_wins_change_race(self):
        self.run_order("completion", "change")


if __name__ == "__main__":
    unittest.main()
