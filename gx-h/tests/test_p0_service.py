"""Native submission, callback replay, and attempt recovery on a real SQLite store."""
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from gx_core.artifacts.service import ArtifactService
from gx_core.context import Ctx, Principal
from gx_core.domain import clock, ids
from gx_core.errors import LeaseExpired, ScopeDenied
from gx_core.hostobs.app_server_client import ReceivedMessage
from gx_core.orchestration.service import default_registry
from gx_core.p0 import intent, schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store
from gx_core.teams.service import TeamService


class WorkflowProductService(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.conn = store.open_store(self.directory / "gx.db")
        self.addCleanup(self.conn.close)
        schema.install(self.conn)
        workflow = default_registry().get("science_grid", "1")
        self.run = records.create_run(
            self.conn, objective="Review a frozen numerical result", project_ref="product-service", actor="backend",
            workflow_id="science_grid", workflow_version="1", pins={"workflow_sha256": workflow.digest()})
        self.conn.execute("UPDATE runs SET status='ACTIVE' WHERE run_id=?", (self.run,))
        intent.bind_main(self.conn, self.run, "coordinator")
        assets = ArtifactService(self.conn)

        def put(kind, value):
            return assets.put(run_id=self.run, kind=kind, media_type="application/json",
                              content=ids.canonical_json(value), origin="HOST_PUT", put_by="coordinator").artifact_ref

        proposal = put("proposal", {"author_principal": "author", "threshold": 10,
                                    "paths": [{"id": "sample", "expected_reaches_threshold": True}]})
        observations = put("observations", {"values": [12]})
        evaluation = put("evaluation", {"threshold": 10, "count_at_or_above": 1, "input_ref": observations})
        evidence = put("evidence", {"routes": [{"path_id": "sample", "outcome_kind": "valid_scientific_outcome",
                                                "observations_ref": observations, "evaluation_ref": evaluation}]})
        self.product = {"proposal_ref": proposal, "evidence_refs": [evidence], "findings": [
            {"path_id": "sample", "supported": True, "reason": "12 exceeds 10.", "evaluation_ref": evaluation}]}
        self.task = records.create_task(self.conn, run_id=self.run, actor="backend", kind="research_round",
            role="evidence_reviewer", max_attempts=1, required_artifacts=["science_assessment"],
            task_input={"p0": True, "output_kind": "science_assessment", "proposal_ref": proposal,
                        "evidence_refs": [evidence]}, acceptance={"validate_workflow_product": True})
        self.lease = store.issue_lease(self.conn, task_id=self.task, attempt=1, seconds=3600,
                                       owner="gx:p0-dispatch", principal="reviewer", worker_role="evidence_reviewer")
        self.conn.execute("UPDATE tasks SET status='CLAIMED',attempt=1,lease_id=?,owner=? WHERE task_id=?",
                          (self.lease["lease_id"], self.lease["owner"], self.task))
        self.conn.execute("INSERT INTO p0_roots(root_key,run_id,task_id,attempt,lease_id,thread_id,cwd,role,model,"
                          "effort,brief,state) VALUES('key',?,?,1,?,'reviewer',?,'evidence_reviewer','fixture',"
                          "'high','Review the original evaluation','bound')",
                          (self.run, self.task, self.lease["lease_id"], str(self.directory)))
        self.roots = Roots(self.conn, run_id=self.run, cwd=self.directory)
        self.roots.active_turns["reviewer"] = "turn"

    def invoke(self, product, *, thread="reviewer", turn="turn", call_id="submit"):
        return self.roots._callback(ReceivedMessage(1, {"method": "item/tool/call", "params": {
            "threadId": thread, "turnId": turn, "callId": call_id,
            "tool": "gx_submit_product", "arguments": {"product": product}}}))

    def test_rollback_retry_and_cold_replay_share_one_accepted_verdict(self):
        self.assertFalse(self.invoke(self.product, thread="other")["success"])
        self.assertFalse(self.invoke(self.product, turn="other")["success"])
        self.conn.execute("CREATE TEMP TRIGGER fail_receipt BEFORE INSERT ON p0_root_calls "
                          "BEGIN SELECT RAISE(ABORT, 'receipt write failed'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "receipt write failed"):
            self.invoke(self.product)
        for table in ("submissions", "p0_root_calls"):
            self.assertEqual(self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM artifacts WHERE task_id=?", (self.task,)).fetchone()[0], 0)
        self.assertIsNone(self.conn.execute("SELECT goal_desired FROM p0_roots").fetchone()[0])
        self.assertFalse(self.roots._goal_queue)
        store.validate_lease(self.conn, self.lease["lease_id"], task_id=self.task)
        self.conn.execute("DROP TRIGGER fail_receipt")
        accepted = self.invoke(self.product)
        self.assertTrue(accepted["success"], accepted)
        result = json.loads(accepted["contentItems"][0]["text"])
        self.assertEqual(result["task_status"], "ACCEPTED")
        self.assertEqual(ArtifactService(self.conn).read_json(result["artifact_ref"]), self.product)
        saved = self.conn.execute("SELECT * FROM submissions").fetchone()
        self.assertEqual(saved["content_sha256"], ids.sha256_of_obj(self.product))
        self.assertEqual(saved["brief_sha256"], ids.sha256_of_obj("Review the original evaluation"))
        self.assertEqual(saved["records_artifact_ref"], result["records_ref"])
        with self.assertRaises(LeaseExpired):
            store.validate_lease(self.conn, self.lease["lease_id"])
        self.assertEqual(self.conn.execute("SELECT goal_desired FROM p0_roots").fetchone()[0], "complete")
        self.assertEqual(self.roots._goal_queue, {"reviewer"})

        self.conn.close()
        self.conn = store.open_store(self.directory / "gx.db")
        self.addCleanup(self.conn.close)
        self.roots = Roots(self.conn, run_id=self.run, cwd=self.directory)
        self.roots.active_turns["reviewer"] = "turn"
        self.assertEqual(self.invoke(self.product), accepted)
        self.assertFalse(self.invoke({})["success"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM submissions").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM p0_root_calls").fetchone()[0], 1)
        outbox = self.conn.execute("SELECT payload_json FROM outbox WHERE topic='submission.accepted'").fetchall()
        self.assertEqual([json.loads(r[0])["subject"] for r in outbox], [saved["submission_id"]])

    def test_pinned_validator_retains_its_scientific_refusal(self):
        bad = {**self.product, "findings": [{**self.product["findings"][0], "supported": False}]}
        answer = self.invoke(bad)
        self.assertTrue(answer["success"], answer)
        result = json.loads(answer["contentItems"][0]["text"])
        self.assertEqual(result["task_status"], "REJECTED")
        verdict = ArtifactService(self.conn).read_json(result["records_ref"])
        self.assertIn("contradicts the frozen result", verdict["records"][0]["message"])
        self.assertEqual(self.invoke(bad), answer)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM outbox WHERE topic='submission.accepted'").fetchone()[0], 0)

    def test_public_methods_fence_wrong_principal_and_expired_attempt(self):
        team = TeamService(self.conn)
        arguments = dict(run_id=self.run, task_id=self.task, lease_ref=self.lease["lease_id"],
                         brief_sha256=ids.sha256_of_obj("brief"), product=self.product,
                         callback={"thread_id": "wrong", "turn_id": "turn", "call_id": "call"})
        with self.assertRaises(ScopeDenied):
            team.submit_workflow_product(**arguments)
        arguments["callback"]["thread_id"] = "reviewer"
        user = TeamService(self.conn, ctx=Ctx(self.conn, Principal("reviewer")))
        with self.assertRaises(ScopeDenied):
            user.submit_workflow_product(**arguments)
        with self.assertRaises(ScopeDenied):
            user.recover_failed_attempt(task_id=self.task, lease_ref=self.lease["lease_id"], reason="unowned")
        with self.assertRaises(ScopeDenied):
            team.recover_failed_attempt(task_id=self.task, lease_ref="old", reason="late failure")
        self.conn.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00.000Z' WHERE lease_id=?",
                          (self.lease["lease_id"],))
        with self.assertRaises(LeaseExpired):
            team.submit_workflow_product(**arguments)
        self.assertIsNone(self.roots._claim(self.task))
        task = self.conn.execute("SELECT status,attempts_consumed,lease_id FROM tasks WHERE task_id=?", (self.task,)).fetchone()
        self.assertEqual(tuple(task), ("BLOCKED", 1, None))
        self.assertIsNotNone(self.conn.execute("SELECT released_at FROM leases").fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM submissions").fetchone()[0], 0)

    def test_hard_failed_p0_turns_lapse_under_their_original_root_and_exhaust_exactly_once(self):
        from gx_core.hostobs import host_policy
        document, digest, host = host_policy.p0_host()
        self.conn.execute("UPDATE runs SET agent_policy_json=?,agent_policy_sha256=?,host_json=? WHERE run_id=?",
                          (ids.canonical_json(document), digest, ids.canonical_json(host), self.run))
        self.conn.execute("UPDATE tasks SET max_attempts=2 WHERE task_id=?", (self.task,))
        self.roots = Roots(self.conn, run_id=self.run, cwd=self.directory)
        team = TeamService(self.conn)
        lease, thread = self.lease["lease_id"], "reviewer"
        for attempt in (1, 2):
            for index in (1, 2):
                turn = f"failure-{attempt}-{index}"
                self.conn.execute("INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,"
                                  "human,interrupt,state,method,turn_id) VALUES(?,?,?,?,?,0,0,'acknowledged',"
                                  "'turn/start',?)", (self.run, turn, thread, turn, digest, turn))
                self.roots._ended_turn(thread, {"id": turn, "status": "failed"})
            self.conn.execute("UPDATE leases SET expires_at=? WHERE lease_id=?", (clock.iso_after(0.05), lease))
            time.sleep(0.08)
            if attempt == 1:
                self.conn.execute("UPDATE leases SET principal='another-thread' WHERE lease_id=?", (lease,))
                with self.assertRaises(ScopeDenied):
                    team.settle(run_id=self.run)
                self.conn.execute("UPDATE leases SET principal=? WHERE lease_id=?", (thread, lease))
            result = team.settle(run_id=self.run)
            self.assertEqual(result["lapsed"], [self.task])
            self.assertEqual(result["requeued" if attempt == 1 else "exhausted"], [self.task])
            self.assertEqual(team.settle(run_id=self.run), {})
            if attempt == 1:
                with patch("gx_core.p0.roots.tempfile.gettempdir", return_value=str(self.directory)):
                    successor = self.roots._claim(self.task)
                lease, thread = successor["lease_id"], "successor"
                self.assertNotEqual(lease, self.lease["lease_id"])
                self.roots._bind(successor, {"id": thread, "sessionId": thread,
                                             "source": "appServer", "cwd": successor["cwd"]})
        events = self.conn.execute("SELECT payload_json FROM events WHERE kind='task.attempt_lapsed' ORDER BY event_id")
        self.assertEqual([json.loads(r[0])["attempt"] for r in events], [1, 2])
        status = self.conn.execute("SELECT status,attempts_consumed FROM tasks WHERE task_id=?", (self.task,)).fetchone()
        self.assertEqual(tuple(status), ("BLOCKED", 2))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM science_settlements "
                                           "WHERE kind='infrastructure_failure'").fetchone()[0], 4)
