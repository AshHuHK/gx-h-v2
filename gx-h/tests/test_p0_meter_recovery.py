"""Paid-turn admission and exact provider receipts across faults and old ledgers."""
import json
import tempfile
import unittest
from pathlib import Path

from gx_core import budgets
from gx_core.errors import BudgetExceeded, HoldActive
from gx_core.p0 import schema
from gx_core.p0.root_delivery import RootDelivery
from gx_core.persistence import records, store
from test_p0_root_delivery import Peer


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


if __name__ == "__main__":
    unittest.main()
