"""Native product callbacks through the real grid@3 workflow and SQLite transaction owners."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gx_core import budgets
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import ScopeDenied
from gx_core.hostobs.app_server_client import AppServerTimeout, NO_TOOL_RESPONSE, ReceivedMessage
from gx_core.orchestration.service import default_registry
from gx_core.p0 import intent, schema, terminal_products
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store
from workflows.science_grid_v3 import products


class TerminalProductTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.db = self.directory / "gx.db"
        self.open()
        schema.install(self.conn)
        workflow = default_registry().get("science_grid", "3")
        self.run = records.create_run(self.conn, objective="Interpret a failed synthetic measurement", project_ref="v31",
            actor="backend", workflow_id="science_grid", workflow_version="3", pins={"workflow_sha256": workflow.digest()})
        self.conn.execute("UPDATE runs SET status='ACTIVE' WHERE run_id=?", (self.run,))
        intent.bind_main(self.conn, self.run, "coordinator")
        self.input = self.put("grid", {"synthetic": True, "candidates": [{"x": 1, "candidate_id": "x"}]})
        proposal = self.put("proposal", {"method": "fixture-grid@1", "threshold": 0, "author_principal": "author",
            "paths": [{"id": "path", "input_ref": self.input, "prediction": "One score crosses the cutoff.",
                       "expected_reaches_threshold": True}]})
        self.proposal = proposal
        evidence = self.put("evidence", {"proposal_ref": proposal, "routes": [{"path_id": "path", "job_id": "fixture",
            "status": "FAILED", "outcome_kind": "measurement_failure", "input_ref": self.input}]})
        task_input, self.contract = products.for_review(self.conn, self.run, {
            "proposal_ref": proposal, "evidence_refs": [evidence], "round": 0, "max_rounds": 1})
        self.task = records.create_task(self.conn, run_id=self.run, actor="backend", kind="research_round",
            role="evidence_reviewer", task_input=task_input, acceptance={"validate_workflow_product": True},
            required_artifacts=["science_assessment"], max_attempts=1)
        lease = store.issue_lease(self.conn, task_id=self.task, attempt=1, seconds=3600, owner="gx:p0-dispatch",
                                  principal="reviewer", worker_role="evidence_reviewer")
        self.lease = lease["lease_id"]
        self.conn.execute("UPDATE tasks SET status='CLAIMED',attempt=1,lease_id=?,owner=? WHERE task_id=?",
                          (self.lease, lease["owner"], self.task))
        self.conn.execute("INSERT INTO p0_roots(root_key,run_id,task_id,attempt,lease_id,thread_id,cwd,role,model,"
                          "effort,brief,state) VALUES('root',?,?,1,?,'reviewer',?,'evidence_reviewer','fixture',"
                          "'high','Inspect the failed measurement','bound')", (self.run, self.task, self.lease, str(self.directory)))
        self.roots = self.native()
        self.roots._allow_refs("root", task_input)
        self.good = {"findings_by_path": {"path": {"reason": "No valid score was observed.",
                                                   "risk": "The hypothesis remains untested."}}}

    def open(self):
        self.conn = store.open_store(self.db)
        self.addCleanup(self.conn.close)

    def native(self, active=True):
        roots = Roots(self.conn, run_id=self.run, cwd=self.directory)
        if active:
            roots.active_turns["reviewer"] = "turn"
        return roots

    def put(self, kind, body):
        return ArtifactService(self.conn).put(run_id=self.run, kind=kind, media_type="application/json",
            content=ids.canonical_json(body), origin="HOST_PUT", put_by="author").artifact_ref

    def frame(self, call, args, tool="gx_submit_product", thread="reviewer"):
        return ReceivedMessage(1, {"method": "item/tool/call", "id": 10, "params": {"threadId": thread,
            "turnId": "turn", "callId": call, "tool": tool, "arguments": args,
            "namespace": "gx" if tool == "gx_submit_product" else None}})

    def submit(self, body, call="submit"):
        return self.roots._callback(self.frame(call, {"product": body}))

    def table(self, name):
        return [dict(row) for row in self.conn.execute("SELECT * FROM " + name)]

    def test_structural_error_dedupes_after_rollback_and_cold_correction_reuses_lease(self):
        with patch.object(ArtifactService, "put", side_effect=AssertionError("published invalid product")):
            rejected = self.submit({}, "bad")
        self.assertFalse(rejected["success"])
        error = json.loads(rejected["contentItems"][0]["text"])
        self.assertEqual((error["code"], error["failure_count"], error["max_failures"], error["contract_digest"]),
                         ("SCHEMA_INVALID", 1, 2, self.contract.digest))
        self.assertEqual(self.submit({}, "bad"), rejected)
        self.assertFalse(self.submit(self.good, "bad")["success"])
        self.assertEqual(len(self.table("p0_product_repairs")), 1)
        for name in ("submissions", "p0_root_calls", "p0_root_terminals", "jobs"):
            self.assertEqual(self.table(name), [])
        self.assertEqual(store.validate_lease(self.conn, self.lease)["attempt"], 1)
        self.conn.close()
        self.open()
        self.roots = self.native()
        self.assertEqual(self.submit({}, "bad"), rejected)
        self.assertIs(self.submit(self.good, "corrected"), NO_TOOL_RESPONSE)
        saved, = self.table("submissions")
        self.assertEqual((saved["result"], saved["attempt"], saved["lease_id"]), ("ACCEPTED", 1, self.lease))
        ref, = json.loads(saved["artifact_refs_json"])
        body = ArtifactService(self.conn).read_json(ref)
        self.assertEqual((body["findings"][0]["supported"], body["findings"][0]["measurement_valid"]), (None, False))
        self.assertEqual(saved["content_sha256"], ids.sha256_of_obj(body))
        self.assertEqual(len(self.table("leases")), 1)

    def test_only_a_live_authorized_task_may_replay_reads_or_charge_failures(self):
        read = self.frame("read", {"ref": self.proposal}, tool="gx_read_artifact")
        self.assertTrue(self.roots._callback(read)["success"])
        refused = self.roots._callback(self.frame("bad", {"product": {}}, thread="other"))
        self.assertFalse(refused["success"])
        self.assertEqual(self.table("p0_product_repairs"), [])
        self.assertIs(self.submit(self.good), NO_TOOL_RESPONSE)
        with self.assertRaises(ScopeDenied):
            self.roots._call(read.message["params"])
        self.assertIs(self.roots._callback(read), NO_TOOL_RESPONSE)
        self.assertIs(self.roots._callback(self.frame("other", {"ref": self.input}, "gx_read_artifact")), NO_TOOL_RESPONSE)
        self.assertEqual(len(self.table("p0_root_calls")), 2)
        self.roots = self.native(active=False)
        self.assertIs(self.submit(self.good), NO_TOOL_RESPONSE)
        self.assertEqual(len(self.table("submissions")), 1)

    def test_only_namespaced_submission_is_direct_and_other_feature_choices_survive(self):
        config = terminal_products.direct_tool_config({"features": {"code_mode": {"enabled": True,
            "direct_only_tool_namespaces": ["documents", "gx"]}}})
        self.assertEqual(config["features.code_mode.direct_only_tool_namespaces"], ["documents", "gx"])
        self.assertIs(terminal_products.direct_tool_config({"features": {"code_mode": False}})["features.code_mode.enabled"], False)
        self.roots._product_tool_config = config
        with patch("gx_core.hostobs.host_policy.p0_provider_for_run", return_value="test"):
            settings = self.roots._settings(self.roots._binding("reviewer"), new=True)
        direct, = [tool for tool in settings["dynamicTools"] if tool["type"] == "namespace"]
        self.assertEqual(direct["name"], "gx")
        submit, = direct["tools"]
        self.assertEqual((submit["name"], submit["inputSchema"]["properties"]["product"]),
                         ("gx_submit_product", self.contract.schema()))
        self.assertIn("gx_read_artifact", [tool["name"] for tool in settings["dynamicTools"] if tool["type"] == "function"])
        for namespace, tool, args in ((None, "gx_submit_product", {"product": self.good}),
                                     ("other", "gx_submit_product", {"product": self.good}),
                                     ("gx", "gx_read_artifact", {"ref": self.proposal})):
            frame = self.frame("blocked", args, tool)
            frame.message["params"]["namespace"] = namespace
            self.assertFalse(self.roots._callback(frame)["success"])
        self.assertEqual(self.table("p0_root_calls"), [])
        self.assertIs(self.submit(self.good), NO_TOOL_RESPONSE)

    def test_limit_is_configurable_and_closes_only_one_attempt_with_no_scientific_verdict(self):
        self.conn.execute("UPDATE runs SET inputs_json=? WHERE run_id=?", (ids.canonical_json(
            {"inputs": {"p0_spec": {"max_product_structural_failures": 3}}}), self.run))
        self.roots = self.native()
        for count in (1, 2):
            self.assertFalse(self.submit({}, str(count))["success"])
        self.assertIs(self.submit({}, "3"), NO_TOOL_RESPONSE)
        self.assertIs(self.submit({}, "3"), NO_TOOL_RESPONSE)
        self.assertIs(self.submit(self.good, "late"), NO_TOOL_RESPONSE)
        self.assertEqual(self.table("p0_root_terminals")[0]["reason"], "GX_REPAIR_LIMIT")
        self.assertEqual(len(self.table("p0_product_repairs")), 3)
        task, = self.table("tasks")
        self.assertEqual((task["status"], task["attempts_consumed"], task["attempt"], task["lease_id"]),
                         ("BLOCKED", 1, 1, None))
        self.assertFalse(any(a["task_id"] == self.task for a in self.table("artifacts")))
        self.assertEqual(self.table("submissions"), [])
        self.assertEqual(self.table("jobs"), [])

    def test_receipt_insert_failure_rolls_back_terminal_and_files_and_retry_closes_despite_diagnostic_error(self):
        before = set(self.directory.rglob("*.json"))
        self.conn.execute("CREATE TEMP TRIGGER fail_receipt BEFORE INSERT ON p0_root_calls BEGIN "
                          "SELECT RAISE(ABORT,'receipt interrupted'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit(self.good)
        self.assertEqual(set(self.directory.rglob("*.json")), before)
        for name in ("p0_root_terminals", "p0_root_calls", "submissions"):
            self.assertEqual(self.table(name), [])
        self.assertFalse(self.roots.terminal_products.due)
        self.conn.execute("DROP TRIGGER fail_receipt")
        with patch.object(self.roots.delivery.attribution, "outcome", side_effect=RuntimeError("unavailable observer")):
            with self.assertLogs("gx_core.p0.roots", "WARNING"):
                self.assertIs(self.submit(self.good), NO_TOOL_RESPONSE)
        self.assertEqual(self.table("p0_root_terminals")[0]["reason"], "GX_ACCEPTED")

    def test_reviewer_independence_refusal_is_terminal_and_not_a_root_failure(self):
        self.conn.execute("UPDATE p0_intent_main SET thread_id='reviewer' WHERE run_id=?", (self.run,))
        self.assertIs(self.submit(self.good), NO_TOOL_RESPONSE)
        sub, = self.table("submissions")
        self.assertEqual(sub["result"], "REJECTED")
        verdict = ArtifactService(self.conn).read_json(sub["records_artifact_ref"])
        self.assertIn("independent", verdict["records"][0]["message"])
        self.assertEqual(self.table("p0_root_terminals")[0]["reason"], "GX_TERMINAL_REJECTED")
        self.roots._ended_turn("reviewer", {"id": "turn", "status": "interrupted"})
        self.assertFalse(self.table("science_settlements"))
        self.assertFalse(self.table("p0_product_repairs"))

    def test_goal_timeout_still_interrupts_and_reconciles_only_original_turn(self):
        self.assertIs(self.submit(self.good), NO_TOOL_RESPONSE)
        calls = []
        def rpc(method, params, **kwargs):
            self.assertFalse(self.conn.in_transaction)
            self.assertEqual(params["threadId"], "reviewer")
            calls.append(method)
            if method == "thread/goal/get" and calls.count(method) == 1:
                raise AppServerTimeout("lost goal receipt")
            if method == "thread/goal/get":
                return {"goal": {"status": "active"}}
            if method == "thread/goal/set":
                return {"goal": {"status": "complete"}}
            if method == "turn/interrupt":
                self.assertEqual(params["turnId"], "turn")
                raise AppServerTimeout("lost interrupt receipt")
            self.assertEqual(method, "thread/turns/list")
            return {"data": [{"id": "another", "status": "inProgress"}, {"id": "turn", "status": "interrupted"}]}
        self.roots._request = rpc
        self.roots.terminal_products.flush()
        cut, = self.table("p0_root_terminals")
        self.assertEqual((cut["goal_last_error"], cut["interrupt_last_error"], cut["interrupt_attempts"]),
                         ("timeout", "timeout", 1))
        self.assertIsNone(cut["goal_checked_at"])
        self.assertIsNone(cut["interrupt_ack_at"])
        self.roots = self.native(active=False)
        self.roots._request = rpc
        self.roots.terminal_products.flush()
        self.assertEqual(calls, ["thread/goal/get", "turn/interrupt", "thread/goal/get", "thread/goal/set", "thread/turns/list"])
        self.assertEqual(terminal_products.pending(self.conn, self.run), [])
        self.assertEqual(self.table("p0_root_turn_usage")[0]["terminal_status"], "interrupted")
        self.assertEqual(len(self.table("submissions")), 1)

    def test_permanently_missing_replies_are_bounded_and_never_answer_the_tool(self):
        self.assertIs(self.submit(self.good), NO_TOOL_RESPONSE)
        owner = self.roots.terminal_products
        owner.retry_seconds = 0
        self.roots.client = SimpleNamespace(response_received=lambda unused: None)
        calls = []
        def noluck(method, params, **kwargs):
            calls.append(method)
            if method == "thread/turns/list":
                return {"data": [{"id": "turn", "status": "inProgress"}]}
            raise AppServerTimeout("lost", request_id=len(calls))
        self.roots._request = noluck
        for _ in range(12):
            owner.flush()
        self.assertEqual(calls.count("turn/interrupt"), 2)
        self.assertEqual(calls.count("thread/goal/get"), 1)
        self.assertEqual(len(owner.late_rpcs), 3)
        self.assertEqual(self.table("p0_root_terminals")[0]["interrupt_attempts"], 2)
        self.assertTrue(terminal_products.pending(self.conn, self.run))

    def test_child_charge_waits_for_verified_terminal_child_usage(self):
        budget = budgets.create_root(self.conn, run_id=self.run, profile_name="p0", actor="test",
                                     limits={"tokens": 1000, "api_calls": 1})
        self.conn.execute("UPDATE runs SET budget_ref=? WHERE run_id=?", (budget, self.run))
        self.roots.token_budget = 1000
        with store.transaction(self.conn):
            ref, tokens, prior = self.roots.delivery._reserve("reviewer", "start", self.roots._binding("reviewer"))
            self.conn.execute("INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,"
                              "human,interrupt,state,method,turn_id,reservation_ref,token_budget,prior_tokens) "
                              "VALUES(?,'start','reviewer','start','fixture',0,0,'acknowledged','turn/start','turn',?,?,?)",
                              (self.run, ref, tokens, prior))
        def event(method, params):
            self.roots._observe(ReceivedMessage(1, {"method": method, "params": params}))
        event("turn/started", {"threadId": "reviewer", "turn": {"id": "turn"}})
        event("item/started", {"threadId": "reviewer", "turnId": "turn", "item": {
            "type": "subAgentActivity", "kind": "started", "agentThreadId": "child"}})
        event("turn/started", {"threadId": "child", "turn": {"id": "child-turn", "rootTurnId": "turn"}})
        child = {"id": "child", "sessionId": "reviewer", "parentThreadId": "reviewer",
                 "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "reviewer"}}}}
        self.roots.delivery.verify_descendants(requested={"child": child})
        for thread, turn, total in (("reviewer", "turn", 70), ("child", "child-turn", 183)):
            event("rawResponse/completed", {"threadId": thread, "turnId": turn, "responseId": thread,
                                             "usage": {"totalTokens": total}})
            event("thread/tokenUsage/updated", {"threadId": thread, "turnId": turn, "tokenUsage": {"total": {"totalTokens": total}}})
        self.assertIs(self.submit(self.good), NO_TOOL_RESPONSE)
        event("turn/completed", {"threadId": "reviewer", "turn": {"id": "turn", "status": "interrupted"}})
        self.assertTrue(self.roots.has_pending_children())
        self.assertEqual(self.table("p0_meter_native_turns")[0]["budget_status"], "HELD")
        event("turn/completed", {"threadId": "child", "turn": {"id": "child-turn", "status": "completed"}})
        self.assertFalse(self.roots.has_pending_children())
        self.assertEqual(budgets.state(self.conn, budget)["spent"], {"tokens": 253, "api_calls": 1})
        self.assertEqual(self.table("p0_meter_native_turns")[0]["confidence"], "PROVIDER_CONFIRMED")
