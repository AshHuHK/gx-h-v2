"""V6's native adoption transaction, run on a Goal created by its public service."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import BudgetExceeded, GxError, HoldActive, IdempotencyConflict, SchemaInvalid, ScopeDenied, StaleRevision
from gx_core.p0 import schema, metering, v6_branch_budget, v6_decisions as decisions, v6_delegation, v6_goal
from gx_core.p0.roots import Roots
from gx_core.persistence import store


def branch(name="sample", jobs=1):
    return {"op": "open_branch", "branch": "local:" + name, "hypothesis": "coverage differs",
            "alternative": "coverage agrees", "method_family": "inspection",
            "discriminating_observation": "per-chain coordinate count",
            "budget_limits": {"job_submissions": jobs, "cpu_seconds": 1},
            "stop_condition": "after comparing counts"}


def draft(*ops):
    return {"rationale": "Measure coordinate coverage before interpreting the fit",
            "evidence_refs": [], "ops": list(ops), "mode": "amend" if ops else "resolve",
            "reconsider_when": [{"event": "evidence_added", "subject_ref": "run"}]}


def bind_native_fixture(db, run_id, directory, *, task_id=None):
    task = task_id or db.execute("SELECT task_id FROM tasks WHERE run_id=? AND kind='decide_revision' "
                           "AND status='READY'", (run_id,)).fetchone()[0]
    thread = "thread_" + task
    turn = "turn_" + task
    with store.transaction(db, transaction_owner="V6Test", operation="bind_native_fixture"):
        lease = store.issue_lease(db, task_id=task, owner=thread, principal=thread, worker_role="planner",
                                  seconds=300, attempt=1)["lease_id"]
        store.transition(db, machine="task", subject_id=task, to_state="CLAIMED", actor=thread,
                         extra_updates={"attempt": 1, "lease_id": lease, "owner": thread}, bump=False)
        from gx_core.p0.v6_native import PREFIX
        body = db.execute("SELECT input_json FROM tasks WHERE task_id=?", (task,)).fetchone()[0]
        brief = PREFIX + ids.canonical_json(json.loads(body)["brief"])
        db.execute("""INSERT INTO p0_roots(root_key,run_id,task_id,attempt,lease_id,thread_id,cwd,role,
            model,effort,brief,state) VALUES(?,?,?,1,?,?,?,'planner','test','xhigh',?,'bound')""",
            ("root-" + task, run_id, task, lease, thread, str(Path(directory)/task), brief))
        db.execute("""INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,
            human,interrupt,state,method,turn_id) VALUES(?,?,?,?,?,0,0,'acknowledged','turn/start',?)""",
            (run_id, "start-" + task, thread, "client-" + task, hashlib.sha256(brief.encode()).hexdigest(), turn))
    return dict(run_id=run_id, task_id=task, lease_id=lease, thread_id=thread, turn_id=turn, call_id="proposal")


class Adoption(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "gx.db"
        self.db = store.open_store(self.path)
        self.addCleanup(self.db.close)
        schema.install(self.db)
        self.run = v6_goal.start(self.db,
            {"project": "v6-test", "mode": "goal", "objective": "What do the observed coordinates support?",
             "capabilities": []}, run_key="unit", current=None)["run_id"]
        self.call = self.bind()

    def bind(self):
        return bind_native_fixture(self.db, self.run, self.path.parent)

    def next_call(self):
        ref = ArtifactService(self.db).put(run_id=self.run, content='{"coverage":3}', kind="observation",
            media_type="application/json", put_by="test", origin="INGEST").artifact_ref
        v6_goal.record_observation(self.db, self.run, ref, source="new-evidence")
        self.assertIsNotNone(v6_goal.ensure_task(self.db, self.run))
        return self.bind(), ref

    def native_branch(self, extra):
        op = branch()
        op["budget_limits"].update(extra)
        first = decisions.adopt(self.db, **self.call, decision=draft(op))
        bid = first["aliases"]["local:sample"]
        parent, _ = self.next_call()
        plan = v6_delegation.prepare(self.db, **dict(parent, call_id="delegate"),
                                     payload={"branch_id":bid, "question":"Review the inventory"})
        receipt = v6_delegation.commit(self.db, plan)
        call = bind_native_fixture(self.db, self.run, self.path.parent, task_id=receipt["task_id"])
        self.db.execute("UPDATE p0_roots SET tool_names_json=? WHERE task_id=?",
                        (json.dumps(["gx_decide_revision"]), call["task_id"]))
        return bid, call

    def native_send(self, db, call, event):
        roots = Roots(db, run_id=self.run, cwd=self.path.parent, token_budget=1000)
        row, fresh = roots.delivery._prepare(call["thread_id"], "New measurement", event, False,
                                            False, "client-" + event, None)
        return row["reservation_ref"], row["token_budget"], fresh

    def test_branch_native_reservation_concurrent_replay_settle_and_next_call(self):
        bid, call = self.native_branch({"tokens":150, "api_calls":2})
        barrier = Barrier(2)
        def send(_):
            with closing(store.connect(self.path)) as db:
                barrier.wait(timeout=5)
                return self.native_send(db, call, "next")
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(send, range(2)))
        self.assertEqual({r[2] for r in receipts}, {False, True})
        first, = {r[0] for r in receipts}
        self.assertEqual({r[1] for r in receipts},{150})
        held = v6_branch_budget.exposure(self.db,bid)
        self.assertEqual((held["tokens"],held["api_calls"],held["worker_tasks"]), (150,1,1))
        with store.transaction(self.db):
            self.db.execute("UPDATE p0_root_deliveries SET state='acknowledged',turn_id='measured' WHERE event_id='next'")
            self.db.execute("INSERT INTO p0_root_turn_usage(thread_id,turn_id,terminal_status,total_tokens) VALUES(?,"
                            "'measured','completed',96)", (call["thread_id"],))
            metering.confirm(self.db, first, 96)
        second, tokens, fresh = self.native_send(self.db,call,"second")
        self.assertNotEqual(second,first)
        self.assertTrue(fresh)
        self.assertEqual(tokens,54)
        with store.transaction(self.db):
            self.db.execute("UPDATE p0_root_deliveries SET state='acknowledged',turn_id='second' WHERE event_id='second'")
            self.db.execute("INSERT INTO p0_root_turn_usage(thread_id,turn_id,terminal_status,total_tokens) VALUES(?,"
                            "'second','completed',100)", (call["thread_id"],))
            metering.confirm(self.db,second,4)
        with self.assertRaises(HoldActive) as rejected:
            self.native_send(self.db,call,"third")
        self.assertEqual(rejected.exception.details["key"],"api_calls")
        used = v6_branch_budget.exposure(self.db,bid)
        self.assertEqual((used["tokens"],used["api_calls"]), (100,2))
        self.assertEqual(self.db.execute("SELECT count(*) FROM budget_reservations WHERE subject_kind='native_turn'").fetchone()[0],2)
        self.assertEqual(self.db.execute("SELECT status FROM runs WHERE run_id=?",(self.run,)).fetchone()[0],"ACTIVE")

    def test_branch_omitted_native_caps_use_run_and_withdrawn_scope_cannot_reserve(self):
        bid, call = self.native_branch({})
        with store.transaction(self.db):
            self.db.execute("UPDATE v6_branches SET status='STOPPING' WHERE branch_id=?", (bid,))
        with self.assertRaisesRegex(ScopeDenied,"no longer admitted"):
            self.native_send(self.db,call,"withdrawn")
        self.assertEqual(self.db.execute("SELECT count(*) FROM budget_reservations WHERE subject_kind='native_turn'").fetchone()[0],0)
        self.db.execute("UPDATE v6_branches SET status='OPEN' WHERE branch_id=?", (bid,))
        _, grant, _ = self.native_send(self.db,call,"fresh")
        self.assertEqual(grant,1000)

    def test_competing_proposals_first_head_and_exact_cold_replay(self):
        calls = [dict(self.call, call_id="first", decision=draft(branch("one"))),
                 dict(self.call, call_id="second", decision=draft(branch("two")))]
        barrier = Barrier(2)
        def compete(args):
            with closing(store.connect(self.path)) as db:
                plan = decisions.prepare(db, **args)
                barrier.wait(timeout=10)
                try:
                    return decisions.commit(db, plan)
                except GxError as exc:
                    return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            answers = list(pool.map(compete, calls))
        accepted, = [x for x in answers if isinstance(x, dict)]
        rejected, = [x for x in answers if isinstance(x, str)]
        self.assertIn(rejected, {"STALE_REVISION", "SCOPE_DENIED"})
        winning = calls[answers.index(accepted)]
        with closing(store.open_store(self.path)) as fresh:
            schema.install(fresh)
            self.assertEqual(accepted, decisions.adopt(fresh, **winning))
            self.assertEqual(ArtifactService(fresh).read_json(accepted["decision_ref"]), winning["decision"])
            with self.assertRaises(IdempotencyConflict):
                decisions.adopt(fresh, **dict(winning, decision=draft(branch("other"))))
            with self.assertRaises(IdempotencyConflict):
                decisions.adopt(fresh, **dict(winning, turn_id="different"))
        for table in ("v6_graph_versions", "v6_decisions", "submissions", "v6_branches", "checkpoints"):
            self.assertEqual(self.db.execute("SELECT count(*) FROM "+table).fetchone()[0], 1, table)
        self.assertEqual(self.db.execute("SELECT count(*) FROM artifacts WHERE kind='research_decision'").fetchone()[0],1)

    def test_local_aliases_scoped_partial_change_and_neutral_resolve(self):
        one = draft(branch("a"), branch("b"))
        first = decisions.adopt(self.db, **self.call, decision=one)
        a, b = first["aliases"]["local:a"], first["aliases"]["local:b"]
        call, ref = self.next_call()
        grant = v6_delegation.prepare(self.db, **dict(call, call_id="delegate"),
                                      payload={"branch_id": a, "question": "Review the first branch"})
        granted = v6_delegation.commit(self.db, grant)
        call = bind_native_fixture(self.db, self.run, self.path.parent, task_id=granted["task_id"])
        with self.assertRaises(ScopeDenied):
            decisions.adopt(self.db, **call, decision=draft({"op":"stop_branch","branch":b,"reason":"cross scope"}))
        same = draft()
        same["evidence_refs"] = [ref]
        kept = decisions.adopt(self.db, **call, decision=same)
        self.assertEqual(kept["result_head"], first["result_head"])
        self.assertIsNone(kept["checkpoint_id"])
        self.assertEqual([r[0] for r in self.db.execute("SELECT status FROM v6_branches")], ["OPEN","OPEN"])

    def test_prepared_proposal_cannot_outlive_lease_goal_turn_or_funding(self):
        plan = decisions.prepare(self.db, **self.call, decision=draft(branch()))
        for edit, args, message in [
            ("UPDATE leases SET expires_at='2000-01-01T00:00:00Z' WHERE lease_id=?", (self.call["lease_id"],),"lease"),
            ("UPDATE v6_goals SET goal_version=goal_version+1 WHERE run_id=?", (self.run,),"Goal"),
            ("UPDATE budgets SET limits_json='{}' WHERE budget_ref=(SELECT budget_ref FROM runs WHERE run_id=?)", (self.run,),"budget"),
            ("INSERT INTO p0_root_turn_usage(thread_id,turn_id,terminal_status) VALUES(?,?,'completed')",
             (self.call["thread_id"], self.call["turn_id"]),"turn")]:
            with self.subTest(message=message):
                self.db.execute("SAVEPOINT fault")
                try:
                    self.db.execute(edit,args)
                    with self.assertRaises(GxError):
                        decisions.commit(self.db, plan)
                    self.assertIsNone(decisions.head(self.db,self.run))
                finally:
                    self.db.execute("ROLLBACK TO fault")
                    self.db.execute("RELEASE fault")
        decisions.commit(self.db,plan)

    def test_malformed_and_unfunded_decisions_have_no_side_effects(self):
        unknown = draft(branch())
        unknown["evidence_refs"] = ["gxa_other"]
        watch = draft(branch())
        watch["reconsider_when"][0]["subject_ref"] = "local:missing"
        for decision in (unknown, watch, draft(branch(jobs=10001)), draft(branch(), branch()),
                         draft({"op":"invent"}), draft("open"), draft([]),
                         draft({"op":"close_run","reason":"stop","claim_refs":["secret"],"unresolved":[]})):
            with self.subTest(decision=decision):
                with self.assertRaises((ScopeDenied, SchemaInvalid, BudgetExceeded)):
                    decisions.adopt(self.db, **self.call, decision=decision)
                self.assertIsNone(decisions.head(self.db,self.run))
        self.assertEqual(self.db.execute("SELECT count(*) FROM v6_branches").fetchone()[0],0)

    def test_durable_stop_intent_and_retained_other_branch(self):
        accepted = decisions.adopt(self.db, **self.call, decision=draft(branch("a"),branch("b")))
        a,b = accepted["aliases"].values()
        args, ref = self.next_call()
        op = {"op":"stop_branch","branch":a,"reason":"Hypothesis resolved"}
        change = draft(op)
        change["evidence_refs"] = [ref]
        revised = decisions.adopt(self.db, **args, decision=change)
        with closing(store.connect(self.path)) as fresh:
            stopped = fresh.execute("SELECT status,stop_decision_ref,stop_op_digest FROM v6_branches "
                                    "WHERE branch_id=?", (a,)).fetchone()
            self.assertEqual(tuple(stopped), ("STOPPING",revised["decision_ref"],ids.sha256_of_obj(op)))
            self.assertEqual(fresh.execute("SELECT status FROM v6_branches WHERE branch_id=?", (b,)).fetchone()[0],"OPEN")

    def test_cyclic_draft_disallowed_before_outbox(self):
        caps = {"inspect@1": "pin"}
        node = {"op": "add_node", "node": "local:x", "branch": "local:b", "inputs": [],
                "depends_on": ["local:y"], "capability": "inspect@1", "parameters": {}, "question": "Is it supported?"}
        cycle = draft(branch("b"), node, dict(node, node="local:y",depends_on=["local:x"]))
        self.db.execute("UPDATE v6_goals SET capabilities_json=?", (json.dumps(caps),))
        with patch("gx_core.p0.v6_jobs.validate_node"), self.assertRaisesRegex(SchemaInvalid,"cycle"):
            decisions.adopt(self.db, **self.call, decision=cycle)
        self.assertIsNone(decisions.head(self.db,self.run))
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox WHERE topic='v6.node'").fetchone()[0],0)

    def test_new_allocation_respects_remaining_obligation_and_unrelated_spend(self):
        decisions.adopt(self.db, **self.call, decision=draft(branch(jobs=2)))
        call, _ = self.next_call()
        with store.transaction(self.db):
            self.db.execute("""UPDATE budgets SET limits_json=json_set(limits_json,'$.job_submissions',5),
                spent_json=json_set(spent_json,'$.job_submissions',3)
                WHERE budget_ref=(SELECT budget_ref FROM runs WHERE run_id=?)""", (self.run,))
        with self.assertRaisesRegex(BudgetExceeded,"job_submissions"):
            decisions.adopt(self.db, **call, decision=draft(branch("other", jobs=2)))


if __name__ == "__main__":
    unittest.main()
