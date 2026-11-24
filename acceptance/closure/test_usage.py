"""P2A U01–U08 against real GX SQLite, RootDelivery notifications and the offline Driver."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h"), str(ROOT / "gx-h/tests")]
from acceptance.closure.profile import test_environment
from gx_core import budgets
from gx_core.domain import clock
from gx_core.p0 import driver, metering
from gx_core.p0.root_delivery import RootDelivery
from gx_core.persistence import store
from tests import test_p0_meter_recovery as fixtures


class UsageClosure(unittest.TestCase):
    artifact_dir = None

    def setUp(self):
        f = self.f = fixtures.TurnMeterTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.addCleanup(clock.reset)
        self.db, self.run = f.db, f.run
        self.configure(30)
        self.history = {}
        self.addCleanup(self.export)

    def configure(self, grace):
        inputs = {"inputs":{"p0":True, "p0_spec":{"metering":{"grace_seconds":grace}}}}
        self.db.execute("UPDATE runs SET status='PAUSED',inputs_json=? WHERE run_id=?", (json.dumps(inputs), self.run))

    def inspect(self, label):
        status = metering.inspect(self.db, self.run)
        self.history[label] = status
        return status

    def events(self):
        return [dict(row) for row in self.db.execute(
            "SELECT event_id,ts,kind,subject_id,payload_json FROM events WHERE run_id=? AND "
            "(kind LIKE 'budget.%' OR kind LIKE 'usage.%') ORDER BY event_id", (self.run,))]

    def export(self):
        if self.artifact_dir is None:
            return
        dest = Path(self.artifact_dir)
        dest.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(dest / "ledger.db")) as backup:
            self.db.backup(backup)
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        (dest / "usage.json").write_text(json.dumps(self.history, indent=2) + "\n")
        events = [{**row, "payload_json":json.loads(row["payload_json"])} for row in self.events()]
        (dest / "events.json").write_text(json.dumps(events, indent=2) + "\n")

    def drive(self, seconds=.14):
        with patch.dict(os.environ, {driver.OFFLINE:"1"}), closing(driver.Driver(self.db, self.f.directory, self.run)) as owner:
            owner.drive(seconds)

    def interrupted(self):
        turn = self.f.start()
        self.f.raw(turn, "ok", 35)
        self.f.peer.turns[-1]["status"] = "interrupted"
        self.f.delivery.observe("turn/completed", {"threadId":"root", "turn":self.f.peer.turns[-1]})
        return turn

    def start_child(self, parent, child, child_turn):
        deliver = self.f.delivery
        deliver.observe_descendant("item/completed", {"threadId":"root", "turnId":parent, "item":{
            "type":"subAgentActivity", "kind":"started", "agentThreadId":child}})
        deliver.verify_descendants({child:{"id":child, "sessionId":"root", "parentThreadId":"root",
                                           "source":{"subAgent":{"thread_spawn":{"parent_thread_id":"root"}}}}})
        deliver.observe_descendant("turn/started", {"threadId":child, "turn":{"id":child_turn, "rootTurnId":parent}})
        return {"threadId":child, "turnId":child_turn}

    def funding_fixture(self):
        self.f.budget = budgets.create_root(self.db, run_id=self.run, profile_name="p0", actor="review",
                                            limits={"tokens":1500, "api_calls":12})
        self.db.execute("UPDATE runs SET budget_ref=? WHERE run_id=?", (self.f.budget, self.run))

    def funded_turn(self, fund=None):
        route = patch("gx_core.p0.team_admission.team_funding", return_value=fund) if fund else nullcontext()
        with route:
            event = f"paid-{len(self.f.peer.turns) + 1}"
            turn = self.f.delivery.deliver("root", event, event_id=event)["turn_id"]
        self.f.delivery.observe("turn/started", {"threadId":"root", "turn":{"id":turn}})
        return turn

    def leaf(self, name):
        return budgets.open_child(self.db, parent_ref=self.f.budget, subject_kind="team", subject_id=name,
                                  limits={"tokens":400, "api_calls":4}, actor="review")["budget_ref"]

    def common_case(self, separate):
        self.funding_fixture()
        a = self.leaf("A")
        b = self.leaf("B") if separate else a
        first = self.funded_turn(a)
        self.f.raw(first, "late", None)
        self.f.complete()
        clock.advance(31)
        self.drive()
        second = self.funded_turn(b)
        self.f.sample(second, 145)
        self.f.complete()
        before = self.inspect("145_pre_grace")
        self.assertEqual(before["turns"][1]["budget_status"], "HELD")
        self.assertEqual(before["adjustments"], [])
        self.f.sample(first, 120)
        self.f.raw(first, "late", 120)
        after = self.inspect("120_attributed_to_first")
        self.assertEqual([r["budget_status"] for r in after["turns"]], ["SETTLED","HELD"])
        self.assertEqual(after["risk"]["settled_tokens"], 120)
        self.assertEqual(after["risk"]["observed_plus_settled"], 145)
        self.assertEqual(after["cost_report"]["UNATTRIBUTED_DELTA"]["tokens"], 25)
        self.assertEqual(budgets.state(self.db, a)["spent"], {"api_calls":1,"tokens":120})
        if separate:
            self.assertEqual(budgets.state(self.db, b)["spent"], {})

    def test_B01_different_funding_owners(self):
        self.common_case(True)

    def test_B02_same_fund_waits_for_grace(self):
        self.common_case(False)

    def test_B03_unreserved_turn_keeps_its_own_receipts(self):
        self.funding_fixture()
        first = self.funded_turn()
        self.f.raw(first,"first",20)
        self.f.sample(first,20)
        self.f.complete()
        host = "unreserved-goal"
        self.f.delivery.observe("turn/started", {"threadId":"root", "turn":{"id":host}})
        self.f.raw(host,"host",200)
        self.f.sample(host,220)
        self.f.delivery.observe("turn/completed", {"threadId":"root","turn":{"id":host,"status":"completed"}})
        second = self.funded_turn()
        self.f.raw(second,"missing",None)
        self.f.sample(second,225)
        self.f.complete()
        clock.advance(31)
        self.drive()
        after = self.inspect("unreserved_charge_is_independent")
        ticket = after["turns"][1]
        self.assertEqual((ticket["prior_tokens"], ticket["observed_lower_bound"], ticket["settled_tokens"]), (220,0,100))
        self.assertEqual(after["cost_report"]["PROVIDER_CONFIRMED"]["tokens"], 220)
        self.assertEqual(after["cost_report"]["UNATTRIBUTED_DELTA"]["tokens"], 5)
        self.assertEqual(after["cost_report"]["ESTIMATED"]["tokens"], 95)
        self.assertEqual(after["risk"]["unfunded_closed_thread_tokens"], 200)
        self.assertEqual(after["risk"]["observed_plus_settled"], 320)

    def test_B04_closed_funding_line_retains_residual(self):
        self.funding_fixture()
        branch = self.leaf("closed")
        first = self.funded_turn(branch)
        self.f.raw(first,"actual",20)
        self.f.sample(first,20)
        self.f.complete()
        self.f.sample("unreserved-goal",70)
        before = self.inspect("open_funding")["risk"]
        budgets.release(self.db, budgets.FUND_PREFIX+branch, actor="review", reason="completed")
        after = self.inspect("closed_funding")["risk"]
        for risk in (before, after):
            self.assertEqual((risk["settled_tokens"],risk["unfunded_closed_thread_tokens"],risk["observed_plus_settled"]),
                             (20,50,70))

    def test_B05_unknown_historical_cost_preserves_spent(self):
        self.funding_fixture()
        first = self.funded_turn()
        ref = self.inspect("admitted")["turns"][0]["reservation_ref"]
        with store.transaction(self.db):
            self.db.execute("UPDATE budgets SET spent_json=?,reserved_json='{}' WHERE budget_ref=?",
                            (json.dumps({"tokens":100,"api_calls":1}),self.f.budget))
            self.db.execute("UPDATE budget_reservations SET status='SETTLED',settled_at=? WHERE reservation_ref=?",
                            (clock.now_iso(),ref))
            self.db.execute("UPDATE p0_meter_turns SET terminal_at=?,confidence='HISTORICAL_SETTLED',"
                            "attribution='HISTORICAL_TURN',settled_tokens=NULL WHERE reservation_ref=?", (clock.now_iso(),ref))
            self.db.execute("INSERT INTO p0_root_turn_usage(thread_id,turn_id,terminal_status,total_tokens,children_known) "
                            "VALUES('root',?,'completed',100,1)", (first,))
            metering.observe(self.db,self.run,"root","root",first,100)
        self.f.peer.turns[-1]["status"] = "completed"
        self.assertEqual(self.inspect("original_bill_unknown")["risk"]["observed_plus_settled"],100)
        second = self.funded_turn()
        self.f.raw(second,"new",20)
        self.f.sample(second,120)
        self.f.complete()
        after = self.inspect("independent_new_charge")
        self.assertEqual(after["risk"]["observed_plus_settled"],120)
        self.assertEqual(after["adjustments"],[])
        self.assertEqual(after["cost_report"]["UNRESOLVED"]["reservation_refs"],[ref])
        self.assertIsNone(after["turns"][0]["settled_tokens"])
        goal = "new-host-turn"
        self.f.delivery.observe("turn/started", {"threadId":"root","turn":{"id":goal}})
        self.f.raw(goal,"new-host-50",50)
        self.f.sample(goal,170)
        self.f.delivery.observe("turn/completed", {"threadId":"root","turn":{"id":goal,"status":"completed"}})
        closed = self.inspect("legacy_with_proven_new_host_receipt")["risk"]
        self.assertEqual((closed["settled_tokens"], closed["observed_plus_settled"]),(120,170))
        third = self.funded_turn()
        self.f.sample(third,175)
        active = self.inspect("legacy_with_additional_open_turn")["risk"]
        self.assertEqual((active["settled_tokens"],active["observed_plus_settled"]),(120,175))

    def signed_active(self, final, total, scoped):
        self.funding_fixture()
        first = self.funded_turn()
        self.f.raw(first,"late",None)
        self.f.complete()
        clock.advance(31)
        self.drive()
        second = self.funded_turn()
        self.f.raw(first,"late",final)
        self.f.sample(first,final)
        self.f.sample(second if scoped else "unreserved-goal",total)
        after = self.inspect("active_turn_after_correction")
        self.assertEqual(after["risk"]["observed_plus_settled"], total)
        self.assertEqual(after["risk"]["settled_tokens"],final)
        self.assertEqual([a["delta"] for a in after["adjustments"]],[final-100])

    def test_B06_positive_adjustment_during_later_hold(self):
        self.signed_active(140,150,True)

    def test_B07_negative_adjustment_during_later_hold(self):
        self.signed_active(60,95,False)

    def test_B08_late_exact_within_already_funded_pool(self):
        self.funding_fixture()
        first = self.funded_turn()
        self.f.raw(first,"known",35)
        self.f.raw(first,"late",None)
        self.f.sample(first,35)
        self.f.complete()
        clock.advance(31)
        self.drive()
        second = self.funded_turn()
        self.f.sample(second,145)
        self.f.complete()
        self.assertEqual(self.inspect("pre_grace")["turns"][1]["budget_status"],"HELD")
        clock.advance(31)
        self.drive()
        self.assertEqual(self.inspect("pooled")["risk"]["settled_tokens"],145)
        self.f.sample(first,120)
        self.f.raw(first,"late",85)
        partial = self.inspect("first_exact")
        self.assertEqual(partial["cost_report"]["PROVIDER_CONFIRMED"]["tokens"],120)
        self.assertEqual(partial["cost_report"]["ESTIMATED"]["provisional_budget_tokens"],25)
        self.assertEqual(partial["risk"]["settled_tokens"],145)
        self.f.raw(second,"final",25)
        after = self.inspect("all_exact")
        self.assertEqual(after["cost_report"]["PROVIDER_CONFIRMED"]["tokens"],145)
        self.assertEqual(after["cost_report"]["ESTIMATED"]["provisional_budget_tokens"],0)
        self.assertEqual(after["risk"]["settled_tokens"],145)
        self.assertEqual([r["usage_class"] for r in after["turns"]],["PROVIDER_CONFIRMED"]*2)

    def test_B09_simultaneous_historical_holds_keep_distinct_receipts(self):
        self.funding_fixture()
        first = self.funded_turn()
        self.f.raw(first,"first-response",180)
        self.f.complete()
        newer = budgets.reserve(self.db,budget_ref=self.f.budget,subject_kind="native_turn",subject_id="older-format",
                                amounts={"tokens":100,"api_calls":1},actor="review")
        self.db.execute("INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,human,interrupt,"
                        "state,method,turn_id,reservation_ref,token_budget,prior_tokens) "
                        "VALUES(?,'older-format','root','older','hash',0,0,'acknowledged','turn/start','later',?,100,NULL)",
                        (self.run,newer["reservation_ref"]))
        self.f.delivery.observe("turn/started", {"threadId":"root","turn":{"id":"later"}})
        self.f.raw("later","second-response",160)
        self.f.raw("later","second-response",160)
        risk = self.inspect("two_simultaneous_historical_holds")["risk"]
        self.assertEqual((risk["reserved_tokens"],risk["observed_plus_settled"],risk["committed_tokens"]),(200,340,340))

    def signed_after_close(self, tokens):
        self.funding_fixture()
        a,b = self.leaf("old"),self.leaf("new")
        first = self.funded_turn(a)
        self.f.raw(first,"original",None)
        self.f.complete()
        clock.advance(31)
        self.drive()
        budgets.release(self.db,budgets.FUND_PREFIX+a,actor="review",reason="child completed")
        new = self.funded_turn(b)
        self.f.raw(first,"original",tokens)
        self.f.sample(first,tokens)
        self.f.sample(new,tokens+8)
        self.f.raw(new,"independent",8)
        self.f.complete()
        view = self.inspect("signed_correction_of_closed_budget")
        self.assertEqual(budgets.state(self.db,a)["spent"]["tokens"],tokens)
        self.assertEqual(budgets.state(self.db,b)["spent"]["tokens"],8)
        self.assertEqual(view["risk"]["observed_plus_settled"],tokens+8)
        self.assertEqual(view["risk"]["settled_tokens"],tokens+8)

    def test_B10_positive_receipt_after_funding_close(self):
        self.signed_after_close(140)

    def test_B11_negative_receipt_after_funding_close(self):
        self.signed_after_close(60)

    def test_U01_official_receipt_exactly_once(self):
        turn = self.f.start()
        for _ in range(2):
            self.f.raw(turn, "r1", 17)
            self.f.raw(turn, "r2", 21)
            self.f.sample(turn, 38)
            self.f.complete()
        view = self.inspect("duplicate_terminal")
        self.assertEqual(view["cost_report"]["PROVIDER_CONFIRMED"]["tokens"], 38)
        self.assertEqual(view["turns"][0]["usage_class"], "PROVIDER_CONFIRMED")
        self.assertEqual(budgets.state(self.db, self.f.budget)["spent"], {"tokens":38, "api_calls":1})
        self.assertEqual(sum(e["kind"] == "budget.settled" for e in self.events()), 1)

    def test_U02_natural_interruption_before_grace(self):
        self.interrupted()
        self.drive()
        view = self.inspect("before_grace")
        self.assertEqual(view["turns"][0]["budget_status"], "HELD")
        self.assertEqual(view["turns"][0]["usage_class"], "UNRESOLVED")
        self.assertIsNone(view["turns"][0]["settled_tokens"])
        self.assertEqual(view["cost_report"]["PROVIDER_CONFIRMED"]["tokens"], 35)
        self.assertGreater(metering.next_deadline_seconds(self.db, self.run), 0)
        self.assertEqual(sum(e["kind"] == "budget.settled" for e in self.events()), 0)

    def test_U03_driver_estimates_after_grace(self):
        self.interrupted()
        clock.advance(31)
        self.drive()
        self.drive()
        view = self.inspect("after_two_driver_passes")
        ticket, = view["turns"]
        self.assertEqual((ticket["usage_class"], ticket["settled_tokens"]), ("ESTIMATED", 100))
        self.assertEqual(view["cost_report"]["ESTIMATED"], {"tokens":65, "provisional_budget_tokens":100})
        self.assertIsNone(metering.next_deadline_seconds(self.db, self.run))
        event, = [json.loads(e["payload_json"]) for e in self.events() if e["kind"] == "budget.settled"]
        self.assertEqual((event["confidence"], event["actual"]["tokens"]), ("ESTIMATED", 100))

    def test_U04_late_receipt_links_original_estimate(self):
        turn = self.f.start()
        self.f.raw(turn, "known", 35)
        self.f.raw(turn, "later", None)
        self.f.sample(turn, 70)
        self.f.complete()
        clock.advance(31)
        self.drive()
        original, = [e for e in self.events() if e["kind"] == "budget.settled"]
        self.inspect("estimate")
        self.f.delivery = RootDelivery(self.f.peer)
        self.f.raw(turn, "later", 35)
        self.f.raw(turn, "later", 35)
        self.drive()
        view = self.inspect("late_confirmation")
        adj, = view["adjustments"]
        self.assertEqual((adj["basis"], adj["delta"], adj["confirmed_tokens"], adj["settlement_event_id"]),
                         ("TURN_RECEIPT", -30, 70, original["event_id"]))
        self.assertEqual(view["cost_report"]["ESTIMATED"]["provisional_budget_tokens"], 0)
        self.assertEqual(view["turns"][0]["usage_class"], "PROVIDER_CONFIRMED")
        self.assertEqual(budgets.state(self.db, self.f.budget)["spent"], {"tokens":70, "api_calls":1})
        self.assertEqual([e for e in self.events() if e["kind"] == "budget.settled"], [original])

        # A receipt equal to the estimate still produces a durable confirmation.
        next_turn = self.f.delivery.deliver("root", "continue", event_id="second")["turn_id"]
        self.f.delivery.observe("turn/started", {"threadId":"root", "turn":{"id":next_turn}})
        self.f.raw(next_turn, "equal", None)
        self.f.sample(next_turn, 170)
        self.f.complete()
        clock.advance(31)
        self.drive()
        self.f.raw(next_turn, "equal", 100)
        self.f.raw(next_turn, "equal", 100)
        self.assertEqual([a["delta"] for a in self.inspect("equal_late_receipt")["adjustments"]], [-30, 0])
        self.assertEqual(sum(e["kind"] == "usage.receipt_adjusted" for e in self.events()), 2)
        self.assertEqual(sum(e["kind"] == "budget.token_adjusted" for e in self.events()), 1)

    def test_U05_duplicate_out_of_order_and_conflicting_receipts(self):
        turn = self.f.start()
        self.f.raw(turn, "first", 30)
        self.f.raw(turn, "last", None)
        self.f.sample(turn, 80)
        self.f.complete()
        clock.advance(31)
        self.drive()
        self.f.raw(turn, "last", 100)  # Arrived before the matching 130 snapshot.
        self.f.sample("other-turn", 130)
        self.assertEqual(self.inspect("wrong_scope")["turns"][0]["usage_class"], "ESTIMATED")
        def replay(total):
            with closing(store.open_store(self.f.directory / "ledger.sqlite")) as conn:
                owner = RootDelivery(fixtures.Peer(conn, self.run))
                owner.observe("thread/tokenUsage/updated", {"threadId":"root", "turnId":turn,
                              "tokenUsage":{"total":{"totalTokens":total}}})
        with ThreadPoolExecutor(2) as workers:
            list(workers.map(replay, [130, 130]))
        self.f.sample(turn, 80)
        view = self.inspect("concurrent_replay")
        self.assertEqual([(a["basis"], a["delta"]) for a in view["adjustments"]], [("TURN_RECEIPT", 30)])
        self.assertEqual(view["turns"][0]["usage_class"], "PROVIDER_CONFIRMED")
        self.assertEqual(budgets.state(self.db, self.f.budget)["spent"], {"tokens":130, "api_calls":1})
        self.f.raw(turn, "first", 99)
        view = self.inspect("conflicting_replay")
        self.assertEqual(view["turns"][0]["usage_class"], "UNRESOLVED")
        self.assertEqual(view["cost_report"]["PROVIDER_CONFIRMED"]["tokens"], 100)
        self.assertEqual(len(view["adjustments"]), 1)
        self.assertEqual(sum(e["kind"] == "budget.settled" for e in self.events()), 1)

        next_turn = self.f.delivery.deliver("root", "continue", event_id="disputed")["turn_id"]
        self.f.delivery.observe("turn/started", {"threadId":"root", "turn":{"id":next_turn}})
        self.f.raw(next_turn, "last", 100)  # This receipt identity already belongs to the first Turn.
        self.f.sample(next_turn, 230)
        self.f.complete()
        clock.advance(31)
        self.drive()
        second = self.inspect("cross_turn_identity_conflict")["turns"][1]
        self.assertEqual((second["budget_status"], second["confidence"]), ("HELD", "UNRESOLVED"))
        self.assertEqual(sum(e["kind"] == "budget.settled" for e in self.events()), 1)

    def test_U06_parent_and_child_have_distinct_usage(self):
        turn = self.f.start()
        child = self.start_child(turn, "delegated", "child-turn")
        observe = self.f.delivery.observe_descendant
        for _ in range(2):
            observe("rawResponse/completed", {**child, "responseId":"same-id", "usage":{"totalTokens":11}})
            observe("thread/tokenUsage/updated", {**child, "tokenUsage":{"total":{"totalTokens":11}}})
        observe("turn/completed", {**child, "turn":{"id":"child-turn", "status":"completed"}})
        self.f.raw(turn, "same-id", 35)
        self.f.sample(turn, 35)
        self.f.complete()
        view = self.inspect("parent_and_child")
        accounts = {r["thread_id"]:r for r in view["cost_report"]["threads"]}
        self.assertEqual([accounts[key]["turn_attributed_total"] for key in ("root", "delegated")], [35, 11])
        self.assertEqual(view["cost_report"]["PROVIDER_CONFIRMED"]["tokens"], 46)
        self.assertEqual(view["cost_report"]["UNATTRIBUTED_DELTA"]["tokens"], 0)
        self.assertEqual(budgets.state(self.db, self.f.budget)["spent"], {"tokens":46, "api_calls":1})

    def test_U07_replay_original_915_snapshot_and_four_183_receipts(self):
        source = ROOT / "acceptance/phase2a/regression/final/batch/extra-child/observation.json"
        data = json.loads(source.read_text())
        rows = data["meter_snapshots"][-1]["rows"]
        child, = rows["p0_native_child_meters"]
        receipts = rows["p0_meter_responses"]
        child_receipts = [r for r in receipts if r["thread_id"] == child["child_id"]]
        self.assertEqual([r["tokens"] for r in child_receipts], [183] * 4)
        self.assertEqual(len({r["turn_id"] for r in child_receipts}), 1)
        self.assertTrue(all(not r["conflicting"] for r in receipts))
        parent = self.f.start()
        cid = child["child_id"]
        cturn = child_receipts[0]["turn_id"]
        envelope = self.start_child(parent, cid, cturn)
        for row in receipts:
            if row["thread_id"] == child["root_id"]:
                self.assertEqual(row["turn_id"], child["owner_turn"])
                self.f.raw(parent, row["response_id"], row["tokens"])
            else:
                self.f.delivery.observe_descendant("rawResponse/completed", {**envelope, "responseId":row["response_id"],
                                                   "usage":{"totalTokens":row["tokens"]}})
        for row in rows["p0_meter_threads"]:
            if row["thread_id"] == cid:
                self.f.delivery.observe_descendant("thread/tokenUsage/updated", {**envelope, "tokenUsage":{
                    "total":{"totalTokens":row["observed_total"]}}})
            else:
                self.assertEqual(row["thread_id"], child["root_id"])
                self.f.sample(parent, row["observed_total"])
        self.f.delivery.observe_descendant("turn/completed", {**envelope, "turn":{"id":cturn,"status":"completed"}})
        self.f.complete()
        self.history["source"] = {"path":str(source.relative_to(ROOT)), "sha256":hashlib.sha256(source.read_bytes()).hexdigest(),
                                  "evidence_class":"OFFICIAL_HOST_LOOPBACK", "historical_result":data["observed_status"],
                                  "thread_map":{child["root_id"]:"root", cid:cid}, "parent_turn_map":{child["owner_turn"]:parent}}
        self.assertEqual(self.inspect("before_grace")["turns"][0]["budget_status"], "HELD")
        clock.advance(31)
        self.drive()
        view = self.inspect("after_grace")
        cost = view["cost_report"]
        account, = [a for a in cost["threads"] if a["thread_id"] == cid]
        self.assertEqual((account["thread_confirmed_cumulative"], account["turn_attributed_total"],
                          account["unattributed_delta"]), (915, 732, 183))
        self.assertEqual(cost["PROVIDER_CONFIRMED"]["tokens"], 837)
        self.assertEqual(cost["UNATTRIBUTED_DELTA"]["tokens"], 183)
        self.assertEqual(view["turns"][0]["settled_tokens"], 837)
        self.assertEqual(view["risk"]["unfunded_closed_thread_tokens"], 183)
        self.assertEqual(view["risk"]["committed_tokens"], 1020)
        self.assertEqual(view["adjustments"], [])
        self.assertTrue(all(r["provider_confirmed_tokens"] != 915 for r in cost["turn_receipts"]))

    def test_U08_process_restart_across_real_deadline(self):
        self.configure(1)
        self.interrupted()
        before = self.inspect("before_restart")["turns"][0]
        self.assertEqual(before["budget_status"], "HELD")
        deadline = clock.parse(before["terminal_at"]).timestamp() + 1
        time.sleep(max(0, deadline - time.time()) + .03)
        dbfile = self.f.directory / "ledger.sqlite"
        env = test_environment(self.f.directory / "child")
        env.update(PYTHONPATH=str(ROOT / "gx-h"), GX_CODEX_OFFLINE="1")
        code = ("from contextlib import closing, nullcontext\nimport os,pathlib,sys\n"
                "from gx_core.persistence import store\nfrom gx_core.p0.driver import Driver\n"
                "p=pathlib.Path(sys.argv[1])\n"
                "with closing(store.open_store(p)) as db, closing(Driver(db,p.parent,sys.argv[2])) as owner:\n"
                " owner.drive(0.3)\n print(os.getpid())\n")
        pids = []
        for _ in range(2):
            result = subprocess.run([sys.executable, "-c", code, str(dbfile), self.run], cwd=ROOT,
                                    env=env, capture_output=True, text=True, timeout=6, check=True)
            pids.append(int(result.stdout.strip()))
        self.history["driver_processes"] = {"creating_pid":os.getpid(), "reopened_pids":pids, "deadline_utc_epoch":deadline}
        self.assertEqual(len({*pids, os.getpid()}), 3)
        after = self.inspect("after_reopened_drivers")["turns"][0]
        self.assertEqual((after["reservation_ref"], after["usage_class"]), (before["reservation_ref"], "ESTIMATED"))
        self.assertGreaterEqual(clock.parse(after["estimated_at"]).timestamp(), deadline)
        self.assertEqual(sum(e["kind"] == "budget.settled" for e in self.events()), 1)

    def test_unestimable_legacy_hold_stays_unresolved(self):
        self.interrupted()
        # Some historical reservations have no token line or per-Turn grant.
        ref, = [r["reservation_ref"] for r in self.inspect("admitted")["turns"]]
        self.db.execute("UPDATE p0_root_deliveries SET token_budget=NULL WHERE reservation_ref=?", (ref,))
        self.db.execute("UPDATE budget_reservations SET amounts_json=? WHERE reservation_ref=?", ('{"api_calls":1}', ref))
        clock.advance(31)
        self.drive()
        view = self.inspect("no_usable_grant")
        self.assertEqual((view["turns"][0]["confidence"], view["turns"][0]["budget_status"]), ("UNRESOLVED", "HELD"))
        self.assertIsNone(metering.next_deadline_seconds(self.db, self.run))
        self.assertIsNone(view["cost_report"]["UNRESOLVED"]["tokens"])
        self.assertEqual([e for e in self.events() if e["kind"] == "budget.settled"], [])
