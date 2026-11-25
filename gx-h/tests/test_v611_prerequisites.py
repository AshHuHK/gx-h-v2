"""Public V6.1.1 decision wake and scoped observation regressions (zero model inference)."""
from contextlib import closing
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from gx_core.artifacts import ArtifactService
from gx_core.errors import BudgetExceeded
from gx_core.jobs.service import JobService
from gx_core.p0 import intent, v6_goal, v6_jobs, v6_native
from gx_core.persistence import store
from tests import test_v6_delegation, test_v6_goal
from tests.test_v6_decisions import draft


class GoalPrerequisites(unittest.TestCase):
    def fixture(self, case):
        fixture = case()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        return fixture

    def test_branch_watch_wakes_global_lead_once_from_correct_branch_and_decision(self):
        f = self.fixture(test_v6_delegation.BranchDelegation)
        grant = f.assign()
        branch = f.bind(grant["task_id"])
        global_hold = draft()
        global_hold["reconsider_when"] = [{"event": "budget_changed", "subject_ref": "run"}]
        parent = f.call(f.parent, v6_native.DECIDE, {"decision": global_hold}, "parent-maintains")
        hold = draft()
        hold["reconsider_when"] = [{"event": "evidence_added", "subject_ref": "run"}]
        own = f.call(branch, v6_native.DECIDE, {"decision": hold}, "branch-maintains")
        f.observe("unrelated-world", f.right)
        self.assertIsNone(v6_goal.ensure_task(f.conn, f.run))
        ref = f.observe("branch-late-result", f.left)
        with closing(store.open_store(f.conn.execute("PRAGMA database_list").fetchone()["file"])) as cold:
            self.assertEqual(v6_goal.waiting_for(cold, f.run)["kind"], "research_event")
            tid = v6_goal.ensure_task(cold, f.run)
            self.assertIsNotNone(tid)
            packet = store.loads(cold.execute("SELECT * FROM tasks WHERE task_id=?", (tid,)).fetchone(),
                                 "input_json")["brief"]
            self.assertEqual(packet["previous_decision"]["decision_ref"], parent["decision_ref"])
            triggers = [hit for change in packet["trigger"]["changes"] for hit in change.get("branch_watches", [])]
            self.assertEqual(len(triggers), 1)
            self.assertEqual(triggers[0]["scope"], "branch:" + f.left)
            self.assertEqual(triggers[0]["branch_id"], f.left)
            self.assertEqual(triggers[0]["decision_ref"], own["decision_ref"])
            self.assertIn(ref, packet["evidence_refs"])
            self.assertIn(own["decision_ref"], packet["evidence_refs"])
            self.assertEqual(v6_goal.record_observation(cold, f.run, ref, source="branch-late-result",
                                                       scope=f.left)["event_id"], packet["trigger"]["through_cursor"])
            self.assertIsNone(v6_goal.ensure_task(cold, f.run))
        f.call(f.bind(tid), v6_native.DECIDE, {"decision": global_hold}, "branch-wake-ack")
        self.assertIsNone(v6_goal.ensure_task(f.conn, f.run))

    def test_small_non_json_metadata_then_scoped_native_read(self):
        f = self.fixture(test_v6_delegation.BranchDelegation)
        f.call(f.parent, v6_native.DECIDE, {"decision": draft()}, "await")
        assets = ArtifactService(f.conn)
        text = "public,csv\nlocal_scope_marker,3"
        ref = assets.put(run_id=f.run, kind="sensor_notes", origin="INGEST", media_type="text/csv",
                         content=text, put_by="fixture").artifact_ref
        event = v6_goal.record_observation(f.conn, f.run, ref, source="local-csv", scope=f.left)
        self.assertEqual(v6_goal.record_observation(f.conn, f.run, ref, source="local-csv", scope=f.left), event)
        task = f.bind(v6_goal.ensure_task(f.conn, f.run))
        packet = f.call(task, "gx_task_context", {}, "context")
        observation, = (o for o in packet["observations"] if o["artifact_ref"] == ref)
        self.assertEqual((observation["read_with"], observation["media_type"], observation["bytes"]),
                         ("gx_read_artifact", "text/csv", len(text)))
        self.assertEqual(observation["sha256"], assets.describe(ref)["sha256"])
        self.assertNotIn("local_scope_marker", json.dumps(packet))
        self.assertIn("local_scope_marker", json.dumps(f.call(task, "gx_read_artifact", {"ref": ref}, "read")))

    def test_idle_run_has_actionable_operator_wait_across_reopen_and_real_supplement_ingress(self):
        f = self.fixture(test_v6_goal.GoalRuntime)
        pending = {**f.native.body, "reconsider_when": [{"event": "job_settled", "subject_ref": "run"}]}
        f.decide(pending)
        with closing(store.open_store(f.db.execute("PRAGMA database_list").fetchone()["file"])) as cold:
            for _ in range(2):
                self.assertIsNone(v6_goal.ensure_task(cold, f.rid))
                wait = v6_goal.waiting_for(cold, f.rid)
                self.assertEqual(wait["kind"], "operator")
                self.assertIn("supplement", wait["continue_with"])
            action = {"kind": "supplement", "text": "A new local calibration source was deposited."}
            intent.bind_main(cold, f.rid, "operator-thread")
            intent.stage_input(cold, f.rid, "manual-input", action)
            received = intent.accept_input(cold, f.rid, "operator-thread", "turn-manual", "manual-input")
            self.assertEqual(received["status"], "supplement_registered")
            tid = v6_goal.ensure_task(cold, f.rid)
            self.assertIsNotNone(tid)
            brief = store.loads(cold.execute("SELECT * FROM tasks WHERE task_id=?", (tid,)).fetchone(), "input_json")["brief"]
            self.assertEqual(brief["observations"][-1]["observation"]["text"], action["text"])
            self.assertIsNone(v6_goal.ensure_task(cold, f.rid))

    def test_inflight_node_keeps_job_wait_after_reopen(self):
        from tests import test_v6_operator_recovery as fixture
        r = self.fixture(fixture.OperatorRecovery)
        with closing(store.open_store(r.db_path)) as conn:
            call = r.claim(conn)
            inventory = fixture.node("local:quality", "tl1a-coordinate-quality@1", [r.structure])
            from gx_core.p0 import v6_decisions
            v6_decisions.adopt(conn, **call, decision=fixture.decision(fixture.branch("local:counts"), inventory))
        with closing(store.open_store(r.db_path)) as cold:
            waiting = v6_goal.waiting_for(cold, r.run_id)
            self.assertEqual((waiting["kind"], waiting["nodes"][0]["status"]), ("nodes", "PENDING"))
            # Deterministically hold the first admissible submission at the JobService boundary.
            with patch.object(JobService, "submit", side_effect=BudgetExceeded("wait for science CPU")):
                v6_jobs.advance(cold, r.run_id)
        with closing(store.open_store(r.db_path)) as cold:
            waiting = v6_goal.waiting_for(cold, r.run_id)
            node, = waiting["nodes"]
            self.assertEqual(node["wait"], {"code": "BUDGET_EXCEEDED", "message": "wait for science CPU"})
            self.assertIsNone(node["job_id"])
            self.assertIsNone(v6_goal.ensure_task(cold, r.run_id))
            v6_jobs.advance(cold, r.run_id)
        with closing(store.open_store(r.db_path)) as cold:
            running = v6_goal.waiting_for(cold, r.run_id)
            node, = running["nodes"]
            self.assertEqual(node["status"], "RUNNING")
            self.assertTrue(node["job_id"])
            self.assertNotIn("wait", node)
            self.assertEqual(cold.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
            self.assertIsNone(v6_goal.ensure_task(cold, r.run_id))
            # Wait for the real CPU process and collect it via the original Job owner.
            timeout = time.monotonic() + 15
            while time.monotonic() < timeout:
                v6_jobs.advance(cold, r.run_id)
                if cold.execute("SELECT status FROM v6_nodes").fetchone()[0] == "SUCCEEDED":
                    break
                time.sleep(.02)
            settled = dict(cold.execute("SELECT node_id,job_id,exec_key,status,result_ref FROM v6_nodes").fetchone())
            self.assertEqual(settled["status"], "SUCCEEDED")
            if directory := os.environ.get("GX_V611_PREREQUISITE_EVIDENCE"):
                destination = Path(directory)
                destination.mkdir(exist_ok=True, parents=True)
                receipt = {"run_id": r.run_id, "admission_wait": waiting, "admitted": running, "settled": settled,
                           "job_count": cold.execute("SELECT count(*) FROM jobs").fetchone()[0],
                           "paid_model_calls": 0, "provider_tokens": None}
                (destination / "pending-to-settled.json").write_text(json.dumps(receipt, indent=2) + "\n")
