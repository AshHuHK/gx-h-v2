import unittest

from gx_core.p0.research_page import complete_decision


class DecisionReading(unittest.TestCase):
    def test_original_rationale_and_both_endpoint_types_are_preserved(self):
        conclusion = "A short summary. " + "Clinical scope is unknown. " * 12 + "The original cutoff was 4.7."
        result = {"goal":"Read the records", "closure":{"closed_event":103},
                  "final_decision":{"decision_ref":"gxa_decision","evidence_refs":["gxa_source"],
                      "rendered":{"reason":"The investigation is closed.","rationale":conclusion,
                                  "unresolved":["Coverage was partial."]},
                      "used_endpoints":[{"reading":{"metric_id":"radius","display":"12.216 Å","output_name":"qc.json"},
                                         "source_ref":"gxa_qc","event_id":81},
                                        {"reading":{"observable":"/mass","value":4.7,"unit":"g"},
                                         "source_ref":"gxa_data","event_id":76}]}}
        page=complete_decision(result)
        for expected in (conclusion, "12.216 Å", "/mass · 4.7 g · gxa_data", "delivery event 76",
                         "gxa_source", "Coverage was partial.", "closed at event 103"):
            self.assertIn(expected,page)
