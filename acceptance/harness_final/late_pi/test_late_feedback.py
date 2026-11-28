"""A closing choice belongs to its delivered PI dialogue and Goal. Exercise the actual message and Goal owners."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.harness_refactor.closing.test_closeout import Study
from acceptance.multiturn import test_pi_questions
from acceptance.v621 import test_goal_versions
from acceptance.v622.test_close import actual_packet, close_decision
from acceptance.v62a.interleavings_fixture import draft
from gx_core.errors import StaleRevision
from gx_core.p0 import pi_materials, research_dialogue, v6_decisions
from gx_core.p0.driver import Driver
from tests.test_v6_operator_recovery import HumanReceipt
from tests.test_v6_decisions import branch


FIRST = "The two time stamps use different clocks."
SECOND = "The newest calibration certificate applies only to the reference instrument."


def feedback(s, key, text):
    reply = test_pi_questions.PIQuestions().route_message(s, key, text, "feedback", source_kind="information", branch_id="run")
    clause, = reply["clauses"]
    if clause["status"] != "routed" or clause["source_message_id"] != key:
        raise AssertionError(clause)
    row, = research_dialogue.feedback_status(s.conn, s.run_id, key)
    if row["text"] != text or row["scope"] != "run":
        raise AssertionError(row)
    return row


def decision(s, actor, key, *, items=(), close=True):
    scientific_choice = close_decision() if close else draft()
    if items:
        scientific_choice["pi_dispositions"] = [
            {"feedback_id":f["feedback_id"], "outcome":"maintain",
             "reason":"Limit the conclusion to the observable record."} for f in items]
    return s.call(actor, "gx_decide_revision", {"decision":scientific_choice}, key)


class LatePI(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_earlier_lead_cannot_close_after_two_late_main_feedback_receipts(self):
        for when in ("after_ack", "after_prepare"):
            with self.subTest(when=when):
                self.check_delivery_boundary(when)

    def check_delivery_boundary(self, when):
        with Study(self.directory/when) as s:
            earlier = s.lead
            first_packet = actual_packet(s,earlier)
            self.assertFalse(first_packet["packet"].get("pi_dialogue"))
            items = []
            def route_pair():
                items.extend((feedback(s,"time-stamps",FIRST),feedback(s,"reference-instrument",SECOND)))
            def routed_after_prepare(*args,**kwargs):
                plan = prepare(*args,**kwargs)
                route_pair()
                return plan
            prepare = v6_decisions.prepare
            if when == "after_ack":
                route_pair()
            hook = patch.object(v6_decisions,"prepare",routed_after_prepare) if when == "after_prepare" else nullcontext()
            with hook:
                try:
                    old_receipt = decision(s,earlier,"old-close")
                except StaleRevision as exc:
                    rejection = exc.details
                else:
                    self.fail(json.dumps({"accepted_obsolete_close":old_receipt["decision_ref"],
                        "run_id":s.run_id, "frozen_cursor":first_packet["packet"]["trigger"]["through_cursor"],
                        "late_feedback_events":[f["event_id"] for f in items],
                        "state":s.rows("SELECT status FROM v6_goals")[0]["status"]}))
            cursor = first_packet["packet"]["trigger"]["through_cursor"]
            self.assertTrue(all(f["event_id"] > cursor for f in items))
            self.assertEqual(s.rows("SELECT status,stop_decision_ref FROM v6_goals"),
                             [{"status":"OPEN","stop_decision_ref":None}])
            self.assertEqual(s.rows("SELECT 1 FROM v6_decisions WHERE call_id='old-close'"),[])
            # Close the connection before the next normal driver step. The
            # successor must be reconstructed from the committed clauses.
            rid = s.run_id
        with Study(self.directory/when,reopen=True) as cold:
            self.assertEqual(cold.run_id,rid)
            current = cold.next_actor("run")
            self.assertNotEqual(current.task_id,earlier.task_id)
            packet = actual_packet(cold,current)
            received = [f for f in packet["packet"]["pi_dialogue"] if f["kind"] == "feedback"]
            self.assertEqual([(f["feedback_id"],f["text"]) for f in received],
                             [(f["feedback_id"],f["text"]) for f in items])
            answer = decision(cold,current,"informed-close",items=items)
            cold.advance()
            self.assertEqual(cold.rows("SELECT status,stop_decision_ref FROM v6_goals"),
                             [{"status":"CLOSED","stop_decision_ref":answer["decision_ref"]}])
            self.assertEqual(cold.rows("SELECT 1 FROM jobs"),[])
            status = research_dialogue.feedback_status(cold.conn,cold.run_id)
            self.assertEqual([f["status"] for f in status],["DISPOSED","DISPOSED"])
            if target := os.environ.get("GX_LATE_PI_RECEIPTS"):
                (Path(target)/(when+".json")).write_text(json.dumps({
                    "run_id":rid,"goal_version":packet["packet"]["goal_version"], "prior_task":earlier.task_id,
                    "acknowledged_cursor":cursor, "prior_brief_sha256":first_packet["text_sha256"],
                    "stale_reason":rejection, "successor_task":current.task_id,
                    "delivered_brief_sha256":packet["text_sha256"], "received":received,
                    "effective_close":answer["decision_ref"], "dispositions":answer["pi_dispositions"],
                    "jobs":0,"transport":"scripted official callbacks; CLI PI ingress and Main routing"
                },indent=2,ensure_ascii=False)+"\n")

    def test_disclosed_and_disposed_feedback_or_main_only_discussion_do_not_block_close(self):
        with Study(self.directory/"already") as s:
            item = feedback(s,"first",FIRST)
            decision(s,s.lead,"work",close=False)
            informed = s.next_actor("run")
            in_packet = actual_packet(s,informed)["packet"]["pi_dialogue"]
            self.assertEqual([f["text"] for f in in_packet if f["kind"]=="feedback"],[FIRST])
            reply = test_pi_questions.PIQuestions().route_message(s,"discuss","Discuss presentation separately.","discussion")
            self.assertEqual(reply["clauses"][0]["status"],"for_main_reply")
            closed = decision(s,informed,"seen-close",items=[item])
            s.advance()
            self.assertEqual(s.rows("SELECT stop_decision_ref FROM v6_goals"),[{"stop_decision_ref":closed["decision_ref"]}])
        with Study(self.directory/"disposed") as s:
            item = feedback(s,"first",FIRST)
            decision(s,s.lead,"yield",close=False)
            current = s.next_actor("run")
            response = decision(s,current,"record-disposition",items=[item],close=False)
            self.assertIn("pi_dispositions",response)
            feedback(s,"next-record","The reference series ends at the recorded date.")
            final = s.next_actor("run")
            self.assertNotIn(FIRST,json.dumps(actual_packet(s,final)["packet"].get("pi_dialogue",[])))
            self.assertIn("decision_ref",decision(s,final,"no-repeat"))

    def test_previous_goal_feedback_cannot_block_or_enter_current_goal(self):
        with Study(self.directory) as s:
            old = feedback(s,"old",FIRST)
            self.assertEqual(old["goal_version"],1)
            test_goal_versions.GoalVersions().change_goal(s)
            lead = s.next_actor("run")
            packet = actual_packet(s,lead)["packet"]
            self.assertEqual(packet["goal_version"],2)
            self.assertNotIn(FIRST,json.dumps(packet.get("pi_dialogue",[])))
            self.assertIn("decision_ref",decision(s,lead,"current-goal-close"))

    def test_branch_private_material_omits_body_and_does_not_refresh_lead(self):
        with Study(self.directory) as s:
            op = branch("archive")
            op.update(hypothesis="One series has a different calibration.",alternative="Both use the stated reference.",
                      method_family="record comparison",discriminating_observation="Dated reference certificate")
            opened = s.call(s.lead,"gx_decide_revision",{"decision":draft(op)},"open-archive")
            branch_id = opened["aliases"]["local:archive"]
            feedback(s,"existing","The common catalog is ready for final comparison.")
            lead = s.next_actor("run")
            previous = actual_packet(s,lead)
            text = "Consult the signed reference only in this archive branch."
            original = self.directory/"sealed.txt"
            original.write_text("Confidential reference C7")
            sent = test_pi_questions.cli(s,"message","--run-id",s.run_id,"--message-id","sealed",
                "--text",text,"--branch-id",branch_id,"--attachment",str(original),"--queue-only")
            self.assertEqual(sent.returncode,0,sent.stderr)
            confidential = pi_materials.for_message(s.conn,s.run_id,"sealed")[0]["artifact_ref"]
            row = s.conn.execute("SELECT * FROM p0_operator_messages WHERE run_id=? AND message_id='sealed'",(s.run_id,)).fetchone()
            driver = Driver(s.conn,s.state,s.run_id)
            try:
                driver._human_turn(HumanReceipt(),row)
                reply = driver.main_read("gx_route_pi_message",{"clauses":[{"kind":"material","quote":text,
                    "branch_id":branch_id,"artifact_refs":[confidential]}]},
                    {"thread_id":"test-native-main","turn_id":"test-turn-sealed","call_id":"seal"})
                self.assertEqual(reply["clauses"][0]["status"],"routed")
            finally:
                driver.close()
            raw = json.dumps(research_dialogue.packet(s.conn,s.run_id,"run",1))
            self.assertNotIn("Confidential reference C7",raw)
            self.assertNotIn(confidential,raw)
            self.assertEqual(previous,actual_packet(s,lead))
            self.assertIn("decision_ref",decision(s,lead,"root-close"))


if __name__ == "__main__":
    # Imports above reuse fixtures, not their unrelated historical test campaigns.
    unittest.main(defaultTest="LatePI")
