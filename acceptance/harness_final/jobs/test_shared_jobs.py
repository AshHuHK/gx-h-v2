"""Two delegated researchers share one physical CPU Job and retain separate outcomes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier
import time
import unittest
from unittest.mock import patch

from acceptance.v62a.interleavings_fixture import PRODUCT, QUALITY, draft
from acceptance.v622.test_close import SurveyedStudy
from gx_core import budgets
from gx_core.artifacts import ArtifactService
from gx_core.errors import ResourceDenied, BudgetExceeded
from gx_core.capabilities import registry
from gx_core.domain import ids
from gx_core.jobs import executor
from gx_core.jobs.service import JobService
from gx_core.p0 import manifest, v6_jobs, v6_native, v62_modules
from gx_core.plugins import goal as goal_plugins
from gx_core.persistence import store


class SharedJob(unittest.TestCase):
    def start(self, temporary, *, isolated=False, independent=False, count=2):
        root = Path(temporary)
        template = PRODUCT / "examples/v62a-data-inventory/grant.json"
        cfg = json.loads(template.read_text())
        for item in cfg["input_files"]:
            item["path"] = str((template.parent / item["path"]).resolve())
        cfg.update(research_effort="deep", research_modules=["ad_hoc@1"], turn_token_budget=1000)
        cfg["run_budget"] = cfg["workspace_budget"] = {
            "cpu_seconds": 1200, "api_calls": 40, "tokens": 80000, "worker_tasks": 40,
            "job_submissions": 20, "wall_hours": 2}
        path = root / "grant.json"
        path.write_text(json.dumps(cfg))
        start = subprocess.run([sys.executable, str(PRODUCT / "bin/gx-p0"), "research", "--state",
                                str(root / "state"), "--run-key", "shared-execution", "--goal",
                                "Check the completeness of the supplied coordinate material.",
                                "--goal-config", str(path), "--timeout", ".001"],
                               env={**os.environ, "GX_CODEX_OFFLINE": "1"}, capture_output=True, text=True)
        self.assertEqual(start.returncode, 75, start.stderr + start.stdout)
        study = self.enterContext(SurveyedStudy(root, reopen=True))
        lead = study.next_actor("run")
        opened = []
        for name in ("a", "b", "c")[:count]:
            args = study.open_args("seat-" + name, inputs=study.materials[:1], caps=[QUALITY])
            args.update(module_id="ad_hoc", focus="data", expected_delivery="Report observed completeness.")
            if isolated and name == "b":
                args["communication"] = "isolated"
            opened.append(study.call(lead, "gx_open_module", args, "open-" + name))
        ids = []
        for i, seat in enumerate(opened):
            worker = study.next_actor(seat["scope"])
            if independent and i == 1:
                op = study.node(worker, "repeat", study.materials[0], capability=QUALITY)
                op["independent_repeat"] = True
                study.call(worker, v6_native.DECIDE, {"decision":draft(op)}, "independent")
            else:
                study.submit_node(worker, f"observe{i}", study.materials[0], capability=QUALITY)
            ids.append(study.conn.execute("SELECT node_id FROM v6_nodes WHERE branch_id=?",
                                          (seat["branch_id"],)).fetchone()[0])
        return study, opened, ids

    def dispatch(self, conn, rid, nid):
        return v6_jobs._run_node(conn, v6_jobs._node(conn, rid, nid), JobService(conn), None)

    def test_lost_ack_two_seats_cancel_producer_cold_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, seats, (a, b) = self.start(tmp)
            real_launch = executor.launch
            external = []

            def drop_ack(directory):
                external.append(real_launch(directory)["runner_pid"])
                raise executor.LaunchReceiptUnknown("lost acknowledgement after physical launch")

            with patch.object(executor, "launch", drop_ack), patch.object(executor, "observe",
                    return_value={"state": executor.UNKNOWN, "runner_alive": None, "deferred": True}):
                self.dispatch(s.conn, s.run_id, a)
                counts = v6_jobs.advance(s.conn, s.run_id)
                self.assertEqual(counts.get("subscribed"), 1)

            def node(conn, nid):
                return dict(v6_jobs._node(conn, s.run_id, nid))
            first, second = node(s.conn, a), node(s.conn, b)
            self.assertEqual((second["status"], second["job_id"]), ("RUNNING", first["job_id"]))
            self.assertEqual(len(external), 1)
            job = first["job_id"]
            self.assertEqual(s.conn.execute("SELECT status FROM jobs WHERE job_id=?", (job,)).fetchone()[0],
                             "OUTCOME_UNKNOWN")
            lead = s.lead
            s.call(lead, "gx_cancel_module", {"module_instance_id": seats[0]["module_instance_id"], "reason": "Withdraw just this seat.",
                                              "operation_key": "cancel-one"}, "cancel-one")
            with closing(store.open_store(s.state / "gx.db")) as cold, patch.object(executor, "launch",
                    side_effect=AssertionError("duplicate CPU launch after restart")):
                self.dispatch(cold, s.run_id, a)
                deadline = time.monotonic() + 30
                while node(cold, b)["status"] == "RUNNING" and time.monotonic() < deadline:
                    JobService(cold).reconcile(s.run_id)
                    self.dispatch(cold, s.run_id, b)
                    time.sleep(.03)
                survivor = node(cold, b)
                self.assertEqual((node(cold, a)["status"], survivor["status"]), ("CANCELLED", "REUSED"))
                self.assertTrue(ArtifactService(cold).verify(survivor["result_ref"])["ok"])
                proof = manifest.read_manifest(cold, job)
                v6_jobs.confirmed_result_identity(cold, b, proof, verify_inputs=True)
                self.assertEqual(cold.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
                self.assertEqual(cold.execute("SELECT count(*) FROM budget_reservations WHERE "
                                              "subject_kind='job' AND status='SETTLED'").fetchone()[0], 1)
                events = [dict(r) for r in cold.execute("SELECT event_id,kind,subject_id,new_state FROM events "
                        "WHERE kind IN ('v6.node.subscribed','v6.node.settled') ORDER BY event_id")]
                self.assertEqual([r["subject_id"] for r in events if r["kind"] == "v6.node.subscribed"], [b])
                v62_modules.advance(cold, s.run_id)
                follow = s.next_actor(seats[1]["scope"])
                receipt, = s.brief(follow)["verified_jobs"]
                self.assertEqual((receipt["node_id"],receipt["job_id"],receipt["artifact_ref"]),
                                 (b, job, survivor["result_ref"]))
                # After cancellation, the only successful logical node is itself a subscriber.
                # A later request can still verify the immutable admission of the original physical owner.
                repeat = s.submit_node(follow, "warm", s.materials[0], capability=QUALITY)
                warm = s.settle(repeat["resolved_ops"][0]["spec"]["node"])
                self.assertEqual((warm["status"],warm["job_id"],warm["result_ref"]),
                                 ("REUSED", job, survivor["result_ref"]))
                v6_jobs.confirmed_result_identity(cold, warm["node_id"], proof, verify_inputs=True)
                if target := os.getenv("GX_SHARED_JOB_TRACE"):
                    Path(target).write_text(json.dumps({"run_id": s.run_id, "job_id": job, "events": events,
                        "nodes": [a,b], "input": s.materials[0], "artifact": survivor["result_ref"],
                        "delivery": {"task":follow.task_id, **receipt}, "later_warm_node":warm["node_id"],
                        "physical_launches": len(external), "settled_job_reservations": 1}, indent=2) + "\n")

    def test_two_simultaneous_subscribers_delivered_one_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, seats, (a, b, c) = self.start(tmp, count=3)
            self.dispatch(s.conn, s.run_id, a)
            barrier = Barrier(2, timeout=15)
            def attach(nid):
                with closing(store.open_store(s.state / "gx.db")) as connection:
                    receiver = v6_jobs._node(connection, s.run_id, nid)
                    v6_jobs._compile(connection, receiver, json.loads(receiver["spec_json"])["inputs"])
                    barrier.wait()
                    return self.dispatch(connection, s.run_id, nid)
            with ThreadPoolExecutor(2) as pool:
                self.assertEqual(list(pool.map(attach, (b,c))), ["subscribed", "subscribed"])
            for nid in (a,b,c):
                s.settle(nid)
            rows = s.rows("SELECT node_id,status,result_ref,job_id,exec_key FROM v6_nodes")
            self.assertCountEqual([r["status"] for r in rows], ["SUCCEEDED", "REUSED", "REUSED"])
            self.assertEqual(len({(r["job_id"], r["result_ref"], r["exec_key"]) for r in rows}), 1)
            physical = manifest.read_manifest(s.conn, rows[0]["job_id"])
            self.assertEqual(physical["admission"]["owner"]["node_id"], a)
            for row in rows:
                v6_jobs.confirmed_result_identity(s.conn, row["node_id"], physical, verify_inputs=True)
                owned = v62_modules.job_outputs(s.conn, s.run_id, row, physical)
                self.assertTrue(any(out["artifact_ref"] == row["result_ref"] for out in owned))
            delivered = []
            for seat, expected in zip(seats, (a,b,c), strict=True):
                follow = s.next_actor(seat["scope"])
                proof, = s.brief(follow)["verified_jobs"]
                self.assertEqual(proof["node_id"], expected)
                delivered.append((follow.task_id, proof["artifact_ref"], proof["output_sha256"]))
            self.assertEqual(len({d[0] for d in delivered}), 3)
            self.assertEqual(len({d[1:] for d in delivered}), 1)
            self.assertEqual(s.conn.execute("SELECT count(*) FROM events WHERE kind='v6.node.subscribed'").fetchone()[0], 2)
            self.assertEqual(s.conn.execute("SELECT count(*) FROM budget_reservations WHERE "
                             "subject_kind='job' AND status='SETTLED'").fetchone()[0], 1)

    def test_simultaneous_first_admissions_converge_on_one_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, _, nodes = self.start(tmp)
            for nid in nodes:
                node = v6_jobs._node(s.conn, s.run_id, nid)
                v6_jobs._compile(s.conn, node, json.loads(node["spec_json"])["inputs"])
            barrier = Barrier(2, timeout=15)
            check = budgets.check_job_room
            def together(*args, **kwargs):
                check(*args, **kwargs)
                barrier.wait()
            def dispatch(nid):
                with closing(store.open_store(s.state / "gx.db")) as cold:
                    try:
                        return self.dispatch(cold, s.run_id, nid)
                    except (BudgetExceeded, ResourceDenied) as exc:
                        v6_jobs._record_wait(cold, v6_jobs._node(cold, s.run_id, nid), exc)
                        return "waiting"
            with patch.object(budgets, "check_job_room", together), ThreadPoolExecutor(2) as pool:
                self.assertCountEqual(list(pool.map(dispatch, nodes)), ["dispatched", "waiting"])
            for nid in nodes:
                s.settle(nid)
            rows = s.rows("SELECT node_id,status,result_ref,job_id FROM v6_nodes")
            self.assertCountEqual([r["status"] for r in rows], ["SUCCEEDED","REUSED"])
            self.assertEqual(len({(r["result_ref"], r["job_id"]) for r in rows}), 1)
            self.assertEqual(s.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_execution_key_distinguishes_seed_weights_private_or_independent_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, _, (a,b) = self.start(tmp)
            base = registry.candidate(s.conn, QUALITY)
            descriptor = next(row for row in goal_plugins.catalog(s.conn, s.run_id) if row["capability_version"] == QUALITY)
            entries = [{"role": "structure", "sha256": ArtifactService(s.conn).describe(s.materials[0])["sha256"]}]

            def identity(method, params, *, local=False):
                return v6_jobs.method_identity(s.conn, s.run_id, QUALITY, method, entries, params,
                                               module_binding={"descriptor":descriptor} if local else None)
            self.assertEqual(identity(base, {}), identity(base, {}, local=True))
            method = deepcopy(base)
            method["manifest"]["applicable_domain"] = {"schema": "gx.science-domain/1", "inputs": {},
                            "randomness":{"mode":"stochastic", "seed_parameter":"seed"}}
            first = identity(method, {"seed": 7})
            second = identity(method, {"seed": 8})
            self.assertNotEqual(ids.sha256_of_obj(first), ids.sha256_of_obj(second))
            self.assertEqual(first["randomness"], [{"mode":"seeded", "seed":7}])
            weights = deepcopy(method)
            weights["manifest"]["weights"] = [{"sha256":"1"*64, "bytes":12, "load_mode":"SAFETENSORS"}]
            self.assertNotEqual(first["weights"], identity(weights, {"seed":7})["weights"])
            unknown = identity(method, {})
            self.assertEqual(unknown["randomness"], [{"mode":"unknown", "seed":None}])
            def scope(nid, refs, method_id, independent=False):
                node = dict(v6_jobs._node(s.conn, s.run_id, nid))
                if independent:
                    node["spec_json"] = json.dumps({**json.loads(node["spec_json"]), "independent_repeat":True})
                return v6_jobs._reuse_scope(s.conn, node, refs, method_id)
            self.assertNotEqual(scope(a, s.materials[:1], unknown), scope(b, s.materials[:1], unknown))
            self.assertNotEqual(scope(a, s.materials[:1], first, True), scope(b, s.materials[:1], first))
            self.assertNotEqual(scope(a, ["branch-only"], first), scope(b, ["branch-only"], first))
            self.assertNotEqual(scope(a, s.materials[:1], {**first, "required_data_sha256":None}),
                                scope(b, s.materials[:1], {**first, "required_data_sha256":None}))

    def test_explicit_independent_recomputation_gets_a_new_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, _, (a,b) = self.start(tmp, independent=True)
            for nid in (a,b):
                s.settle(nid)
            rows = s.rows("SELECT status,job_id FROM v6_nodes")
            self.assertEqual([r["status"] for r in rows], ["SUCCEEDED", "SUCCEEDED"])
            self.assertEqual(len({r["job_id"] for r in rows}), 2)

    def test_isolated_seat_does_its_own_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, _, (a,b) = self.start(tmp, isolated=True)
            for nid in (a,b):
                s.settle(nid)
            rows = s.rows("SELECT status,job_id FROM v6_nodes")
            self.assertEqual([r["status"] for r in rows], ["SUCCEEDED", "SUCCEEDED"])
            self.assertEqual(len({r["job_id"] for r in rows}), 2)


if __name__ == "__main__":
    unittest.main()
