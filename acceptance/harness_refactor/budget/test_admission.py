"""Two independent dispatchers reach the last module Job credit through the real Job owner."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from acceptance.v62a.interleavings_fixture import draft
from acceptance.v622.test_close import SurveyedStudy
from gx_core import budgets
from gx_core.errors import BudgetExceeded
from gx_core.jobs import executor
from gx_core.jobs.service import JobService
from gx_core.p0 import v62_budget, v62_modules, v6_goal, v6_jobs, v6_native
from gx_core.persistence import store

CAP = "social-source-intake@1"


class Admission(unittest.TestCase):
    def test_two_dispatchers_one_credit_cold_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = []
            for index in (1, 2):
                path = root / f"source-{index}.json"
                path.write_text(json.dumps({"url": f"https://mp.weixin.qq.com/s/test{index}",
                                            "text": f"Local arithmetic example number {index}."}))
                inputs.append({"path": str(path), "role": "original"})
            config = {"research_effort": "deep",
                      "research_modules": ["ad_hoc@1"],
                      "plugins": ["org.gx.scholarly-sources@1.0.0"], "capabilities": [CAP],
                      "input_files": inputs}
            config["turn_token_budget"] = 1000
            config["run_budget"] = config["workspace_budget"] = {
                "api_calls": 40, "tokens": 50000, "worker_tasks": 40,
                "cpu_seconds": 1200, "job_submissions": 20, "wall_hours": 2}
            cfg = root / "grant.json"
            cfg.write_text(json.dumps(config))
            product = Path(__file__).resolve().parents[3]
            result = subprocess.run([sys.executable, str(product / "bin/gx-p0"), "research",
                                     "--state", str(root / "state"), "--run-key", "credit-contention",
                                     "--goal", "Compare locally supplied arithmetic examples.",
                                     "--goal-config", str(cfg), "--timeout", "0.001"],
                                    env={**os.environ, "GX_CODEX_OFFLINE": "1"},
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
            with SurveyedStudy(root, reopen=True) as study:
                study.next_actor("run")
                grant = study.open_args("inspect-records", caps=[CAP], inputs=study.materials,
                                        limits={"job_submissions": 1, "cpu_seconds": 180})
                grant.update(module_id="ad_hoc", focus="literature",
                             expected_delivery="Index the supplied local text.")
                opened = study.call(study.lead, "gx_open_module", grant, "delegate")
                study.call(study.lead, v6_native.DECIDE, {"decision": draft()}, "await-findings")
                worker = study.next_actor(opened["scope"])
                requests = [study.node(worker, f"index{i}", ref, capability=CAP)
                            for i, ref in enumerate(study.materials)]
                study.call(worker, v6_native.DECIDE, {"decision": draft(*requests)}, "propose")
                nodes = [row[0] for row in study.conn.execute("SELECT node_id FROM v6_nodes ORDER BY node_id")]
                self.assertEqual(len(nodes), 2)
                child, parent = study.conn.execute("SELECT m.budget_ref,b.parent_ref FROM v62_modules m "
                                                  "JOIN budgets b USING(budget_ref)").fetchone()
                self.assertNotIn("paid_api_calls", budgets.state(study.conn, parent)["limits"])
                self.assertNotIn("paid_api_calls", budgets.state(study.conn, child)["limits"])
                for nid in nodes:
                    node = v6_jobs._node(study.conn, study.run_id, nid)
                    v6_jobs._compile(study.conn, node, json.loads(node["spec_json"])["inputs"])
                fence = Barrier(2, timeout=30)
                original = budgets.check_job_room

                def coincide(conn, *args, **kwargs):
                    original(conn, *args, **kwargs)
                    fence.wait()

                def dispatch(nid):
                    with closing(store.open_store(study.state / "gx.db")) as conn:
                        node = v6_jobs._node(conn, study.run_id, nid)
                        try:
                            v6_jobs._run_node(conn, node, JobService(conn), None)
                        except BudgetExceeded as exc:
                            v6_jobs._record_wait(conn, node, exc)
                            return False
                        return True

                launches = []
                launch = executor.launch

                def drop_ack(job_dir):
                    receipt = launch(job_dir)
                    launches.append(receipt["runner_pid"])
                    raise executor.LaunchReceiptUnknown("lost acknowledgement after the actual CPU launch")

                with patch.object(budgets, "check_job_room", coincide), patch.object(
                        executor, "launch", drop_ack), patch.object(executor, "observe",
                        return_value={"deferred": True}), ThreadPoolExecutor(2) as pool:
                    self.assertCountEqual(pool.map(dispatch, nodes), [True, False])
                winner, job = study.conn.execute("SELECT node_id,job_id FROM v6_nodes WHERE job_id IS NOT NULL").fetchone()
                loser = next(nid for nid in nodes if nid != winner)
                self.assertEqual(len(launches), 1)
                self.assertEqual("OUTCOME_UNKNOWN", study.conn.execute("SELECT status FROM jobs").fetchone()[0])
                hold, = study.conn.execute("SELECT reservation_ref FROM jobs WHERE job_id=?", (job,)).fetchone()
                with closing(store.open_store(study.state / "gx.db")) as cold:
                    self.assertFalse(v62_budget.close_if_settled(cold, child))
                    actual_hold = cold.execute("SELECT status FROM budget_reservations WHERE reservation_ref=?",
                                               (hold,)).fetchone()[0]
                    self.assertEqual(actual_hold, "HELD")
                    self.assertEqual(budgets.state(cold, parent)["reserved"]["job_submissions"], 1)
                    for _ in range(2):
                        self.assertFalse(dispatch(loser))
                    waits = cold.execute("SELECT wait_json FROM v6_node_waits WHERE node_id=?", (loser,)).fetchone()
                    detail = json.loads(waits[0])["details"]
                    line, = detail["shortfalls"]
                    self.assertEqual((line["key"], line["short_by"], line["reserved"], line["available"]),
                                     ("job_submissions", 1, 1, 0))
                    self.assertEqual(detail["held_reservations"][0]["reservation_ref"], hold)
                    self.assertEqual(1, cold.execute("SELECT count(*) FROM events WHERE kind='v6.node.waiting' "
                                                      "AND subject_id=?", (loser,)).fetchone()[0])
                    v62_modules.advance(cold, study.run_id)
                    v6_goal.ensure_task(cold, study.run_id)
                    child_packet = study.brief(study.next_actor(opened["scope"]))
                    self.assertEqual(child_packet["funding_shortfalls"][0]["details"], detail)
                    lead_packet = study.brief(study.next_actor("run"))
                    changes = [c for c in lead_packet["trigger"]["changes"] if c["event"] == "v6.node.waiting"]
                    delta, = changes
                    self.assertEqual((delta["subject_ref"], delta["shortfalls"]), (opened["scope"], detail["shortfalls"]))
                    self.assertEqual(delta["held_reservations"],
                                     [{"reservation_ref": hold, "amounts": {"job_submissions": 1}}])
                    with patch.object(executor, "launch", side_effect=AssertionError("duplicate external launch")):
                        request = json.loads(cold.execute("SELECT request_json FROM v6_job_attempts WHERE node_id=?",
                                                          (winner,)).fetchone()[0])
                        key = cold.execute("SELECT operation_key FROM v6_nodes WHERE node_id=?", (winner,)).fetchone()[0]
                        replay = JobService(cold).submit(**request, idempotency_key=key)
                        self.assertEqual((replay["job_id"], replay["replayed"]), (job, True))
                        receipt = study.settle(winner)
                    self.assertEqual(receipt["job_id"], job)
                    self.assertEqual(receipt["status"], "SUCCEEDED")
                    v6_jobs.advance(cold, study.run_id)
                    self.assertEqual(1, cold.execute("SELECT count(*) FROM jobs").fetchone()[0])
                    self.assertEqual(1, budgets.state(cold, child)["spent"]["job_submissions"])
                    self.assertEqual(1, cold.execute("SELECT count(*) FROM budget_reservations WHERE "
                                                     "subject_kind='job' AND status='SETTLED'").fetchone()[0])

                    if target := os.getenv("GX_HARNESS_BUDGET_RECEIPT"):
                        Path(target).write_text(json.dumps({
                            "entry": "bin/gx-p0 research", "transport": "scripted local actor",
                            "method": CAP, "run": study.run_id, "module": opened["scope"], "nodes": nodes,
                            "winner": winner, "waiting": loser, "job": job, "outcome_after_lost_ack": "OUTCOME_UNKNOWN",
                            "actual_cpu_runner_count":len(launches), "reservation": hold,
                            "shortfall_at_race": detail, "lead_delivery": delta, "result": receipt["result_ref"],
                            "job_status": receipt["status"], "settled_job_units": 1,
                        }, indent=2) + "\n")
