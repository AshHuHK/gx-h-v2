"""Dynamic dispatch, graph fencing and reuse through the detached Job service."""

import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from gx_core import budgets
from gx_core.capabilities import shipped, storage
from gx_core.context import Ctx, Principal
from gx_core.domain import ids
from gx_core.errors import BudgetExceeded, IdempotencyConflict, SchemaInvalid
from gx_core.jobs import executor
from gx_core.jobs.service import JobService
from gx_core.persistence import records, store
from gx_core.p0 import schema, v6_jobs
from gx_core.p0.manifest import read_manifest


def crash_on_launch(db, run_id, log):
    launch = executor.launch

    def launch_then_exit(directory):
        identity = launch(directory)
        Path(log).write_text(json.dumps({"job_id": directory.name, **identity}))
        os._exit(73)

    with patch.object(executor, "launch", side_effect=launch_then_exit):
        v6_jobs.advance(store.open_store(db), run_id)


class DynamicJobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "gx.db"
        self.connect()
        schema.install(self.conn)
        actor = "test-v6"
        ctx = Ctx(conn=self.conn, tool="tests", principal=Principal(subject=actor, scope="operator"))
        shipped.ensure(ctx, shipped.GRID)
        self.budget = budgets.create_root(self.conn, run_id=None, actor=actor)
        with store.transaction(self.conn) as tx:
            self.run_id = records.create_run(
                tx,
                objective="Evaluate synthetic inputs",
                actor=actor,
                project_ref="dynamic",
                budget_ref=self.budget,
            )
            for state in ("AUDITING", "PLANNING", "ACTIVE"):
                store.transition(tx, machine="run", subject_id=self.run_id, to_state=state, actor=actor)
        self.art = (
            storage.artifact_service(self.conn)
            .publish_bytes(
                namespace=f"{self.run_id}/test",
                rel_path="grid.json",
                kind="job_input",
                run_id=self.run_id,
                data=json.dumps(
                    {
                        "fixture_version": "1",
                        "synthetic": True,
                        "method_id": "fixture-grid",
                        "method_version": "1",
                        "delay_seconds": 0.1,
                        "candidates": [{"candidate_id": "two", "x": 2}],
                    }
                ),
                media_type="application/json",
            )
            .artifact_ref
        )
        self.limits = {"cpu_seconds": 120, "job_submissions": 4}
        with store.transaction(self.conn) as tx:
            tx.execute(
                "INSERT INTO v6_goals(run_id,goal,capabilities_json,materials_json,limits_json) "
                "VALUES(?,?,?,?,?)",
                (
                    self.run_id,
                    "Compute the fixture",
                    json.dumps({shipped.GRID: shipped.pin(shipped.GRID)}),
                    json.dumps([self.art]),
                    json.dumps(self.limits),
                ),
            )
            tx.execute(
                "INSERT INTO v6_graph_versions VALUES(?,?,?,NULL,?,?)",
                (self.run_id, 1, "b" * 64, "{}", self.art),
            )
            tx.execute(
                "INSERT INTO v6_branches(branch_id,run_id,decision_ref,spec_json) VALUES(?,?,?,?)",
                ("science", self.run_id, self.art, json.dumps({"budget_limits": self.limits})),
            )

    def connect(self):
        self.conn = store.open_store(self.db)
        self.addCleanup(self.conn.close)

    def new(self, name, operation="same"):
        spec = {
            "op": "add_node",
            "node": name,
            "branch": "science",
            "capability": shipped.GRID,
            "inputs": [self.art],
            "parameters": {"operation": operation},
            "depends_on": [],
            "question": "Synthetic control",
        }
        self.conn.execute(
            "INSERT INTO v6_nodes(run_id,node_id,branch_id,version,decision_ref,spec_json,operation_key)"
            " VALUES(?,?,?,1,?,?,?)",
            (self.run_id, name, "science", self.art, ids.canonical_json(spec), name),
        )
        with store.transaction(self.conn) as tx:
            store.enqueue_outbox(
                tx,
                topic="v6.node",
                idempotency_key=f"v6.node:{name}",
                payload={"node_id": name, "run_id": self.run_id},
            )
        return spec

    def state(self, name):
        return v6_jobs._node(self.conn, self.run_id, name)

    def settle(self, name):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            v6_jobs.advance(self.conn, self.run_id)
            if self.state(name)["status"] in v6_jobs.NODE_TERMINAL:
                return self.state(name)
            time.sleep(0.05)
        self.fail(f"node did not settle: {dict(self.state(name))}")

    def test_lost_launch_ack_reopens_original_job_and_reuses_verified_result(self):
        self.new("original")
        self.conn.close()
        log = self.db.parent / "launcher.json"
        child = multiprocessing.get_context("spawn").Process(
            target=crash_on_launch, args=(self.db, self.run_id, log)
        )
        child.start()
        child.join(25)
        self.assertEqual(child.exitcode, 73)
        receipt = json.loads(log.read_bytes())
        self.connect()
        self.assertEqual(self.state("original")["job_id"], receipt["job_id"])
        self.assertEqual(self.conn.execute("SELECT status FROM jobs").fetchone()[0], "SUBMITTING")
        with patch.object(executor, "launch", side_effect=AssertionError("Duplicate external launch")):
            original = self.settle("original")
            self.assertEqual(original["status"], "SUCCEEDED")
            admission = json.loads(self.conn.execute("SELECT admission_json FROM p0_execution_manifests "
                                                     "WHERE job_id=?", (original["job_id"],)).fetchone()[0])
            self.assertEqual(admission["owner"]["kind"], "graph_node")
            self.assertEqual(admission["owner"]["operation_key"], original["operation_key"])
            self.assertEqual(admission["owner"]["exec_key"], original["exec_key"])
            self.new("reuse")
            same = self.settle("reuse")
            self.assertEqual(
                (same["status"], same["job_id"], same["result_ref"]),
                ("REUSED", receipt["job_id"], original["result_ref"]),
            )
            v6_jobs.advance(self.conn, self.run_id)
            self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
            actual = json.loads(
                self.conn.execute(
                    "SELECT spent_json FROM budgets WHERE budget_ref=?", (self.budget,)
                ).fetchone()[0]
            )
            self.assertEqual(actual["job_submissions"], 1)
            self.assertEqual(
                self.conn.execute(
                    "SELECT count(*) FROM outbox WHERE topic='v6.node' " "AND consumed_at IS NULL"
                ).fetchone()[0],
                0,
            )
        self.new("changed", "another")
        self.assertNotEqual(self.settle("changed")["job_id"], original["job_id"])

    def test_withdraw_between_compile_and_admission_keeps_other_node(self):
        self.new("withdraw")
        self.new("keep", "independent")
        original = JobService._admit

        def revoke(service, request, *args, **kwargs):
            if request["binding"]["node_id"] == "withdraw":
                service.conn.execute("UPDATE v6_nodes SET status='CANCEL_REQUESTED' WHERE node_id='withdraw'")
            return original(service, request, *args, **kwargs)

        with patch.object(JobService, "_admit", revoke):
            v6_jobs.advance(self.conn, self.run_id)
        self.assertEqual(self.settle("keep")["status"], "SUCCEEDED")
        self.assertEqual(self.state("withdraw")["status"], "CANCELLED")
        self.assertIsNone(self.state("withdraw")["job_id"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM budget_reservations WHERE subject_kind='job'").fetchone()[
                0
            ],
            1,
        )

    def test_recovered_evidence_binds_both_graph_nodes_to_the_original_job(self):
        self.new("first")
        original = self.settle("first")
        self.new("same")
        same = self.settle("same")
        self.assertEqual("REUSED", same["status"])
        proof = read_manifest(self.conn, original["job_id"])
        expected = v6_jobs.confirmed_result_identity(self.conn, "first", proof, verify_inputs=True)
        self.assertEqual(original["exec_key"], expected["exec_key"])
        self.assertEqual(proof["admission"]["input_manifest"]["sha256"], expected["input_sha256"])
        self.assertEqual(expected, v6_jobs.confirmed_result_identity(self.conn, "same", proof, verify_inputs=True))
        self.conn.execute("UPDATE v6_job_attempts SET identity_json=json_set(identity_json,'$.runtime.env_id','other') "
                          "WHERE node_id='same'")
        with self.assertRaises(SchemaInvalid):
            v6_jobs.confirmed_result_identity(self.conn, "same", proof)
        self.assertEqual(1, self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0])

    def test_new_compute_requires_an_affordable_branch_and_cache_hit_does_not_spend(self):
        original = self.new("first")
        short = {"job_submissions": 1, "cpu_seconds": 29}
        with self.assertRaises(BudgetExceeded):
            v6_jobs.validate_node(
                self.conn, self.run_id, original, node_ids={"first": shipped.GRID}, limits=short
            )
        self.assertEqual(self.settle("first")["status"], "SUCCEEDED")
        same = self.new("same")
        self.conn.execute("UPDATE v6_branches SET spec_json=?", (json.dumps({"budget_limits": short}),))
        v6_jobs.validate_node(
            self.conn, self.run_id, same, node_ids={"first": shipped.GRID, "same": shipped.GRID}, limits=short
        )
        self.assertEqual(self.settle("same")["status"], "REUSED")
        self.assertEqual(v6_jobs.branch_exposure(self.conn, "science")["job_submissions"], 1)

    def test_compiled_request_cannot_change_and_branch_cap_rolls_back_reservation(self):
        self.new("first")
        row = self.state("first")
        v6_jobs._compile(self.conn, row, [self.art])
        request = json.loads(self.conn.execute("SELECT request_json FROM v6_job_attempts").fetchone()[0])
        with self.assertRaises(IdempotencyConflict):
            JobService(self.conn).submit(
                **{**request, "parameters": {"operation": "tampered"}}, idempotency_key="first"
            )
        spec = json.loads(row["spec_json"])
        self.conn.execute(
            "UPDATE v6_nodes SET spec_json=? WHERE node_id='first'",
            (json.dumps({**spec, "question": "mutated assignment"}),),
        )
        with self.assertRaises(IdempotencyConflict):
            JobService(self.conn).submit(**request, idempotency_key="first")
        self.conn.execute("UPDATE v6_nodes SET spec_json=? WHERE node_id='first'", (json.dumps(spec),))
        self.conn.execute(
            "UPDATE v6_branches SET spec_json=?",
            (json.dumps({"budget_limits": {"cpu_seconds": 29, "job_submissions": 1}}),),
        )
        v6_jobs.advance(self.conn, self.run_id)
        self.assertEqual(self.state("first")["status"], "PENDING")
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM v6_node_waits WHERE node_id='first'").fetchone()
        )
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM budget_reservations WHERE subject_kind='job'").fetchone()[
                0
            ],
            0,
        )

    def test_withdraw_after_admission_prevents_launch(self):
        self.new("node")
        original = JobService._start

        def withdraw(service, row):
            service.conn.execute("UPDATE v6_nodes SET status='CANCEL_REQUESTED' WHERE node_id='node'")
            return original(service, row)

        with patch.object(JobService, "_start", withdraw), patch.object(
            executor, "launch", side_effect=AssertionError("withdrawn launch")
        ):
            v6_jobs.advance(self.conn, self.run_id)
            self.assertEqual(self.settle("node")["status"], "CANCELLED")
        self.assertEqual(
            self.conn.execute("SELECT status FROM budget_reservations WHERE subject_kind='job'").fetchone()[
                0
            ],
            "RELEASED",
        )
