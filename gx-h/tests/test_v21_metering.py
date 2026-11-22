"""GX token accounting over real SQLite, with controllable native protocol notifications."""
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from gx_core import budgets
from gx_core.domain import clock
from gx_core.errors import BudgetExceeded, HoldActive
from gx_core.p0 import meter_limits, metering, schema
from gx_core.p0.root_delivery import RootDelivery
from gx_core.persistence import records, store
from tests import test_p0_meter_recovery as fixtures


class ThreadAccounts(unittest.TestCase):
    def setUp(self):
        self.fx = f = fixtures.TurnMeterTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.addCleanup(clock.reset)
        self.db,self.run = f.db,f.run

    def status(self):
        return metering.inspect(self.db,self.run)

    def estimate(self, *, partial=True):
        f = self.fx
        turn = f.start()
        if partial:
            f.raw(turn,"ok",35)
            f.sample(turn,35)
        f.raw(turn,"missing",None)
        f.complete()
        clock.advance(31)
        metering.maintain(self.db,self.run)
        return turn

    def test_cumulative_includes_cached_input_once_and_regressions_survive_reopen(self):
        f = self.fx
        turn = f.start()
        for _ in range(2):
            f.delivery.observe("rawResponse/completed",{"threadId":"root","turnId":turn,"responseId":"r",
                "usage":{"totalTokens":35,"inputTokens":30,"cachedInputTokens":25,"outputTokens":5}})
            f.sample(turn,35)
        with patch.object(Path,"open",side_effect=AssertionError("meter read a rollout on a normal live turn")):
            f.complete()
        f.sample(turn,31)
        with sqlite3.connect(self.fx.directory/"ledger.sqlite") as check:
            self.assertEqual(check.execute("SELECT observed_total,reconciled_total,anomaly_total FROM p0_meter_threads"
                                           ).fetchone(),(35,35,31))
        self.assertEqual(self.status()["turns"][0]["confidence"],"PROVIDER_CONFIRMED")
        self.assertEqual(budgets.state(self.db,f.budget)["spent"],{"api_calls":1,"tokens":35})
        self.assertIsNone(self.status()["provider_money_cost"])

    def test_late_cumulative_adjusts_both_directions_once_across_two_connections(self):
        f = self.fx
        turn = self.estimate()
        ticket, = self.status()["turns"]
        self.assertEqual((ticket["confidence"],ticket["settled_tokens"],ticket["observed_lower_bound"]),
                         ("ESTIMATED",100,35))
        f.sample(turn,35)
        self.assertEqual(self.status()["adjustments"],[])
        path = f.directory/"ledger.sqlite"
        def replay(total):
            db = store.open_store(path)
            try:
                peer = fixtures.Peer(db,self.run)
                RootDelivery(peer).observe("thread/tokenUsage/updated",
                    {"threadId":"root","turnId":turn,"tokenUsage":{"total":{"totalTokens":total}}})
            finally:
                db.close()
        for total in (70,130):
            with ThreadPoolExecutor(2) as pool:
                list(pool.map(replay,[total,total]))
        self.assertEqual([x["delta"] for x in self.status()["adjustments"]],[-30,60])
        self.assertEqual(budgets.state(self.db,f.budget)["spent"],{"api_calls":1,"tokens":130})
        receipt = self.db.execute("SELECT payload_json FROM events WHERE kind='budget.settled'").fetchone()[0]
        self.assertEqual((json.loads(receipt)["confidence"],json.loads(receipt)["actual"]["tokens"]),("ESTIMATED",100))

    def test_unknown_turn_can_continue_without_charging_old_delta_to_next_turn(self):
        f = self.fx
        self.estimate(partial=False)
        self.assertIsNone(self.status()["threads"][0]["observed_total"])
        f.delivery = RootDelivery(f.peer)
        next_turn = f.delivery.deliver("root","continue",event_id="next")["turn_id"]
        f.delivery.observe("turn/started",{"threadId":"root","turn":{"id":next_turn}})
        f.raw(next_turn,"new",20)
        f.sample(next_turn,90)
        f.complete()
        first,second = self.status()["turns"]
        self.assertEqual((first["attribution"],second["settled_tokens"],second["measured_tokens"]),
                         ("UNATTRIBUTED_THREAD_USAGE",20,20))
        self.assertEqual([e["delta"] for e in self.status()["adjustments"]],[-30])
        self.assertEqual(budgets.state(self.db,f.budget)["spent"],{"tokens":90,"api_calls":2})

    def test_cold_public_cumulative_checkpoint_reconciles_without_raw_or_rollout(self):
        f = self.fx
        self.estimate()
        f.delivery = RootDelivery(f.peer)
        tid = f.delivery.deliver("root","continue",event_id="second")["turn_id"]
        f.delivery.observe("turn/started",{"threadId":"root","turn":{"id":tid}})
        f.sample(tid,70)
        f.complete()
        self.assertEqual([t["confidence"] for t in self.status()["turns"]],["PROVIDER_RECONCILED"]*2)
        self.assertTrue(all(t["measured_tokens"] is None for t in self.status()["turns"]))
        self.assertEqual([a["delta"] for a in self.status()["adjustments"]],[-65])
        self.assertEqual(budgets.state(self.db,f.budget)["spent"],{"tokens":70,"api_calls":2})

    def test_extra_child_usage_after_estimation_is_counted_once_under_the_parent_call(self):
        f = self.fx
        parent = f.start()
        f.delivery.observe_descendant("item/completed",{"threadId":"root","turnId":parent,"item":{
            "type":"subAgentActivity","kind":"started","agentThreadId":"child"}})
        self.db.execute("UPDATE p0_native_child_meters SET metadata_verified=1 WHERE child_id='child'")
        observe = f.delivery.observe_descendant
        observe("turn/started",{"threadId":"child","turn":{"id":"child-turn","rootTurnId":parent}})
        child = {"threadId":"child","turnId":"child-turn"}
        observe("thread/tokenUsage/updated",{**child,"tokenUsage":{"total":{"totalTokens":40}}})
        f.raw(parent,"parent",35)
        f.sample(parent,35)
        f.complete()
        clock.advance(31)
        metering.maintain(self.db,self.run)
        self.assertEqual(self.status()["turns"][0]["confidence"],"ESTIMATED")
        observe("rawResponse/completed",{**child,"responseId":"c","usage":{"totalTokens":121}})
        observe("thread/tokenUsage/updated",{**child,"tokenUsage":{"total":{"totalTokens":121}}})
        for _ in range(2):
            observe("turn/completed",{**child,"turn":{"id":"child-turn","status":"completed"}})
        self.assertEqual(budgets.state(self.db,f.budget)["spent"],{"api_calls":1,"tokens":156})
        self.assertEqual([a["delta"] for a in self.status()["adjustments"]],[56])
        self.assertEqual(sorted(t["observed_total"] for t in self.status()["threads"]),[35,121])

    def test_grace_never_resolves_unknown_dispatch_or_returns_missing_usage_as_zero(self):
        f = self.fx
        f.peer.drop = True
        with self.assertRaises(EOFError):
            f.delivery.deliver("root","send",event_id="ambiguous")
        clock.advance(86400)
        metering.maintain(self.db,self.run)
        ticket, = self.status()["turns"]
        self.assertEqual((ticket["state"],ticket["confidence"],ticket["settled_tokens"]),("sending","RESERVED",None))
        self.assertEqual(budgets.state(self.db,f.budget)["reserved"],{"api_calls":1,"tokens":100})

    def test_durable_positive_raw_receipts_set_an_estimate_floor_when_all_snapshots_are_missing(self):
        f = self.fx
        t = f.start()
        f.raw(t,"expensive",145)
        f.raw(t,"expensive",145)
        f.raw(t,"unknown",None)
        f.complete()
        f.delivery = RootDelivery(f.peer)
        clock.advance(31)
        f.delivery.maintain()
        ticket, = self.status()["turns"]
        self.assertEqual((ticket["confidence"],ticket["observed_lower_bound"],ticket["settled_tokens"]),
                         ("ESTIMATED",145,145))
        self.assertIsNone(ticket["total_tokens"])

    def test_existing_v2_charges_seed_the_cumulative_checkpoint_without_rebilling(self):
        f = self.fx
        t = f.start()
        f.raw(t,"r",22)
        f.sample(t,22)
        f.complete()
        self.db.execute("DROP VIEW p0_meter_native_turns")
        for table in ("p0_meter_adjustments","p0_meter_turns","p0_meter_threads","p0_meter_interrupts","p0_meter_responses",
                      "p0_meter_legacy_runs"):
            self.db.execute("DROP TABLE "+table)
        self.db.executemany("DELETE FROM schema_meta WHERE key=?", (("p0.migration."+k,) for k in (
            "z_v21_metering.sql","z_v21_response_receipts.sql","zz_v21_historical_meter.sql","y_v21_legacy_meter.sql")))
        schema.install(self.db)
        schema.install(self.db)
        self.assertEqual(self.status()["threads"][0]["reconciled_total"],22)
        self.assertTrue(metering.rollout_diagnostics(self.db,self.run))
        self.assertEqual(budgets.state(self.db,f.budget)["spent"],{"api_calls":1,"tokens":22})


