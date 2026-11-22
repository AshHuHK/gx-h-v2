"""Durable binding, crash recovery, and trusted callbacks on a real GX database."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gx_core.errors import OutcomeUnknown, ScopeDenied
from gx_core.hostobs.app_server_client import ReceivedMessage
from gx_core.p0 import schema
from gx_core.p0.roots import Roots, require_binding
from gx_core.persistence import records, store


class DurableRoots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.open_store(Path(self.tmp.name)/"test.db")
        self.addCleanup(self.conn.close)
        schema.install(self.conn)
        self.run = records.create_run(self.conn, objective="root receipts", project_ref="test", actor="test")
        self.conn.execute("UPDATE runs SET status='ACTIVE' WHERE run_id=?", (self.run,))
        self.task = records.create_task(self.conn, run_id=self.run, actor="test", kind="research_round",
                                        role="evidence_reviewer", task_input={"instruction":"read original"},
                                        required_artifacts=["science_assessment"], max_attempts=2)
        store.transition(self.conn, machine="task", subject_id=self.task, to_state="READY", actor="backend",
                         reason="fixture")
        self.roots = Roots(self.conn, run_id=self.run, cwd=self.tmp.name)
        tmp = patch("gx_core.p0.roots.tempfile.gettempdir", return_value=self.tmp.name)
        tmp.start()
        self.addCleanup(tmp.stop)
        self.mock_profile = patch.dict("sys.modules", {"gx_core.p0.profiles":SimpleNamespace(
            role_profile=lambda role:{"model":"unit-model","effort":"xhigh"},provider_name=lambda:"unit")})
        self.mock_profile.start()
        self.addCleanup(self.mock_profile.stop)
        self.roots.client = SimpleNamespace(transport=SimpleNamespace(process=SimpleNamespace(pid=os.getpid())))
        self.serial = 0

    def bind(self):
        row = self.roots._claim(self.task)
        thread = self.roots._materialize(row)
        self.roots.active_turns[thread] = "turn"
        return self.roots._binding(thread)

    def fake(self, method, p):
        self.assertFalse(self.conn.in_transaction, method)
        if method == "thread/list":
            return {"data":[]}
        self.assertEqual(method,"thread/start")
        self.serial += 1
        tid = f"root-{self.serial}"
        return {"thread":{"id":tid,"sessionId":tid,"cwd":p["cwd"],"source":"appServer"},
                "model":p["model"],"modelProvider":"unit","reasoningEffort":"xhigh",
                "approvalPolicy":"never","sandbox":{"type":"readOnly","networkAccess":False}}

    def test_different_attempt_separate_root_and_lease(self):
        self.roots._request = self.fake
        first = self.bind()
        self.assertEqual(self.roots._claim(self.task)["thread_id"], first["thread_id"])
        with store.transaction(self.conn):
            store.release_lease(self.conn, first["lease_id"])
            store.transition(self.conn, machine="task",subject_id=self.task,to_state="READY",actor="backend",
                             reason="changed input",extra_updates={"owner":None,"lease_id":None})
        second = self.bind()
        self.assertNotEqual(first["thread_id"],second["thread_id"])
        self.assertEqual(second["attempt"],2)
        self.assertEqual(self.roots.thread_for_lease(self.task,first["lease_id"]),first["thread_id"])
        with self.assertRaises(ScopeDenied):
            require_binding(self.conn, self.run, first["thread_id"])
        self.assertEqual(require_binding(self.conn,self.run,second["thread_id"])["lease_id"],second["lease_id"])

    def test_only_current_root_and_actual_turn_can_write(self):
        self.roots._request = self.fake
        row = self.bind()
        self.roots.worker_tools.append({"name":"probe"})
        written = []
        def callback(binding,tool,args,envelope):
            written.append(binding["lease_id"])
            return {"turn":envelope["turn_id"]}
        self.roots.worker_call = callback
        def call(thread,turn,args=None):
            return self.roots._callback(ReceivedMessage(1,{"method":"item/tool/call","params":{
                "callId":"call","threadId":thread,"turnId":turn,"tool":"probe","arguments":args or {}}}))
        self.assertFalse(call("child","turn",{"thread_id":row["thread_id"]})["success"])
        self.assertFalse(call(row["thread_id"],"fake")["success"])
        for _ in range(2):
            self.assertTrue(call(row["thread_id"],"turn")["success"])
        self.assertFalse(call(row["thread_id"],"turn",{"replay":"conflict"})["success"])
        self.assertEqual(written,[row["lease_id"]])

    def test_lost_creation_checks_official_index_and_preserves_original_attempt(self):
        row = self.roots._claim(self.task)
        def lost(method,p):
            self.assertEqual(method,"thread/start")
            raise TimeoutError()
        self.roots._request = lost
        with self.assertRaises(TimeoutError):
            self.roots._materialize(row)
        self.roots._request = self.fake
        with self.assertRaises(OutcomeUnknown):
            self.roots._materialize(self.roots._row(row["root_key"]))
        self.conn.execute("UPDATE p0_root_creations SET host_pid=-1 WHERE cwd=?",(row["cwd"],))
        tid = self.roots._materialize(self.roots._row(row["root_key"]))
        self.assertEqual(self.roots._binding(tid)["lease_id"],row["lease_id"])
        self.assertEqual(self.conn.execute("SELECT outcome FROM p0_root_creations WHERE cwd=?",
                                           (row["cwd"],)).fetchone()[0],"orphan_before_input")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM p0_root_deliveries").fetchone()[0],0)

    def test_job_wait_reuses_lease_and_reconciles_wakeup(self):
        self.roots._request = self.fake
        row = self.bind()
        def job(job_id,lease):
            self.conn.execute("INSERT INTO jobs(job_id,run_id,task_id,lease_ref,capability_id,capability_version,"
                              "status,idempotency_key,submit_intent_json,input_manifest_ref,created_at)"
                              "VALUES(?,?,?,?, 'grid','1','RUNNING',?,'{}','ref','now')",
                              (job_id,self.run,self.task,lease,job_id))
        job("original",row["lease_id"])
        job("foreign","another-lease")
        with self.assertRaises(ScopeDenied):
            self.roots._park_job(row,"foreign")
        self.assertTrue(self.roots._park_job(row,"original")["waiting"])
        self.conn.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00Z' WHERE lease_id=?",(row["lease_id"],))
        parked = self.roots.dispatch(self.task)
        self.assertEqual(parked["lease_id"],row["lease_id"])
        self.assertEqual(parked["wait_job_id"],"original")
        self.conn.execute("UPDATE jobs SET status='SUCCEEDED' WHERE job_id='original'")
        self.roots.active_turns.clear()
        goal_budget = []
        self.roots._goal_for_budget = lambda tid,budget: goal_budget.append((tid,budget))
        calls = []
        def deliver(thread,text,*,event_id,interrupt=False):
            calls.append(event_id)
            if len(calls)==1:
                raise TimeoutError("receipt pending")
            return {"turn_id":"wake","token_budget":50}
        self.roots.deliver=deliver
        with self.assertRaises(TimeoutError):
            self.roots.dispatch(self.task)
        self.assertEqual(self.roots._binding(row["thread_id"])["wait_job_id"],"original")
        wake=self.roots.dispatch(self.task)
        self.assertEqual(wake["turn_id"],"wake")
        self.assertIsNone(wake["wait_job_id"])
        self.assertEqual(calls[0],calls[1])
        self.assertEqual(wake["attempt"],1)
        self.assertEqual(goal_budget,[(row["thread_id"],50)])

    def test_main_has_no_worker_tools_or_goal(self):
        self.roots._request = self.fake
        tid = self.roots.ensure_main()
        row = self.roots._binding(tid)
        settings = self.roots._settings(row,new=True)
        self.assertFalse(settings["config"]["features.goals"])
        self.assertEqual(settings["dynamicTools"],[])
        self.assertEqual(settings["environments"],[])
        self.assertEqual(settings["sandbox"],"read-only")
        self.assertFalse(self.conn.execute("SELECT 1 FROM p0_root_deliveries").fetchone())


if __name__ == "__main__":
    unittest.main()
