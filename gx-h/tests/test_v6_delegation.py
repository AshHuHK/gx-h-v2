"""Run-Lead and branch-owner callbacks against a real GX database using simulated transport."""
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gx_core.artifacts import ArtifactService
from gx_core.capabilities import shipped
from gx_core.domain import ids
from gx_core.errors import BudgetExceeded, GxError, ScopeDenied
from gx_core.native_release import native_state
from gx_core.p0 import cli, v6_delegation, v6_goal, v6_jobs, v6_native
from gx_core.p0.roots import Roots
from gx_core.persistence import store
from tests.test_v6_decisions import branch, draft


class BranchDelegation(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        state = Path(tmp)
        self.enterContext(native_state(state))
        self.conn = self.enterContext(closing(store.open_store(state / "gx.db")))
        dataset = state / "grid.json"
        dataset.write_text(json.dumps({"fixture_version":"1","synthetic":True,"method_id":"fixture-grid",
            "method_version":"1","candidates":[{"candidate_id":"two","x":2}]}))
        started = cli.start(self.conn, state, {"project":"branch-test", "mode":"goal", "objective":"How robust is this comparison?",
            "capabilities":[shipped.GRID], "input_files":[{"path":str(dataset)}]}, run_key="delegation")
        self.run = started["run_id"]
        self.enterContext(patch("gx_core.p0.roots.tempfile.gettempdir", return_value=tmp))
        self.roots = Roots(self.conn, run_id=self.run, cwd=state)
        self.roots.client = SimpleNamespace(transport=SimpleNamespace(process=SimpleNamespace(pid=os.getpid())))
        first_task = self.conn.execute("SELECT task_id FROM v6_decision_tasks WHERE run_id=?", (self.run,)).fetchone()[0]
        first = self.bind(first_task)
        left, right = branch("left"), branch("right")
        left["budget_limits"].update(cpu_seconds=120, worker_tasks=1)
        right["budget_limits"]["worker_tasks"] = 0
        adopted = self.call(first, v6_native.DECIDE, {"decision":draft(left, right)}, "open")
        self.left, self.right = [adopted["aliases"]["local:" + key] for key in ("left", "right")]
        self.shared = self.observe("protocol-note", "run")
        self.left_ref = self.observe("left-correction", self.left)
        self.right_ref = self.observe("right-correction", self.right)
        self.parent = self.bind(v6_goal.ensure_task(self.conn, self.run))
        self.request = {"branch_id": self.left, "question": "Which observations support this branch?"}

    def observe(self, source, scope):
        ref = ArtifactService(self.conn).put(run_id=self.run, kind="measurement", origin="INGEST",
            media_type="application/json", content=json.dumps({"measurement": source}), put_by="fixture").artifact_ref
        v6_goal.record_observation(self.conn, self.run, ref, source=source, scope=scope)
        return ref

    def bind(self, task_id):
        row = self.roots._claim(task_id)
        thread = self.roots._bind(row, {"id": "thread-" + task_id, "cwd": row["cwd"], "source": "appServer"})
        turn = "turn-" + task_id
        self.roots.active_turns[thread] = turn
        self.conn.execute("""INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,
            human,interrupt,state,method,turn_id) VALUES(?,?,?,?,?,0,0,'acknowledged','turn/start',?)""",
            (self.run, turn, thread, task_id, ids.sha256_of_obj(row["brief"]), turn))
        return {"threadId": thread, "turnId": turn}

    def call(self, identity, tool, args, key):
        return self.roots._call({**identity, "callId": key, "tool": tool, "arguments": args,
                                 **({"namespace":"gx"} if tool == v6_native.DECIDE else {})})

    def assign(self, key="assign"):
        return self.call(self.parent, v6_delegation.TOOL["name"], self.request, key)

    def test_explicit_scoped_brief_and_once_only_lease(self):
        self.assertEqual(0, self.conn.execute("SELECT count(*) FROM v6_decision_tasks WHERE scope!='run'").fetchone()[0])
        grant = self.assign()
        self.assertEqual(grant, self.assign())
        with self.assertRaises(ScopeDenied):
            self.assign("another-grant-for-open-task")
        with self.assertRaises(GxError):
            self.call(self.parent, v6_delegation.TOOL["name"], {**self.request, "question":"Changed"}, "assign")
        delegated = self.bind(grant["task_id"])
        packet = self.call(delegated, "gx_task_context", {}, "context")
        self.assertEqual(packet["scope"], "branch:" + self.left)
        self.assertEqual(packet["delegation_question"], self.request["question"])
        self.assertEqual(packet["budget"]["limits"], {"cpu_seconds":120, "job_submissions":1, "worker_tasks":1})
        self.assertEqual([row["branch_id"] for row in packet["branches"]], [self.left])
        self.assertIn(self.shared, packet["evidence_refs"])
        self.assertIn(self.left_ref, packet["evidence_refs"])
        self.assertNotIn(self.right_ref, json.dumps(packet))
        self.assertNotIn("right-correction", json.dumps(packet))
        with self.assertRaises(ScopeDenied):
            self.call(delegated, "gx_read_artifact", {"ref": self.right_ref}, "sibling-read")
        with self.assertRaises(ScopeDenied):
            self.call(delegated, v6_delegation.TOOL["name"], self.request, "subdelegate")
        close = {"op": "close_run", "reason": "no further measurements", "claim_refs": [], "unresolved": []}
        for operation in (branch("escape"), close, {"op":"stop_branch","branch":self.right,"reason":"out of scope"}):
            with self.subTest(op=operation["op"]), self.assertRaises(ScopeDenied):
                self.call(delegated, v6_native.DECIDE, {"decision":draft(operation)}, operation["op"])
        result = self.call(self.parent, v6_native.DECIDE, {"decision":draft()}, "maintain")
        self.assertEqual(result["result_head"], grant["base"])
        self.assertEqual(self.assign(), grant)  # Exact native receipt remains readable after retirement.
        local = draft()
        local["evidence_refs"] = [self.left_ref]
        local["reconsider_when"][0]["subject_ref"] = self.left
        result = self.call(delegated, v6_native.DECIDE, {"decision":local}, "interpret")
        self.assertEqual(result["scope"], grant["scope"])
        self.assertEqual(result["result_head"], grant["base"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events WHERE kind='v6.branch.delegated'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_worker_charge_and_grant_are_atomic_and_replays_do_not_spend(self):
        before = self.conn.execute("SELECT count(*) FROM v6_decision_tasks WHERE scope!='run'").fetchone()[0]
        other = {**self.request, "branch_id":self.right}
        with self.assertRaises(BudgetExceeded):
            self.call(self.parent, v6_delegation.TOOL["name"], other, "no-worker-budget")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM v6_decision_tasks WHERE scope!='run'").fetchone()[0], before)
        grant = self.assign()
        self.assertEqual(self.assign(), grant)
        charged = self.conn.execute("SELECT reservation_ref,status,amounts_json FROM budget_reservations "
                                    "WHERE subject_kind='task' AND subject_id=?", (grant["task_id"],)).fetchall()
        self.assertEqual(len(charged), 1)
        self.assertEqual((charged[0][0],charged[0][1],json.loads(charged[0][2])),
                         (grant["worker_reservation_ref"], "SETTLED", {"worker_tasks":1}))
        delegated = self.bind(grant["task_id"])
        local = draft()
        local["reconsider_when"][0]["subject_ref"] = self.left
        self.call(delegated, v6_native.DECIDE, {"decision":local}, "resolved")
        with self.assertRaises(BudgetExceeded):
            self.assign("another-worker")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM v6_decision_tasks WHERE scope!='run'").fetchone()[0], before+1)

    def test_head_change_expires_competing_task_without_consuming_a_failure(self):
        grant = self.assign()
        delegated = self.bind(grant["task_id"])
        competing = self.call(self.parent, v6_native.DECIDE, {"decision":draft(branch("third"))}, "new-head")
        self.assertGreater(competing["result_head"]["version"], grant["base"]["version"])
        local = draft()
        local["reconsider_when"][0]["subject_ref"] = self.left
        with self.assertRaises(GxError):
            self.call(delegated, v6_native.DECIDE, {"decision":local}, "obsolete")
        task = self.conn.execute("SELECT status,attempt FROM tasks WHERE task_id=?", (grant["task_id"],)).fetchone()
        self.assertEqual(tuple(task), ("CANCELLED", 1))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM leases WHERE task_id=? AND released_at IS NULL",
                                          (grant["task_id"],)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events WHERE kind='v6.branch.delegation_ended' "
                                          "AND subject_id=?", (grant["task_id"],)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT status FROM v6_branches WHERE branch_id=?", (self.right,)).fetchone()[0], "OPEN")

    def test_scoped_owner_dispatches_one_real_job_on_its_fixed_allowance(self):
        grant = self.assign()
        delegated = self.bind(grant["task_id"])
        packet = self.call(delegated, "gx_task_context", {}, "context")
        node = {"op":"add_node","node":"local:compute","branch":self.left,"capability":shipped.GRID,
                "inputs":[packet["materials"][0]["artifact_ref"]], "parameters":{"operation":"initial"},
                "depends_on":[], "question":"What value is observed in the supplied grid?"}
        decision = draft(node)
        decision["reconsider_when"] = [{"event":"job_settled","subject_ref":"local:compute"}]
        accepted = self.call(delegated, v6_native.DECIDE, {"decision":decision}, "dispatch")
        node_id = accepted["aliases"]["local:compute"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            v6_jobs.advance(self.conn, self.run)
            row = self.conn.execute("SELECT status,result_ref,job_id FROM v6_nodes WHERE node_id=?", (node_id,)).fetchone()
            if row["status"] in v6_jobs.NODE_TERMINAL:
                break
            time.sleep(0.05)
        self.assertEqual(row["status"], "SUCCEEDED")
        self.assertTrue(ArtifactService(self.conn).verify(row["result_ref"])["ok"])
        for _ in range(2):
            v6_jobs.advance(self.conn, self.run)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (self.run,)).fetchone()[0], 1)
        self.assertEqual(v6_jobs.branch_exposure(self.conn, self.left)["job_submissions"], 1)
        self.assertEqual(v6_jobs.branch_exposure(self.conn, self.right)["job_submissions"], 0)
        self.assertEqual(self.conn.execute("SELECT status FROM v6_branches WHERE branch_id=?", (self.right,)).fetchone()[0], "OPEN")

    def test_branch_commit_wakes_a_replacement_global_brief(self):
        grant = self.assign()
        delegated = self.bind(grant["task_id"])
        local = draft({"op":"stop_branch", "branch":self.left,"reason":"This branch's question was answered."})
        local["evidence_refs"] = [self.left_ref]
        local["reconsider_when"][0]["subject_ref"] = self.left
        receipt = self.call(delegated, v6_native.DECIDE, {"decision":local}, "local-stop")
        self.assertEqual(receipt["scope"], grant["scope"])
        new = v6_goal.ensure_task(self.conn, self.run)
        self.assertIsNotNone(new)
        replacement = self.bind(new)
        context = self.call(replacement, "gx_task_context", {}, "fresh")
        self.assertEqual(context["head"], receipt["result_head"])
        with self.assertRaises(GxError):
            self.call(self.parent, v6_native.DECIDE, {"decision":draft()}, "obsolete-parent")
        self.assertEqual(self.conn.execute("SELECT status FROM v6_branches WHERE branch_id=?", (self.right,)).fetchone()[0], "OPEN")


if __name__ == "__main__":
    unittest.main()