class SharedBudgetTests(unittest.TestCase):
    def setUp(self):
        f = self.fx = fixtures.TaskRetryMeterTests()
        f.setUp()
        self.addCleanup(f.doCleanups)

    def independent(self, *, priority=None):
        f = self.fx
        task = records.create_task(f.db,run_id=f.run,actor="test",kind="research_round",role="evidence_reviewer",
                                   task_input={"priority":priority} if priority else {})
        store.transition(f.db,machine="task",subject_id=task,to_state="READY",actor="backend",reason="independent")
        return task

    def test_soft_threshold_stops_only_low_priority_and_job_receipt_replay_remains_local(self):
        f = self.fx
        f.db.execute("UPDATE runs SET inputs_json=? WHERE run_id=?",(json.dumps({"inputs":{
            "p0_spec":{"metering":{"soft_tokens":10}}}}),f.run))
        budgets.spend(f.db,budget_ref=f.budget,subject_kind="fixture",subject_id="early",amounts={"tokens":10},actor="test")
        low = self.independent(priority="low")
        with self.assertRaises(HoldActive) as soft:
            f.roots.dispatch(low)
        self.assertEqual(soft.exception.details["reason"],"soft_budget")
        with self.assertRaises(HoldActive):
            meter_limits.check_soft(f.db,f.run,low)
        self.assertTrue(f.roots.dispatch(f.task)["turn_id"])

    def test_multi_root_hard_crossing_queues_before_rpc_and_reconciles_exact_terminals(self):
        f = self.fx
        budgets.spend(f.db,budget_ref=f.budget,subject_kind="fixture",subject_id="earlier",
                      amounts={"tokens":850000},actor="test")
        roots = [f.roots.dispatch(f.task),f.roots.dispatch(self.independent())]
        for r,amount in zip(roots,[100000,80000]):
            f.roots.active_turns[r["thread_id"]]=r["turn_id"]
            f.total(r,amount)
        s = metering.inspect(f.db,f.run)
        self.assertEqual((s["risk"]["active_root_count"],s["risk"]["observed_plus_settled"]),(2,1030000))
        self.assertEqual(len(s["interrupts"]),2)
        self.assertTrue(all(row["sent_at"] is None for row in s["interrupts"]))
        with self.assertRaises(BudgetExceeded):
            f.roots.dispatch(self.independent())
        for _ in range(2):
            f.roots.delivery.flush_interrupts()
        self.assertEqual([sum(m=="turn/interrupt" for m,_ in f.peers[r["thread_id"]].sent) for r in roots],[1,1])
        for r in roots:
            f.roots.delivery.observe("turn/completed",{"threadId":r["thread_id"],
                "turn":{"id":r["turn_id"],"status":"interrupted"}})
        self.assertEqual([r["terminal_status"] for r in metering.inspect(f.db,f.run)["interrupts"]],["interrupted"]*2)
