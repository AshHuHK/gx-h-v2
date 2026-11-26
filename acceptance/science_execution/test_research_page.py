"""Project the accepted decision and frozen gaps without replacing a prediction with a later rewrite."""
import json
import sqlite3
import unittest

from gx_core.p0 import research_page, research_report


class ResearchPage(unittest.TestCase):
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
