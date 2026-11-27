"""Counterexamples for the real-study verifier, with a positive control for each path."""
import unittest

from acceptance.unified_multiturn.live_receipts import discussion_checks, measurement_deliveries


class LiveReceiptAttribution(unittest.TestCase):
    def test_wrong_source_or_version_cannot_answer_the_anchored_trace(self):
        def message(mid, text, anchor=None):
            return dict(message_id=mid, reply_to=anchor, run_id="parent", goal_version=1,
                        reply=dict(text=text, turn_id=mid+"-turn", status="completed"))

        compound = message("challenge", "继续分支 B，负责人决定 20。分支 A 已采纳，报告 36、处理记录 37。")
        trace = message("trace", "收到反馈事件 26，研究者采纳记录 37，研究报告 36。", "challenge")
        sealed = message("sealed", "负责人决定 20，结题决定 96。")
        receipts = {(m["message_id"], m["reply"]["turn_id"]): n for m,n in
                    ((compound,40), (trace,46), (sealed,100))}
        study = dict(run_id="parent", goal_version=1, closure=dict(decision_ref="gxa_close", committed_event=96))
        decisions = {"gxa_retain": dict(waiting_for={"B"}, ref="gxa_retain", event=20)}
        feedback = [dict(event_id=26, feedback_id="pif_a", disposition=dict(
            event_id=37, product_ref="gxa_a", product_event_id=36))]

        def check():
            return discussion_checks(compound, trace, sealed, feedback, {"module_instance_id":"B"},
                                     study, decisions, receipts)

        self.assertEqual(check(), (True, True))
        trace["goal_version"] = 2
        self.assertEqual(check(), (False, True))
        trace["goal_version"] = 1
        trace["reply_to"] = "other-message"
        self.assertFalse(check()[0])
        trace["reply_to"] = "challenge"
        trace["reply"]["text"] = "收到反馈事件 26，采纳记录 37。据另一分支报告 39，共测量 36 对。"
        self.assertEqual(check(), (False, True))

    def test_new_job_plus_unrelated_handoff_or_old_final_citation_cannot_pass(self):
        turn = ("lead", "turn")
        decision = dict(ref="gxa_close", event=50, turn=turn, evidence={"gxa_new", "gxa_report"})
        final = dict(decision_ref=decision["ref"], thread_id=turn[0], turn_id=turn[1], evidence_refs=["gxa_new","gxa_report"])
        job = dict(job_id="job-new", owner={"branch_id":"target"}, outputs=[
            dict(output_name="interface.json", artifact_ref="gxa_new")])
        report = dict(scope="module:m", run_id="child", goal_version=1, report_ref="gxa_report",
                      source_refs=["gxa_new"], event_id=35, released_event=36)
        module = dict(scope=report["scope"], branch_id="target", report=report)
        handoff = dict(publisher=module["scope"], report_ref="gxa_report", published_event=35, released_event=36,
                       receiver_scope="run", status="CITED", received_content=True, decision_ref="gxa_close",
                       endpoint_delivery_events=[41], decision_event=50, thread_id=turn[0], turn_id=turn[1])
        study = dict(run_id="child", goal_version=1, final_decision=final,
                     closure=dict(decision_ref=decision["ref"], committed_event=50),
                     deliveries=[], routes={"modules":[module], "handoffs":[handoff]})
        decisions = {decision["ref"]:decision}

        def uses():
            return [r["output_ref"] for r in measurement_deliveries(study, [job], decisions)]

        self.assertEqual(uses(), ["gxa_new"])
        report["source_refs"] = ["gxa_old"]
        self.assertEqual(uses(), [])
        report["source_refs"] = ["gxa_new"]
        handoff["report_ref"] = "gxa_other"
        self.assertEqual(uses(), [])
        study["deliveries"] = [dict(scope="run", event_id=40, thread_id=turn[0], turn_id=turn[1],
                                    endpoints=[{"source_ref":"gxa_new"}])]
        self.assertEqual(uses(), ["gxa_new"])
        decisions["gxa_previous"] = dict(ref="gxa_previous", event=48, turn=("lead","previous"), evidence={"gxa_new"})
        study["deliveries"][0]["turn_id"] = "previous"
        final["evidence_refs"] = ["gxa_old"]
        decision["evidence"] = {"gxa_old"}
        self.assertEqual(uses(), [])
