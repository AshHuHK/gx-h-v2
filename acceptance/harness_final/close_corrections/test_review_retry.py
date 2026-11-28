"""Ordinary research CLI, real locators and review receipts, controlled official dynamic tool callbacks."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from acceptance.harness_final.late_pi.test_late_feedback import feedback
from acceptance.harness_refactor.closing.test_closeout import Study
from acceptance.v622.test_close import actual_packet, close_decision
from acceptance.v62a.interleavings_fixture import draft
from gx_core.hostobs.app_server_client import NO_TOOL_RESPONSE
from gx_core.p0 import v6_goal


def submit(s, actor, refs, call_id, *, pi=None):
    decision = close_decision(refs)
    if pi:
        decision["pi_dispositions"] = [{"feedback_id": pi["feedback_id"], "outcome":"maintain",
                                      "reason":"Restrict the comparison to the two calibrated sources."}]
    envelope = {"threadId":actor.thread, "turnId":actor.turn, "namespace":"gx", "tool":"gx_decide_revision",
                "callId":call_id, "arguments":{"decision":decision}}
    wire = s.roots._callback(SimpleNamespace(message={"method":"item/tool/call", "params":envelope}))
    if wire is NO_TOOL_RESPONSE:
        row = s.conn.execute("SELECT receipt_json FROM v6_decisions WHERE run_id=? AND task_id=? AND call_id=?",
                             (s.run_id,actor.task_id,call_id)).fetchone()
        if row is None:
            raise AssertionError("Suppressed callback without a committed close")
        return True, json.loads(row[0])
    return wire["success"], json.loads(wire["contentItems"][0]["text"])


def save_receipt(name, value):
    if path := os.environ.get("GX_REVIEW_RETRY_RECEIPTS"):
        (Path(path)/(name+".json")).write_text(json.dumps(value,indent=2,ensure_ascii=False)+"\n")


class ReviewRetry(unittest.TestCase):
    def setUp(self):
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        files = []
        for name, text in (("depot","Both reference clocks were calibrated before the evening sample."),
                           ("archive","The archive lists a separate standard for each reference clock.")):
            path = base/(name+".txt")
            path.write_text(text)
            files.append({"path":str(path),"role":"literature"})
        self.s = self.enterContext(Study(base/"study",spec_updates={"research_effort":"deep",
            "research_modules":["scholarly_investigation@1"], "plugins":["org.gx.scholarly-sources@1.0.0"],
            "capabilities":[], "input_files":files}))
        s = self.s
        self.assertEqual(actual_packet(s,s.lead)["packet"]["harness_policy"],"gx.harness/3")
        extractions = []
        for i,ref in enumerate(s.materials):
            segment = {"source_ref":ref,"segment_id":"p1"}
            read = s.call(s.lead,"gx_read_source",segment,"read-"+str(i))
            extract = s.call(s.lead,"gx_extract_evidence",{**segment,"receipt_id":read["receipt_id"],
                "claim":read["text"],"quote":read["text"],"direction":"supports",
                "applicability":"These reference clocks only.","confidence_basis":"Verbatim statement in the source."},
                "extract-"+str(i))
            extractions.append(extract)
        s.call(s.lead,"gx_decide_revision",{"decision":draft(evidence=[e["evidence_ref"] for e in extractions])},"publish")
        v6_goal.record_observation(s.conn,s.run_id,s.materials[0],source="request-source-reassessment")
        self.actor = s.next_actor("run")
        self.start = actual_packet(s,self.actor)
        self.refs = []
        for i,(ref,extract) in enumerate(zip(s.materials,extractions)):
            read = s.call(self.actor,"gx_read_source",{"source_ref":ref,"segment_id":"p1"},"reread-"+str(i))
            review = s.call(self.actor,"gx_review_evidence",{
                "evidence_ref":extract["evidence_ref"],"receipt_id":read["receipt_id"],
                "quote_sha256_seen":extract["quote_sha256"],"verdict":"SUPPORTED",
                "note":"The stated calibration observation appears in the supplied record."},"review-"+str(i))
            self.assertTrue(review["accepted"])
            self.refs.append(review["review_ref"])
        self.assertEqual(len(set(self.refs)),2)
        self.task_count = len(s.rows("SELECT 1 FROM v6_decision_tasks WHERE scope='run'"))

    def assert_repair(self):
        s, actor = self.s,self.actor
        success, notice = submit(s,actor,self.refs[:1],"omitted-second-review")
        self.assertFalse(success)
        self.assertEqual(notice.get("code"),"SCHEMA_INVALID",notice)
        self.assertIs(notice.get("retryable"),True)
        self.assertEqual((notice.get("reason"),notice.get("field")),("uncited_own_reviews","decision.evidence_refs"))
        self.assertNotIn("handoff_event",notice)
        self.assertEqual(notice.get("uncited_review_refs"),self.refs[1:],notice)
        self.assertEqual(submit(s,actor,self.refs[:1],"omitted-second-review"),(False,notice))
        self.assertEqual(s.rows("SELECT 1 FROM events WHERE kind='v6.decision.superseded'"),[])
        self.assertEqual(s.rows("SELECT status,lease_id FROM tasks WHERE task_id=?",actor.task_id),
                         [{"status":"CLAIMED","lease_id":actor.lease_id}])
        self.assertEqual(s.rows("SELECT 1 FROM v6_decisions WHERE task_id=?",actor.task_id),[])
        self.assertEqual(s.rows("SELECT status FROM v6_goals"),[{"status":"OPEN"}])
        self.assertEqual(s.rows("SELECT 1 FROM p0_root_terminals WHERE thread_id=?",actor.thread),[])
        return notice

    def test_missing_own_review_corrects_in_same_native_turn(self):
        s,actor = self.s,self.actor
        notice = self.assert_repair()
        success, final = submit(s,actor,self.refs,"cited-both-reviews")
        self.assertTrue(success,final)
        s.advance()
        self.assertEqual(s.rows("SELECT status,stop_decision_ref FROM v6_goals"),
                         [{"status":"CLOSED","stop_decision_ref":final["decision_ref"]}])
        self.assertEqual(len(s.rows("SELECT 1 FROM v6_decision_tasks WHERE scope='run'")),self.task_count)
        self.assertEqual(s.rows("SELECT 1 FROM jobs"),[])
        save_receipt("same-turn",{"run_id":s.run_id,"task":actor.task_id,"lease":actor.lease_id,"turn":actor.turn,
            "brief_sha256":self.start["text_sha256"],"review_refs":self.refs,"omitted":notice,
            "accepted_decision":final["decision_ref"],"extra_lead_tasks":0,"jobs":0,"host":"controlled official wire"})

    def test_pi_disclosure_between_correction_and_retry_requires_fresh_lead(self):
        s,old = self.s,self.actor
        notice = self.assert_repair()
        text = "The two instruments share a reference clock. Keep sampling independence unresolved."
        addition = feedback(s,"new-clock-record",text)
        success, handoff = submit(s,old,self.refs,"correction-after-pi")
        self.assertFalse(success)
        self.assertEqual(handoff.get("code"),"STALE_REVISION",handoff)
        self.assertIs(handoff.get("retryable"),False)
        superseded = s.rows("SELECT event_id,subject_id FROM events WHERE kind='v6.decision.superseded'")
        self.assertEqual(superseded,[{"event_id":handoff.get("handoff_event"),"subject_id":old.task_id}])
        self.assertNotIn("gx_run_status",json.dumps(handoff))
        self.assertEqual(submit(s,old,self.refs,"correction-after-pi"),(False,handoff))
        # A late extra RPC on the revoked binding must identify the same
        # handoff rather than suggesting that this seat can resubmit again.
        success, again = submit(s,old,self.refs,"repeat-on-revoked-seat")
        self.assertFalse(success)
        self.assertIs(again.get("retryable"),False)
        self.assertEqual(again.get("handoff_event"),handoff["handoff_event"])
        self.assertEqual(s.rows("SELECT 1 FROM v6_decisions WHERE task_id=?",old.task_id),[])
        self.assertEqual(s.rows("SELECT status FROM v6_goals"),[{"status":"OPEN"}])
        current = s.next_actor("run")
        self.assertNotEqual(current.task_id,old.task_id)
        fresh = actual_packet(s,current)
        pi, = [item for item in fresh["packet"]["pi_dialogue"] if item["kind"]=="feedback"]
        self.assertEqual((pi["feedback_id"],pi["text"]),(addition["feedback_id"],text))
        for ref in self.refs:
            self.assertIn(ref,json.dumps(fresh["packet"]))
            self.assertEqual(s.call(current,"gx_read_artifact",{"ref":ref},"open-"+ref)["verdict"],"SUPPORTED")
        success, final = submit(s,current,self.refs,"fresh-close",pi=pi)
        self.assertTrue(success,final)
        self.assertEqual(final["pi_dispositions"][0]["feedback_id"],pi["feedback_id"])
        s.advance()
        self.assertEqual(s.rows("SELECT status,stop_decision_ref FROM v6_goals"),
                         [{"status":"CLOSED","stop_decision_ref":final["decision_ref"]}])
        self.assertEqual(s.rows("SELECT 1 FROM jobs"),[])
        save_receipt("late-pi",{"run_id":s.run_id,"old_task":old.task_id,"old_turn":old.turn,"first_notice":notice,
            "corrected_old_callback":handoff, "review_refs":self.refs,"acknowledged_pi":pi,
            "fresh_task":current.task_id,"fresh_turn":current.turn,"fresh_sha256":fresh["text_sha256"],
            "accepted_decision":final["decision_ref"],"dispositions":final["pi_dispositions"],"jobs":0,
            "host":"controlled official wire"})


if __name__ == "__main__":
    unittest.main()
