"""Paid-turn admission and exact provider receipts across faults and old ledgers."""
import json
import tempfile
import unittest
from pathlib import Path

from gx_core import budgets
from gx_core.domain import clock
from gx_core.errors import BudgetExceeded, HoldActive, LeaseExpired
from gx_core.p0 import schema, waits
from gx_core.p0.root_delivery import RootDelivery
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store
from gx_core.teams.service import default_lease_seconds
from test_p0_root_delivery import Peer
from tests import test_p0_roots


class TurnMeterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.db = store.open_store(self.directory / "ledger.sqlite")
        self.addCleanup(self.db.close)
        schema.install(self.db)
        self.run = records.create_run(self.db, objective="Check arithmetic", project_ref="accounting", actor="probe")
        self.budget = budgets.create_root(self.db, run_id=self.run, profile_name="p0", actor="probe",
                                          limits={"tokens":300,"api_calls":2})
        self.db.execute("UPDATE runs SET budget_ref=? WHERE run_id=?", (self.budget, self.run))
        self.peer = Peer(self.db, self.run)
        self.delivery = RootDelivery(self.peer)

    def sample(self, turn, total):
        self.delivery.observe("thread/tokenUsage/updated", {"threadId":"root", "turnId":turn,
                              "tokenUsage":{"total":{"totalTokens":total}}})

    def raw(self, turn, rid, amount):
        self.delivery.observe("rawResponse/completed", {"threadId":"root", "turnId":turn, "responseId":rid,
                              "usage":None if amount is None else {"totalTokens":amount}})

    def start(self):
        turn = self.delivery.deliver("root", "compute", event_id="start")["turn_id"]
        self.delivery.observe("turn/started", {"threadId":"root", "turn":{"id":turn}})
        return turn

    def complete(self):
        self.peer.turns[-1]["status"] = "completed"
        self.delivery.observe("turn/completed", {"threadId":"root", "turn":self.peer.turns[-1]})

    def assert_pending(self, turn):
        with self.assertRaises(HoldActive) as wait:
            self.delivery.deliver("root", "continue", event_id="continue")
        self.assertEqual((wait.exception.details["reason"], wait.exception.details["turn_id"]),
                         ("native_usage_pending", turn))
        self.assertIsNone(self.delivery._get("continue"))
        self.assertEqual(len(self.peer.sent), 1)

    def test_late_wrong_turn_snapshot_and_duplicate_response(self):
        turn = self.start()
        self.raw(turn, "r1", 11)
        self.raw(turn, "r1", 11)
        self.raw(turn, "r2", 24)
        self.sample(turn, 11)
        self.complete()
        self.sample("unrelated", 35)
        self.assert_pending(turn)
        self.sample(turn, 35)
        self.sample(turn, 35)
        self.sample(turn, 99)  # a conflicting unsolicited update cannot revise a settled measurement
        state = budgets.state(self.db, self.budget)
        self.assertEqual(state["spent"], {"tokens":35, "api_calls":1})
        self.delivery.deliver("root", "continue", event_id="continue")
        self.assertEqual(self.delivery._get("continue")["prior_tokens"], 35)

    def test_null_provider_receipt_and_replayed_intermediate_remain_pending(self):
        turn = self.start()
        self.raw(turn, "r1", 35)
        self.sample(turn, 35)
        self.raw(turn, "r2", None)
        self.sample(turn, 35)
        self.complete()
        self.delivery = RootDelivery(self.peer)
        self.delivery.reconcile("root")
        self.sample(turn, 35)
        self.assert_pending(turn)
        self.assertEqual(budgets.state(self.db, self.budget)["spent"], {})

    def abnormal_history(self, turn, amounts, *, extra_count=False, terminal_event="turn_aborted"):
        events = [{"type":"session_meta", "payload":{"id":"root"}},
                  {"type":"event_msg", "payload":{"type":"task_started", "turn_id":turn, "root_turn_id":turn}}]
        total = 0
        for n, amount in enumerate(amounts):
            total += amount
            events.append({"type":"token_usage_record", "payload":{"thread_id":"root", "turn_id":turn,
                "session_id":"root", "root_turn_id":turn, "response_id":str(n), "usage":{"total_tokens":amount},
                "turn_token_usage":{"total_tokens":total}}})
            events.append({"type":"event_msg", "payload":{"type":"token_count",
                "info":{"total_token_usage":{"total_tokens":total}}}})
        if extra_count:
            events.append(events[-1])
        events.append({"type":"event_msg", "payload":{"type":terminal_event, "turn_id":turn}})
        path = self.directory / "official-rollout.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in events))
        return {"id":"root", "path":str(path)}

    def assert_unpaired_count_stays_held(self, status):
        turn = self.start()
        amounts = [10, 15, 22, 13, 19]
        thread = self.abnormal_history(turn, amounts, extra_count=True,
                                       terminal_event="turn_aborted" if status == "interrupted" else "task_complete")
        self.delivery.attached("root", thread)
        total = 0
        for n, amount in enumerate(amounts):
            self.raw(turn, str(n), amount)
            total += amount
            self.sample(turn, total)
        self.sample(turn, total)  # Rate limits arrived for a request whose final cost is unknown.
        terminal = self.peer.turns[-1]
        terminal["status"] = status
        self.delivery.observe("turn/completed", {"threadId":"root", "turn":terminal})
        self.assert_pending(turn)
        self.delivery = RootDelivery(self.peer)
        self.delivery.attached("root", {**thread, "turns":[{**terminal, "itemsView":"full"}]})
        self.sample(turn, total)
        self.assert_pending(turn)
        self.assertEqual(budgets.state(self.db, self.budget)["spent"], {})

    def test_interrupted_unpaired_count_holds_live_and_cold(self):
        self.assert_unpaired_count_stays_held("interrupted")

    def test_failed_unpaired_count_holds_live_and_cold(self):
        self.assert_unpaired_count_stays_held("failed")

    def test_verified_zero_usage_and_duplicate_notifications_charge_once_on_interruption(self):
        turn = self.start()
        self.delivery.attached("root", self.abnormal_history(turn, [0, 0]))
        for rid in ["0", "0", "1"]:
            self.raw(turn, rid, 0)
            self.sample(turn, 0)
        terminal = self.peer.turns[-1]
        terminal["status"] = "interrupted"
        for _ in range(2):
            self.delivery.observe("turn/completed", {"threadId":"root", "turn":terminal})
        state = budgets.state(self.db, self.budget)
        self.assertEqual((state["spent"]["api_calls"], state["spent"].get("tokens", 0)), (1, 0))
        self.delivery.deliver("root", "continue", event_id="next")
        self.assertEqual(self.delivery._get("next")["prior_tokens"], 0)

    def test_settled_later_row_does_not_hide_older_unknown_hold(self):
        turn = self.start()
        self.complete()
        # Old releases could persist a second paid turn while the first was HELD.
        newer = budgets.reserve(self.db, budget_ref=self.budget, subject_kind="native_turn",
                                subject_id="old-successor", amounts={"tokens":50, "api_calls":1}, actor="probe")
        self.db.execute("INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,"
                        "human,interrupt,state,method,turn_id,reservation_ref,token_budget,prior_tokens) "
                        "VALUES(?,'old-successor','root','older','hash',0,0,'acknowledged','turn/start','later',?,50,NULL)",
                        (self.run, newer["reservation_ref"]))
        self.db.execute("INSERT INTO p0_root_turn_usage(thread_id,turn_id,terminal_status,total_tokens) "
                        "VALUES('root','later','completed',30)")
        budgets.settle(self.db, newer["reservation_ref"], actual={"tokens":30,"api_calls":1}, actor="probe")
        self.assert_pending(turn)
        self.assertIsNone(self.delivery._get("old-successor")["prior_tokens"])

    def test_unrelated_hold_does_not_mask_exhausted_api_calls(self):
        budgets.spend(self.db, budget_ref=self.budget, subject_kind="test", subject_id="calls",
                       amounts={"api_calls":2}, actor="probe")
        budgets.reserve(self.db, budget_ref=self.budget, subject_kind="test", subject_id="reserved",
                        amounts={"tokens":300}, actor="probe")
        with self.assertRaises(BudgetExceeded) as caught:
            self.delivery.deliver("root", "compute", event_id="denied")
        details = caught.exception.details
        self.assertEqual(details["reason"], "budget_exhausted")
        self.assertEqual({line["key"] for line in details["shortfalls"]}, {"tokens","api_calls"})
        self.assertFalse(self.peer.sent)

    def test_queued_message_to_idle_child_does_not_reserve_a_child_turn(self):
        turn = self.start()
        self.db.execute("INSERT INTO p0_native_child_meters"
                        "(child_id,run_id,root_id,parent_id,owner_turn,metadata_verified,awaiting_turn) "
                        "VALUES('child',?,'root','root','old-turn',1,0)", (self.run,))
        fact = {"threadId":"root", "turnId":turn}
        self.delivery.observe("rawResponseItem/completed", {**fact, "item":{
            "type":"function_call","namespace":"collaboration","name":"send_message","call_id":"send-1"}})
        self.delivery.observe_descendant("item/completed", {**fact, "item":{
            "type":"subAgentActivity","id":"send-1","kind":"interacted","agentThreadId":"child"}})
        self.raw(turn, "paid", 20)
        self.sample(turn, 20)
        self.complete()
        self.assertEqual(budgets.state(self.db, self.budget)["spent"], {"tokens":20,"api_calls":1})

    def test_run_hold_does_not_mask_exhausted_project_phase(self):
        budgets.activate_project_budget(self.db, "accounting", {"tokens":300,"api_calls":1,"job_submissions":1,
                                        "cpu_seconds":1,"gpu_minutes":0,"gpu_jobs":0,"worker_tasks":1}, phase_id="review")
        budgets.spend(self.db, budget_ref=self.budget, subject_kind="test", subject_id="phase-charge",
                      amounts={"api_calls":1}, actor="probe")
        budgets.reserve(self.db, budget_ref=self.budget, subject_kind="test", subject_id="run-hold",
                        amounts={"tokens":300}, actor="probe")
        with self.assertRaises(BudgetExceeded) as caught:
            self.delivery.deliver("root", "compute", event_id="phase-denial")
        details = caught.exception.details
        self.assertEqual((details["reason"],details["key"]), ("budget_exhausted","api_calls"))
        phase_line, = [line for line in details["shortfalls"] if line["key"] == "api_calls"]
        self.assertEqual((phase_line["phase_id"],phase_line["spent"]), ("review",1))
        self.assertIsNone(self.delivery._get("phase-denial"))

    def test_wrong_causal_turn_cannot_release_the_spawn_reservation(self):
        turn = self.start()
        self.delivery.observe_descendant("item/started", {"threadId":"root","turnId":turn, "item":{
            "type":"subAgentActivity","kind":"started","id":"spawn-1","agentThreadId":"child"}})
        self.delivery.observe_descendant("turn/started", {"threadId":"child",
                                         "turn":{"id":"child-turn","rootTurnId":"elsewhere"}})
        self.delivery.observe_descendant("thread/tokenUsage/updated", {"threadId":"child","turnId":"child-turn",
                                         "tokenUsage":{"total":{"totalTokens":30}}})
        self.raw(turn, "response", 10)
        self.sample(turn, 10)
        self.complete()
        self.assertEqual(self.db.execute("SELECT count(*) FROM p0_native_child_usage").fetchone()[0], 0)
        self.assert_pending(turn)

    def test_two_child_turns_in_one_parent_turn_wait_for_the_second_receipt(self):
        turn = self.start()
        self.db.execute("INSERT INTO p0_native_child_meters"
                        "(child_id,run_id,root_id,parent_id,owner_turn,metadata_verified,awaiting_turn) "
                        "VALUES('child',?,'root','root',?,1,0)", (self.run, turn))
        self.db.execute("INSERT INTO p0_native_child_usage"
                        "(child_id,turn_id,owner_turn,prior_tokens,total_tokens,terminal,measured_tokens,children_known)"
                        " VALUES('child','first-child-turn',?,0,11,1,11,1)", (turn,))
        follow = {"threadId":"root", "turnId":turn, "item":{
            "type":"subAgentActivity","kind":"interacted","id":"followup-2","agentThreadId":"child"}}
        self.delivery.observe_descendant("item/started", follow)
        self.raw(turn, "parent-response", 10)
        self.sample(turn, 10)
        self.complete()
        self.assert_pending(turn)
        fact = {"threadId":"child","turnId":"second-child-turn"}
        self.delivery.observe_descendant("turn/started", {"threadId":"child",
                                         "turn":{"id":fact["turnId"],"rootTurnId":turn}})
        self.delivery.observe_descendant("rawResponse/completed", {**fact,"responseId":"child-2",
                                         "usage":{"totalTokens":7}})
        self.delivery.observe_descendant("thread/tokenUsage/updated", {**fact, "tokenUsage":{"total":{"totalTokens":18}}})
        self.delivery.observe_descendant("turn/completed", {**fact, "turn":{"id":fact["turnId"],"status":"completed"}})
        self.delivery.observe_descendant("item/completed", follow)
        self.assertEqual(budgets.state(self.db, self.budget)["spent"], {"tokens":28,"api_calls":1})
        self.assertEqual(self.db.execute("SELECT awaiting_turn FROM p0_native_child_meters").fetchone()[0], 0)

    def test_unloaded_child_requires_its_original_session_and_complete_response_records(self):
        parent_turn = self.start()
        self.delivery.observe_descendant("item/completed", {"threadId":"root","turnId":parent_turn,
            "item":{"type":"subAgentActivity","kind":"started","agentThreadId":"child"}})
        self.db.execute("UPDATE p0_native_child_meters SET awaiting_turn=0 WHERE child_id='child'")
        self.db.execute("INSERT INTO p0_native_child_usage"
                        "(child_id,turn_id,owner_turn,prior_tokens,total_tokens,terminal,measured_tokens,children_known)"
                        " VALUES('child','child-turn',?,0,915,1,732,1)", (parent_turn,))
        self.raw(parent_turn, "parent-response", 35)
        self.sample(parent_turn, 35)
        self.complete()
        path = self.directory / "official-child.jsonl"
        spawn = {"thread_spawn":{"parent_thread_id":"root"}}
        session = {"id":"child","cli_version":"0.169.0","parent_thread_id":"root","session_id":"root",
                   "source":{"subagent":spawn}}
        events = [{"type":"event_msg","payload":{"type":"task_started","turn_id":"child-turn",
                                                 "root_turn_id":parent_turn}}]
        for response, amount, total in (("early",732,732), ("late",183,915)):
            events.extend([
                {"type":"token_usage_record","payload":{"thread_id":"child","turn_id":"child-turn",
                 "session_id":"root","root_turn_id":parent_turn,"response_id":response,
                 "usage":{"total_tokens":amount},"turn_token_usage":{"total_tokens":total}}},
                {"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"total_tokens":total}}}}
            ])
        events.append({"type":"event_msg","payload":{"type":"task_complete","turn_id":"child-turn"}})
        def write(header):
            path.write_text("".join(json.dumps(row)+"\n" for row in [{"type":"session_meta","payload":header}, *events]))
        thread = {"id":"child","sessionId":"child","cliVersion":"0.169.0","status":{"type":"notLoaded"},
                  "source":{"subAgent":spawn},"parentThreadId":"root","forkedFromId":None,"path":str(path),
                  "turns":[{"id":"child-turn","rootTurnId":parent_turn,"status":"completed","itemsView":"full","items":[]}]}
        for wire, header in (
                ({"sessionId":"foreign"}, {}), ({"status":{"type":"active"}}, {}), ({"forkedFromId":"foreign"}, {}),
                ({"source":{"subAgent":{"thread_spawn":{"parent_thread_id":"foreign"}}}}, {}),
                ({}, {"session_id":"foreign"}), ({}, {"id":"foreign"}), ({}, {"parent_thread_id":"foreign"}),
                ({}, {"source":{"subagent":{"thread_spawn":{"parent_thread_id":"foreign"}}}})):
            with self.subTest(wire=wire, header=header):
                write({**session, **header})
                self.delivery.verify_descendants(requested={"child":{**thread, **wire}})
                self.assertEqual(self.db.execute("SELECT metadata_verified FROM p0_native_child_meters").fetchone()[0], 0)
                self.assert_pending(parent_turn)
        write(session)
        self.delivery.verify_descendants(requested={"child":thread})
        self.delivery.verify_descendants(requested={"child":thread})
        self.assertEqual(budgets.state(self.db, self.budget)["spent"], {"tokens":950,"api_calls":1})
        self.assertEqual(self.db.execute("SELECT measured_tokens FROM p0_native_child_usage").fetchone()[0], 915)


class TaskRetryMeterTests(unittest.TestCase):
    def setUp(self):
        fixture = self.fixture = test_p0_roots.DurableRoots()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(clock.reset)
        self.db, self.run, self.task = fixture.conn, fixture.run, fixture.task
        self.budget = budgets.create_root(self.db, run_id=self.run, profile_name="p0", actor="test",
                                          limits={"tokens":1_000_000, "api_calls":9})
        self.db.execute("UPDATE runs SET budget_ref=? WHERE run_id=?", (self.budget, self.run))
        self.peers = {}
        self.starts = {}
        self.attach(fixture.roots)

    def attach(self, roots):
        self.roots = roots
        roots.token_budget = 65_536
        roots._request = self.exchange
        roots._goal_for_budget = lambda thread, grant: None

    def exchange(self, method, params):
        if method == "thread/start":
            result = self.fixture.fake(method, params)
            tid = result["thread"]["id"]
            self.starts[tid] = result
            self.peers[tid] = Peer(self.db, self.run)
            return result
        tid = params["threadId"]
        peer = self.peers[tid]
        if method == "thread/resume":
            return {**self.starts[tid], "thread":{**self.starts[tid]["thread"], "turns":peer.turns}}
        result = peer._request(method, params)
        if method == "thread/read":
            result["thread"]["id"] = tid
        return result

    def start_retry(self):
        first = self.roots.dispatch(self.task)
        with store.transaction(self.db):
            store.release_lease(self.db, first["lease_id"])
            store.transition(self.db, machine="task", subject_id=self.task, to_state="READY", actor="backend",
                             reason="retry selected while the original turn is still streaming",
                             extra_updates={"owner":None, "lease_id":None})
        with self.assertRaises(HoldActive) as caught:
            self.roots.dispatch(self.task)
        self.assertEqual((caught.exception.details["reason"], caught.exception.details["thread_id"]),
                         ("native_usage_pending", first["thread_id"]))
        successor = self.roots._claim(self.task)
        self.assertEqual(successor["attempt"], first["attempt"] + 1)
        self.assertNotEqual(successor["thread_id"], first["thread_id"])
        self.assertFalse(self.peers[successor["thread_id"]].sent)
        return first, successor

    def complete(self, binding, amount, *, with_total=True):
        tid, turn = binding["thread_id"], binding["turn_id"]
        deliver = self.roots.delivery.observe
        deliver("turn/started", {"threadId":tid, "turn":{"id":turn}})
        deliver("rawResponse/completed", {"threadId":tid, "turnId":turn, "responseId":tid + "-r1",
                                          "usage":{"totalTokens":amount}})
        self.peers[tid].turns[0]["status"] = "completed"
        deliver("turn/completed", {"threadId":tid, "turn":self.peers[tid].turns[0]})
        if with_total:
            self.total(binding, amount)

    def total(self, binding, amount):
        self.roots.delivery.observe("thread/tokenUsage/updated", {"threadId":binding["thread_id"],
            "turnId":binding["turn_id"], "tokenUsage":{"total":{"totalTokens":amount}}})

    def test_reviewer_retry_waits_for_actual_charge_then_observes_exhaustion(self):
        # Historical OAuth receipts, replayed into a fresh store without a model call.
        budgets.spend(self.db, budget_ref=self.budget, subject_kind="test", subject_id="three-original-turns",
                      amounts={"tokens":511_679, "api_calls":3}, actor="test")
        first, successor = self.start_retry()
        self.assertEqual(budgets.state(self.db, self.budget)["reserved"], {"tokens":65_536, "api_calls":1})
        self.complete(first, 533_421)
        self.total(first, 533_421)
        with self.assertRaises(BudgetExceeded) as caught:
            self.roots.dispatch(self.task)
        self.assertEqual((caught.exception.details["reason"], caught.exception.details["key"]),
                         ("budget_exhausted", "tokens"))
        self.assertEqual(budgets.state(self.db, self.budget)["spent"], {"tokens":1_045_100, "api_calls":4})
        self.assertFalse(self.peers[successor["thread_id"]].sent)
        self.assertIsNone(self.roots.delivery._get("start:" + successor["root_key"]))
        wait, = waits.pending(self.db, self.run)
        self.assertEqual((wait["reason"], wait["wait_key"]), ("budget", "start:" + successor["root_key"]))
        self.assertFalse(self.db.execute("SELECT 1 FROM events WHERE kind='task.wait_handoff'").fetchone())

    def test_other_task_runs_and_expired_successor_resumes_once_after_cold_metering(self):
        first, successor = self.start_retry()
        other_task = records.create_task(self.db, run_id=self.run, actor="test", kind="research_round",
                                         role="evidence_reviewer", required_artifacts=["science_assessment"])
        store.transition(self.db, machine="task", subject_id=other_task, to_state="READY", actor="backend",
                         reason="independent review")
        other = self.roots.dispatch(other_task)
        self.complete(other, 23)
        self.complete(first, 70, with_total=False)
        self.assertEqual(budgets.state(self.db, self.budget)["spent"], {"tokens":23, "api_calls":1})
        self.db.close()
        self.db = store.open_store(Path(self.fixture.tmp.name) / "test.db")
        self.addCleanup(self.db.close)
        self.fixture.conn = self.db
        for peer in self.peers.values():
            peer.conn = self.db
        self.attach(Roots(self.db, run_id=self.run, cwd=self.fixture.tmp.name))
        clock.advance(default_lease_seconds() + 60)
        self.roots.reconcile_meter(first["thread_id"])
        with self.assertRaises(HoldActive) as caught:
            self.roots.dispatch(self.task)
        self.assertEqual(caught.exception.details["turn_id"], first["turn_id"])
        wait, = waits.pending(self.db, self.run)
        self.assertEqual((wait["reason"], wait["thread_id"], wait["lease_id"]),
                         ("metering", successor["thread_id"], successor["lease_id"]))
        with self.assertRaises(LeaseExpired):
            self.roots.delivery.deliver(first["thread_id"], "revoked", event_id="old-retry")
        self.total(first, 70)
        resumed = self.roots.dispatch(self.task)
        self.assertEqual(resumed["lease_id"], successor["lease_id"])
        self.assertEqual(self.roots.dispatch(self.task)["turn_id"], resumed["turn_id"])
        self.assertEqual(len(self.peers[resumed["thread_id"]].sent), 1)
        self.assertEqual(waits.pending(self.db, self.run), [])
        self.complete(resumed, 50)
        self.assertEqual(budgets.state(self.db, self.budget)["spent"], {"tokens":143, "api_calls":3})


if __name__ == "__main__":
    unittest.main()
