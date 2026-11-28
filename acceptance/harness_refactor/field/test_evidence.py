"""Evidence classification controls, independent of the live scientific actors."""
import tempfile
import unittest
from pathlib import Path

from acceptance.harness_refactor.field import evidence


class FieldEvidence(unittest.TestCase):
    def test_missing_database_remains_unadmitted_and_is_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            for case in ("yeast", "a", "b"):
                report = evidence.collect(Path(tmp), case)
                self.assertEqual(report["status"], "not_admitted")
                self.assertEqual(report["observed"], [])
                self.assertTrue(report["missing"])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_visible_index_alone_does_not_prove_delivery_of_scientific_content(self):
        self.assertEqual(evidence.delivered_content({
            "evidence_refs":["r"], "module_report_index":[{"report_ref":"r","truncated":True}]}), [])
        available = evidence.delivered_content({"module_reports":[{
            "report_ref":"r", "findings":[{"text":"Three dated observations."}], "truncated":False}]})
        self.assertEqual(available, [{"ref":"r","fields":["findings"],"truncated":False}])

    def test_late_read_and_other_reader_are_not_the_childs_prior_read(self):
        method = "yeast-archive-lookup@2"
        actor = "module:reader"
        trace = {"jobs":[],"lead_decisions":[],"explicit_method_choices":{},"native_calls":[
            {"tool":"gx_open_module","official":True,"scope":actor,"arguments":{},"eligible_methods":[method]},
            {"tool":"gx_select_capability","official":True,"scope":actor,"caller_scope":actor,
             "event_id":11,"call_id":"bind","seq":5,"arguments":{"action":"enable","method":method}}],
            "original_reads":[{"scope":actor,"official":True,"source_digest_verified":True,
                              "receipt_id":"late","call_id":"read","kind":"jats","seq":6},
                             {"scope":"module:sibling","official":True,"source_digest_verified":True,
                              "receipt_id":"other","call_id":"othercall","kind":"jats","seq":2}]}
        scored, _ = evidence.proofset("yeast",trace)
        self.assertTrue(scored[0][0])
        self.assertTrue(scored[1][0])
        self.assertFalse(scored[-1][0])
        trace["original_reads"][0]["seq"] = 3
        scored, _ = evidence.proofset("yeast",trace)
        self.assertEqual(scored[-1][1], [{"read":"late","selection_event":11,"scope":actor}])
        trace["native_calls"][0]["arguments"]["method_choices"] = [method]
        self.assertFalse(evidence.proofset("yeast",trace)[0][1][0])
