"""Run offline operator usage decisions against disk-backed GX stores and save the evidence."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/"gx-h"), str(ROOT/"gx-h/tests")]
from gx_core import budgets
from gx_core.context import Principal
from gx_core.domain import clock
from gx_core.errors import PermissionDenied, SchemaInvalid, StaleRevision
from gx_core.p0 import cli, metering, operator, schema
from gx_core.p0.profiles import CODEX_VERSION
from gx_core.persistence import records, store
from test_p0_meter_recovery import TurnMeterTests, Peer, RootDelivery


class OperatorUsage(unittest.TestCase):
    output = None

    def setUp(self):
        root = self.output
        if root is None:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name)
        self.directory = root/self._testMethodName
        self.directory.mkdir()
        self.path = self.directory/"gx.db"
        self.db = store.open_store(self.path)
        self.addCleanup(self.db.close)
        self.addCleanup(clock.reset)
        schema.install(self.db)
        self.run = self.new_run("one", [2,4,6])
        self.f = TurnMeterTests()
        self.f.db,self.f.run=self.db,self.run
        self.f.budget = budgets.create_root(self.db,run_id=self.run,actor="fixture",profile_name="p0",limits={"tokens":800,"api_calls":5})
        self.db.execute("UPDATE runs SET budget_ref=? WHERE run_id=?", (self.f.budget,self.run))
        self.f.peer = Peer(self.db,self.run)
        self.f.delivery = RootDelivery(self.f.peer)
        self.obs = {}

    def new_run(self,name,intervals=None):
        policy={"grace_seconds":1}
        if intervals:
            policy["unresolved_alert_seconds"] = intervals
        return records.create_run(self.db,objective=name,project_ref="arithmetic",actor="fixture",
                                  inputs={"inputs":{"p0":True,"p0_spec":{"metering":policy}}})

    def snapshot(self,name):
        snap = metering.inspect(self.db,self.run,include_responses=True)
        self.obs[name]=snap
        return snap

    def tearDown(self):
        events = [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY event_id")]
        for event in events:
            event["payload_json"] = json.loads(event["payload_json"])
        (self.directory/"events.json").write_text(json.dumps(events,indent=2)+"\n")
        (self.directory/"usage.json").write_text(json.dumps(self.obs,indent=2)+"\n")

    def unresolved(self, *, amount=None):
        turn = self.f.start()
        if amount is not None:
            self.f.raw(turn,"observed",amount)
        self.f.raw(turn,"late",None)
        self.f.complete()
        ref=self.db.execute("SELECT reservation_ref FROM p0_meter_native_turns WHERE turn_id=?", (turn,)).fetchone()[0]
        self.db.execute("UPDATE p0_root_deliveries SET token_budget=NULL WHERE reservation_ref=?", (ref,))
        self.db.execute("UPDATE budget_reservations SET amounts_json=? WHERE reservation_ref=?", ('{"api_calls":1}',ref))
        self.db.execute("UPDATE budgets SET reserved_json=? WHERE budget_ref=?", ('{"api_calls":1}',self.f.budget))
        clock.advance(1.1)
        metering.maintain(self.db,self.run)
        self.assertEqual(self.snapshot("unresolved")["turns"][0]["confidence"],"UNRESOLVED")
        return ref,turn

    def act(self,ref,decision,mid,conn=None,**fields):
        action={"kind":"usage_resolve","decision":decision,"reservation_ref":ref,"reason":"Review "+decision,**fields}
        return operator.submit(operator.local_context(conn or self.db),self.run,action,mid)["result"]

    def submit_separately(self,ref,decision,mid,**fields):
        with closing(store.open_store(self.path)) as conn:
            return self.act(ref,decision,mid,conn,**fields)

    def test_alerts_configured_per_run_durable_and_deduplicated(self):
        ref,_=self.unresolved()
        self.assertEqual(metering.policy(self.db,self.new_run("defaults"))["unresolved_alert_seconds"],[3600,86400,259200])
        other=self.new_run("different",[10,20,30])
        second_budget=budgets.create_root(self.db,run_id=other,profile_name="p0",actor="fixture",limits={"api_calls":2})
        self.db.execute("UPDATE runs SET budget_ref=? WHERE run_id=?",(second_budget,other))
        other_hold=budgets.reserve(self.db,budget_ref=second_budget,subject_kind="native_turn",subject_id="other-input",
                                   amounts={"api_calls":1},actor="fixture")["reservation_ref"]
        self.db.execute("INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,human,interrupt,state,"
            "method,turn_id,reservation_ref) VALUES(?,'other-input','other-root','other-client','digest',0,0,'acknowledged',"
            "'turn/start','other-turn',?)",(other,other_hold))
        self.db.execute("INSERT INTO p0_root_turn_usage(thread_id,turn_id,terminal_status) VALUES('other-root','other-turn','completed')")
        metering.maintain(self.db,other)
        clock.advance(1.1)
        metering.maintain(self.db,other)
        original=budgets.state(self.db,self.f.budget)
        for n in (2,2,2):
            clock.advance(n)
            metering.maintain(self.db,self.run)
            metering.maintain(self.db,self.run)
        alerts = self.snapshot("third_threshold")["unresolved_alerts"]
        self.assertEqual([a["level"] for a in alerts],["notice","warning","critical"])
        self.assertTrue(all(a["event_id"] and a["action"] and a["reservation_ref"]==ref for a in alerts))
        self.assertEqual(budgets.state(self.db,self.f.budget),original)
        with closing(store.open_store(self.path)) as conn:
            again=operator.submit(operator.local_context(conn),self.run,{"kind":"usage"},"status")["result"]["metering"]
            self.assertEqual(again["unresolved_alerts"],alerts)
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE kind='usage.unresolved_alert'").fetchone()[0],3)
        self.assertEqual(metering.inspect(self.db,other)["unresolved_alerts"],[])
        clock.advance(4.1)
        metering.maintain(self.db,other)
        self.assertEqual([a["threshold_seconds"] for a in metering.inspect(self.db,other)["unresolved_alerts"]],[10])
        for bad in ([],[2,1,3],[1,1,2],[True,2,3],[0,2,3]):
            with self.assertRaises(SchemaInvalid):
                metering.validate_policy({"unresolved_alert_seconds":bad})

    def test_official_references_scope_and_distinct_reconciliation(self):
        ref,turn = self.unresolved(amount=17)
        old_budget=budgets.state(self.db,self.f.budget)
        with self.assertRaises(SchemaInvalid):
            self.act(ref,"attach-evidence","unknown",thread_id="root",turn_id=turn,response_id="forged")
        with self.assertRaises(SchemaInvalid):
            self.act(ref,"attach-evidence","wrong",thread_id="foreign",turn_id=turn,response_id="observed")
        with self.assertRaises(SchemaInvalid):
            self.act(ref,"attach-evidence","amount",thread_id="root",turn_id=turn,response_id="observed",tokens=999)
        local=operator.local_context(self.db)
        with self.assertRaises(PermissionDenied):
            operator.submit(local.child(principal=Principal("another","operator",("other-project",),source="local_operator")),
                            self.run,{"kind":"usage_resolve","decision":"keep-unresolved","reason":"no scope","reservation_ref":ref},"forbidden")
        evidence_action={"kind":"usage_resolve","decision":"keep-unresolved","reservation_ref":ref,"reason":"queued by first operator"}
        first=local.child(principal=Principal("original-operator",local.principal.scope,local.principal.project_refs,source="local_operator"))
        operator.submit(first,self.run,evidence_action,"queued-key",queue_only=True)
        replay=operator.submit(local,self.run,evidence_action,"queued-key")["result"]
        self.assertEqual(replay["operator_subject"],"original-operator")
        attached=self.act(ref,"attach-evidence","genuine",thread_id="root",turn_id=turn,response_id="observed")
        self.assertEqual(attached["evidence"]["tokens"],17)
        waiting=self.act(ref,"reconcile","not-ready")
        self.assertEqual(waiting["after"]["budget_status"],"HELD")
        kept=self.act(ref,"keep-unresolved","keep")
        self.assertEqual(kept,self.act(ref,"keep-unresolved","keep"))
        self.assertEqual(budgets.state(self.db,self.f.budget),old_budget)
        self.snapshot("receipt_incomplete")

    def test_disputed_measurement_releases_only_uncontested_receipts(self):
        turn = self.f.start()
        self.f.raw(turn,"uncontested",17)
        self.f.raw(turn,"conflicting",90)
        self.f.complete()
        self.f.raw(turn,"conflicting",100)
        clock.advance(2)
        metering.maintain(self.db,self.run)
        ticket=self.snapshot("conflicting_original_measurement")["turns"][0]
        self.assertEqual((ticket["confidence"],ticket["measured_tokens"]),("UNRESOLVED",107))
        ref=ticket["reservation_ref"]
        args=["operator","usage-resolve","--state",str(self.directory),"--run-id",self.run,"--reservation-ref",ref,
              "--reason","Charge verified components","--decision","accept-exposure","--message-id","accepted"]
        with redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(cli.main(args),0)
        accepted=json.loads(stream.getvalue())["result"]
        self.assertEqual(accepted["before"]["confirmed_lower_bound"],17)
        released=self.act(ref,"release-hold","separate",approve_release=True,acceptance_event_id=accepted["operator_event_id"])
        self.assertEqual(released["after"]["settled_tokens"],17)
        self.assertEqual(budgets.state(self.db,self.f.budget)["spent"],{"api_calls":1,"tokens":17})
        self.snapshot("released_disputed")

    def test_release_then_distinct_late_cost_under_closed_funding(self):
        fund=budgets.open_child(self.db,parent_ref=self.f.budget,subject_kind="team",subject_id="A",
                                limits={"tokens":300,"api_calls":2},actor="fixture")["budget_ref"]
        with patch("gx_core.p0.team_admission.team_funding",return_value=fund):
            ref,turn=self.unresolved(amount=17)
        self.db.execute("UPDATE budgets SET reserved_json=? WHERE budget_ref=?", ('{"api_calls":1}',fund))
        self.db.execute("UPDATE budgets SET reserved_json=? WHERE budget_ref=?", ('{"api_calls":2,"tokens":300}',self.f.budget))
        accepted=self.act(ref,"accept-exposure","accept")
        opts={"acceptance_event_id":accepted["operator_event_id"],"approve_release":True}
        with self.assertRaises(StaleRevision):
            self.act(ref,"release-hold","wrong-event",acceptance_event_id=9999,approve_release=True)
        with self.assertRaises(SchemaInvalid):
            self.act(ref,"release-hold","implicit",acceptance_event_id=opts["acceptance_event_id"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.submit_separately(ref,"release-hold","explicit",**opts),range(2)))
        self.assertEqual(*results)
        self.assertEqual(results[0]["after"]["budget_status"],"RELEASED")
        self.assertEqual(budgets.state(self.db,fund)["spent"],{"api_calls":1,"tokens":17})
        budgets.release(self.db,budgets.FUND_PREFIX+fund,actor="fixture",reason="close funding")
        self.snapshot("released")
        self.f.raw(turn,"late",23)
        self.f.sample(turn,40)
        self.f.raw(turn,"late",23)
        final=self.snapshot("late_official_final")
        self.assertEqual((final["turns"][0]["budget_status"],final["turns"][0]["usage_class"]),("RELEASED","PROVIDER_CONFIRMED"))
        self.assertEqual(final["unresolved_cases"],[])
        self.assertEqual(budgets.state(self.db,fund)["spent"],{"api_calls":1,"tokens":40})
        self.assertEqual(budgets.state(self.db,self.f.budget)["spent"],{"api_calls":1,"tokens":40})
        self.assertEqual(final["risk"]["observed_plus_settled"],40)
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE kind='usage.released_receipt_confirmed'").fetchone()[0],1)
        self.assertEqual(self.act(ref,"release-hold","explicit",**opts),results[0])

    def test_release_ambiguous_send_keeps_identity_and_old_rights(self):
        turn=self.f.start()
        ref=self.db.execute("SELECT reservation_ref FROM p0_meter_native_turns WHERE turn_id=?",(turn,)).fetchone()[0]
        self.db.execute("UPDATE p0_root_deliveries SET turn_id=NULL,state='sending' WHERE reservation_ref=?",(ref,))
        before=self.snapshot("ambiguous_sending")
        self.assertIsNone(before["unresolved_cases"][0]["turn_id"])
        accepted=self.act(ref,"accept-exposure","exposure")
        kept=self.act(ref,"keep-unresolved","defer")
        self.assertEqual(kept["after"]["budget_status"],"HELD")
        with self.assertRaises(StaleRevision):
            self.act(ref,"release-hold","stale",approve_release=True,acceptance_event_id=accepted["operator_event_id"])
        accept=self.act(ref,"accept-exposure","updated")
        out=self.act(ref,"release-hold","explicit",approve_release=True,acceptance_event_id=accept["operator_event_id"])
        self.assertEqual((out["after"]["budget_status"],out["known_api_calls"]),("RELEASED",0))
        self.assertFalse(any(budgets.state(self.db,self.f.budget)["spent"].values()))
        self.f.delivery._ack("start",turn)
        self.f.raw(turn,"verified",29)
        self.f.sample(turn,29)
        self.f.complete()
        final=self.snapshot("verified_late_ack")
        self.assertEqual(final["turns"][0]["budget_status"],"RELEASED")
        self.assertEqual(budgets.state(self.db,self.f.budget)["spent"],{"api_calls":1,"tokens":29})
        self.assertEqual(len(self.f.peer.sent),1)
        second=self.f.delivery.deliver("root","independently funded",event_id="new-input")["turn_id"]
        self.f.delivery.observe("turn/started",{"threadId":"root","turn":{"id":second}})
        self.f.raw(second,"new-receipt",7)
        self.f.sample(second,36)
        self.f.complete()
        self.assertEqual(self.snapshot("next_work_after_release")["turns"][-1]["confidence"],"PROVIDER_CONFIRMED")
        self.assertEqual(budgets.state(self.db,self.f.budget)["spent"],{"api_calls":2,"tokens":36})

    def test_later_bound_source_verifies_ids_and_reconciles(self):
        ref,turn=self.unresolved()
        path=self.directory/"official-rollout.jsonl"
        self.f.delivery.attached("root",{"id":"root","path":str(path),"turns":[]})
        args={"source":"codex-rollout","thread_id":"root","turn_id":turn,"response_id":"late"}
        with self.assertRaises(SchemaInvalid):
            self.act(ref,"attach-evidence","absent",**args)
        def entry(kind,**fields):
            return {"type":"event_msg","payload":{"type":kind,"turn_id":turn,"root_turn_id":turn,**fields}}
        header={"type":"session_meta","payload":{"id":"root","session_id":"root","cli_version":CODEX_VERSION}}
        record={"type":"token_usage_record","payload":{"thread_id":"root","session_id":"root","turn_id":turn,
            "root_turn_id":turn,"response_id":"late","usage":{"total_tokens":42},"turn_token_usage":{"total_tokens":42}}}
        rows=[header,entry("task_started"),record,entry("token_count",info={"total_token_usage":{"total_tokens":42}}),entry("task_complete")]
        path.write_text("\n".join(map(json.dumps,rows))+"\n")
        with self.assertRaises(SchemaInvalid):
            self.act(ref,"attach-evidence","foreign",**{**args,"response_id":"other"})
        saved=self.act(ref,"attach-evidence","new-official",**args)
        self.assertEqual(saved["after"]["budget_status"],"HELD")
        self.assertEqual(saved["evidence"]["tokens"],42)
        self.snapshot("evidence_attached")
        settled=self.act(ref,"reconcile","reconcile")
        self.assertEqual(settled["after"]["usage_confidence"],"PROVIDER_CONFIRMED")
        self.assertEqual(self.act(ref,"reconcile","reconcile"),settled)
        self.assertEqual(budgets.state(self.db,self.f.budget)["spent"],{"api_calls":1,"tokens":42})
        self.snapshot("reconciled")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    OperatorUsage.output=args.output
    cases={}
    for name in unittest.defaultTestLoader.getTestCaseNames(OperatorUsage):
        outcome=unittest.TextTestRunner(verbosity=2,stream=sys.stdout).run(OperatorUsage(name))
        cases[name]={"status":"PASS" if outcome.wasSuccessful() else "FAIL","ledger":f"{name}/gx.db",
                     "events":f"{name}/events.json","observations":f"{name}/usage.json"}
    result={"passed":all(c["status"]=="PASS" for c in cases.values()),"cases":cases,"evidence_class":"SCRIPTED"}
    (args.output/"result.json").write_text(json.dumps(result,indent=2)+"\n")
    return int(not result["passed"])


if __name__=="__main__":
    sys.exit(main())
