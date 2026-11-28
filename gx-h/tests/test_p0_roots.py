"""Durable binding, crash recovery, and trusted callbacks on a real GX database."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gx_core.artifacts.service import ArtifactService
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
        for name,value in (("p0_role_for_run",{"model":"unit-model","effort":"xhigh"}),
                           ("p0_provider_for_run","unit")):
            pinned = patch("gx_core.hostobs.host_policy."+name, return_value=value)
            pinned.start()
            self.addCleanup(pinned.stop)
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
        store.release_lease(self.conn, row["lease_id"])
        self.assertTrue(call(row["thread_id"], "turn")["success"])
        self.assertEqual(written,[row["lease_id"]])

    def child_research(self):
        assets = ArtifactService(self.conn)
        def publish(run, body, media):
            return assets.put(run_id=run, kind="source", media_type=media, content=body,
                              origin="HOST_PUT", put_by="fixture").artifact_ref
        self.original = "μ = 0.25\n" * 3200
        self.source = publish(self.run, self.original, "text/plain; charset=utf-8")
        self.index = publish(self.run, json.dumps({"source": self.source}), "application/json")
        foreign = records.create_run(self.conn, objective="other project", project_ref="other", actor="fixture")
        self.foreign = publish(foreign, "A different project's evidence.", "text/plain")
        self.conn.execute("UPDATE tasks SET input_json=? WHERE task_id=?",
                          (json.dumps({"ref": self.index, "other": self.foreign}), self.task))
        self.roots._request = self.fake
        self.parent = self.bind()
        self.pid = self.parent["thread_id"]
        self.roots.client.run_until = lambda *args, **kwargs: None

    def child_event(self, method, params):
        self.roots._observe(ReceivedMessage(1, {"method": method, "params": params}))

    def child_call(self, thread="child", turn="ct", tool="gx_read_artifact", args=None):
        p = {"threadId": thread, "turnId": turn, "callId": "call-" + tool,
             "tool": tool, "arguments": {"ref": self.index} if args is None else args}
        with patch.object(self.roots, "_request", side_effect=AssertionError("RPC inside callback")):
            response = self.roots._callback(ReceivedMessage(1, {"method": "item/tool/call", "params": p}))
        return response["success"], json.loads(response["contentItems"][0]["text"])

    def child_facts(self, name="child", parent=None):
        parent = parent or self.pid
        return {"id": name, "sessionId": self.pid, "parentThreadId": parent,
                "source": {"subAgent": {"thread_spawn": {"parent_thread_id": parent}}}}

    def child_spawn(self, child="child", parent=None, turn="turn"):
        self.child_event("item/started", {"threadId": parent or self.pid, "turnId": turn,
                         "item": {"type": "subAgentActivity", "kind": "started", "agentThreadId": child}})
        self.child_event("turn/started", {"threadId": child, "turn": {"id": "ct", "rootTurnId": "turn"}})

    def test_child_read_retries_after_verification_and_rechecks_scope_on_every_call(self):
        self.child_research()
        self.child_spawn()
        counts = lambda: tuple(self.conn.execute("SELECT count(*) FROM " + t).fetchone()[0] for t in ("tasks", "leases"))
        before = counts()
        success, result = self.child_call()
        self.assertFalse(success)
        self.assertIn("Retry", result["error"])
        def verify(method, params):
            self.assertFalse(self.conn.in_transaction)
            self.assertEqual((method, params["threadId"]), ("thread/read", "child"))
            return {"thread": {**self.child_facts(), "forkedFromId": self.pid}}
        self.roots._request = verify
        self.roots.poll()
        self.assertEqual(self.child_call(), (True, {"source": self.source}))
        # Reading the index as a child leaves the parent's list untouched.
        self.assertFalse(self.child_call(args={"ref": self.source})[0])
        self.assertTrue(self.child_call(thread=self.pid, turn="turn")[0])
        self.assertTrue(self.child_call(thread=self.pid, turn="turn", tool="gx_task_context", args={})[0])
        ok, page = self.child_call(args={"ref": self.source})
        self.assertTrue(ok)
        self.assertTrue(page["truncated"])
        ok, tail = self.child_call(args={"ref": self.source, "offset": page["metadata"]["next_offset"]})
        self.assertTrue(ok)
        self.assertEqual(page["content"] + tail["content"], self.original)
        self.conn.execute("INSERT INTO p0_root_artifact_reads VALUES(?,?)", (self.parent["root_key"], self.foreign))
        self.assertFalse(self.child_call(args={"ref": self.foreign})[0])
        self.assertFalse(self.child_call(args={"ref": self.source, "root_id": self.pid})[0])
        self.assertFalse(self.child_call(args={"ref": self.source, "offset": True})[0])
        for tool in ("gx_submit_product", "gx_wait_job", "gx_task_context", "custom_worker_write"):
            self.assertFalse(self.child_call(tool=tool, args={})[0])
        with patch.object(self.roots, "_tools_for", return_value=[]):
            self.assertFalse(self.child_call()[0])
        self.conn.execute("DELETE FROM p0_root_artifact_reads WHERE artifact_ref=?", (self.index,))
        self.assertFalse(self.child_call()[0])
        self.assertFalse(self.child_call(thread=self.pid, turn="turn")[0])
        self.conn.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00Z' WHERE lease_id=?", (self.parent["lease_id"],))
        self.assertFalse(self.child_call(args={"ref": self.source})[0])
        self.assertFalse(self.child_call(thread=self.pid, turn="turn", tool="gx_task_context", args={})[0])
        self.conn.execute("UPDATE leases SET expires_at='2100-01-01T00:00:00Z' WHERE lease_id=?", (self.parent["lease_id"],))
        self.conn.execute("UPDATE tasks SET attempt=attempt+1 WHERE task_id=?", (self.task,))
        self.assertFalse(self.child_call(args={"ref": self.source})[0])
        self.assertEqual(counts(), before)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM p0_root_calls WHERE thread_id='child'").fetchone())

    def test_cold_child_requires_parent_activity_metadata_and_current_turn(self):
        self.child_research()
        self.roots = Roots(self.conn, run_id=self.run, cwd=self.tmp.name)
        self.roots.client = SimpleNamespace(run_until=lambda *a, **kw: None)
        requested = []
        metadata = {"child": self.child_facts(), "fork": {**self.child_facts("fork"), "source": "appServer",
                    "forkedFromId": self.pid}, "unknown": self.child_facts("unknown", "different-root"),
                    "unannounced": self.child_facts("unannounced")}
        def official(method, params):
            requested.append((method, params["threadId"]))
            self.assertFalse(self.conn.in_transaction)
            if method == "thread/read":
                return {"thread": metadata[params["threadId"]]}
            if method == "thread/items/list":
                self.assertEqual(params["threadId"], self.pid)
                return {"data": [{"turnId": "turn", "item": {"type": "subAgentActivity", "kind": "started",
                                 "agentThreadId": "child"}}]}
            self.assertEqual((method, params["threadId"]), ("thread/turns/list", "child"))
            return {"data": [{"id": "ct", "rootTurnId": "turn", "status": "inProgress"}]}
        self.roots._request = official
        for name in ("child", "fork", "unknown", "unannounced"):
            self.assertFalse(self.child_call(thread=name)[0])
        self.roots.poll()
        self.assertEqual(self.child_call(), (True, {"source": self.source}))
        for name in ("fork", "unknown", "unannounced"):
            self.assertFalse(self.child_call(thread=name)[0])
        self.assertEqual(requested.count(("thread/read", "child")), 1)
        self.assertFalse(self.child_call(turn="old")[0])
        self.child_event("turn/completed", {"threadId": "child", "turn": {"id": "ct", "status": "completed"}})
        self.child_event("turn/started", {"threadId": "child", "turn": {"id": "ct", "rootTurnId": "turn"}})
        self.assertFalse(self.child_call()[0])
        self.child_spawn("grandchild", parent="child", turn="ct")
        self.assertFalse(self.child_call(thread="grandchild")[0])

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

    def test_provider_admission_requires_pinned_version_and_oauth(self):
        for version,account in (("0.170.0","chatgpt"),("0.169.0","apiKey"),("0.169.0","chatgpt")):
            with self.subTest(version=version,account=account):
                client = MagicMock()
                replies = {"initialize":{"userAgent":f"gx_p0/{version} (Linux)"},
                           "account/read":{"account":{"type":account}}, "config/read":{"config":{}}}
                client.request.side_effect = lambda method,params:replies[method]
                with patch("gx_core.p0.roots.AppServerClient",return_value=client), \
                     patch("gx_core.p0.profiles.clash_env",return_value={}), \
                     patch("gx_core.hostobs.host_policy.p0_provider_for_run",return_value="openai"):
                    roots = Roots(self.conn,run_id=self.run,cwd=self.tmp.name,argv=["codex","app-server"])
                    if version=="0.169.0" and account=="chatgpt":
                        with roots:
                            pass
                    else:
                        with self.assertRaises(ScopeDenied):
                            with roots:
                                self.fail("unexpected provider admission")
                    self.assertTrue(client.close.called)
                    self.assertFalse(any(call.args[0]=="turn/start" for call in client.request.call_args_list))

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
