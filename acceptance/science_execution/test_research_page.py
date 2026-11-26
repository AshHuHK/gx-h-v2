"""Project the accepted decision and frozen gaps without replacing a prediction with a later rewrite."""
import json
import sqlite3
import unittest

from gx_core.p0 import research_page, research_report


class ResearchPage(unittest.TestCase):
    def test_scoped_prediction_and_commission_stay_readable(self):
        prediction = {"statement":"与其他蛋白共沉积的模型中，预测天然 Leu118–Val137 的 G/E 链对将发生变化。",
                      "prediction":{"method":{"metric_id":"pair_delta_A","unit":"Å",
                                               "parameters":{"block_index":6,"mask":"common_ca","pair":"31"}},
                                    "operator":"le","threshold":-0.042}}
        self.assertEqual(research_page._target(prediction,True), "Leu118–Val137 · G/E Cα ΔRMSD (≤ -0.042 Å)")
        action = {"jobs":[{"job_id":"executed"}], "rendered_rationale":"若各组相似，则将研究收窄。",
                  "objectives":["完整主链复核中，E/F 的 RMSD 差是否 ≤ −0.25 Å，且降幅超过相同分散对照的最大变化？"]}
        self.assertEqual(research_page._action_text(action,40),"完整主链复核中，E/F 的 RMSD 差是否 ≤ −0.25 Å？")

    def test_short_close_keeps_a_numeric_recommendation_and_enacted_multimethod_work(self):
        def decision(event, ops, methods, reports):
            return {"event_id":event, "operations":[{"op":op} for op in ops],
                    "metric_refs":methods, "report_events":list(reports), "report_statuses":reports,
                    "jobs":[{"job_id":"new"}] if "add_node" in ops else [], "objectives":["Read the affected pair."],
                    "rendered_rationale":"We selected two measurements to distinguish the alternatives."}
        methods = [{"method":{"id":name}, "outputs":[{"artifact_ref":name, "metrics":[{"metric_ref":name}]}]}
                   for name in ("geometry","interface")]
        close_reason = "One cohort supports a narrower conclusion."
        finding = "The contrast was 0.24 Å. The two groups should be reported separately."
        result = {"goal":"How do these groups compare?", "closure":{"committed_event":80,"closed_event":82},
                  "final_decision":{"rendered":{"reason":close_reason,"rationale":"An early observation. "*80+"\n\n"+finding,
                                               "unresolved":["Missing coverage."]}}, "predictions":[], "jobs":methods,
                  "materials":[], "rejected_decisions":[],
                  "post_read_actions":[decision(10,[],["geometry"],{}),decision(20,["stop_branch"],["geometry"],{}),
                                       decision(40,["add_node"],[],{25:"ENACTED"}),
                                       decision(70,["stop_branch","add_node"],["geometry","interface"],{35:"ENACTED"})]}
        page = research_page.page(result)
        self.assertTrue(page.startswith("# Research findings\n\n"+close_reason))
        self.assertIn(finding,page)
        self.assertIn("geometry + interface",page)
        self.assertIn("- 40 ",page)
        self.assertIn("- 70 ",page)
        self.assertNotIn("- 10 ",page)
        self.assertNotIn("- 20 ",page)
        self.assertLess(len(page),1200)

    def test_original_counterexample_remains_visible_after_a_retrospective_rewrite(self):
        reading = {"artifact_ref":"output", "value":0, "unit":"1",
                   "denominators":{"blocks_missed_observed":0, "blocks_evaluable":13}}
        prediction = {"statement":"Both materials contain a miss.", "registration":{"event_id":12},
                      "prediction":{"method":{"metric_id":"missed_present"}}, "classification":"prospectively_tested",
                      "assessments":[{"direction":"opposed","results":[reading]}],
                      "revisions":[{"prediction":{"method":{"metric_id":"missed_present"}},
                                    "assessments":[{"direction":"undetermined"}]}]}
        data = {"goal":"Which structures contain a miss?", "closure":{"committed_event":20,"closed_event":21},
                "final_decision":{"rendered":{"reason":"Stop.","rationale":"A counterexample was measured.","unresolved":[]}},
                "predictions":[prediction], "jobs":[{"outputs":[{"artifact_ref":"output","summary":{"entry":"sample B"}}]}],
                "materials":[], "post_read_actions":[], "rejected_decisions":[]}
        page = research_page.page(data)
        self.assertEqual(page.count(prediction["statement"]), 1)
        for value in ("Contradicted","sample B Absent (0/13 evaluable blocks missed)", "are retained (1)", "(result.json)"):
            self.assertIn(value, page)

    def test_frozen_gap_and_actual_refusal_correction(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE gaps(run_id,namespace,gap_id,question);
            CREATE TABLE v6_decisions(run_id,decision_ref,context_ref);
            CREATE TABLE research_contexts(run_id,context_ref,snapshot_json);
            CREATE TABLE events(run_id,event_id,subject_id,kind,payload_json);
        """)
        key = "gap:" + "a"*64
        db.execute("INSERT INTO gaps VALUES (?,?,?,?)", ("r","r",key,"Updated question"))
        db.execute("INSERT INTO v6_decisions VALUES ('r','closed','c')")
        db.execute("INSERT INTO research_contexts VALUES ('r','c',?)",(json.dumps({"research_progress":{
            "open_gaps":[{"gap_ref":key,"question":"What was unmeasured?"}]}}),))
        self.assertEqual(research_report._gap_questions(db,"r","closed",[key]), {key:"What was unmeasured?"})
        db.executemany("INSERT INTO events VALUES ('r',?,?,?,?)",[
            (1,"a","v624.decision.rejected",'{"reason":"endpoint_ambiguous"}'),
            (2,"b","v624.decision.rejected",'{"reason":"SCOPE_DENIED"}'),
            (4,"a","v624.decision.corrected",'{"decision_event":3}'),
            (6,"b","v624.decision.corrected",'{"decision_event":5}')])
        rows = research_report._rejections(db,"r",4)
        self.assertEqual([(r["reason"],r["corrected_decision_event"]) for r in rows],
                         [("endpoint_ambiguous",3),("SCOPE_DENIED",None)])
