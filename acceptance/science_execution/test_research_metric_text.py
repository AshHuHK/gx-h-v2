"""Render an adopted decision's own end points on readback, including mixed valid and missing tags."""
import json
import sqlite3
import unittest
from unittest.mock import patch

from gx_core.errors import SchemaInvalid
from gx_core.p0 import research_report, v621_metrics, v623_endpoints


class MetricText(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE v6_decisions(run_id, decision_ref, thread_id, task_id, lease_id, receipt_json);
            CREATE TABLE events(run_id, event_id, kind, actor, payload_json);
        """)
        self.metric = {"schema":v621_metrics.SCHEMA, "metric_ref":"metric_alpha", "metric_id":"angle",
                       "artifact_ref":"job-output", "scope":{}, "unit":"deg", "display":"0.6127 deg"}
        lookup = patch.object(v623_endpoints, "registered", return_value=[self.metric])
        self.addCleanup(lookup.stop)
        lookup.start()
        delivery = {"task_id":"t", "lease_id":"l", "turn_id":"u", "receipt":"page",
                    "endpoints":[{"source_ref":"report", "metric_ref":"metric_alpha"}]}
        self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                        ("r", 30, "v623.endpoints.received", "thread", json.dumps(delivery)))
        for ref, used in (("decision", ["metric_alpha"]), ("unused", [])):
            proof = {"source_ref":"report", "event_id":30, "turn_id":"u", "receipt":"page"}
            adopted = {"event_id":31, "endpoint_use":{"used_metric_refs":used,"receipts":{"metric_alpha":proof}}}
            self.db.execute("INSERT INTO v6_decisions VALUES (?,?,?,?,?,?)",
                            ("r", ref, "thread", "t", "l", json.dumps(adopted)))

    def render(self, ref, text):
        return research_report.render_decision(self.db, "r", {}, ref, text)

    def test_display_preserves_accepted_prose_and_resolves_only_the_used_row(self):
        accepted = "For 101–120 at 2RE9: {{metric:job-output:angle|unit=deg}}."
        self.assertEqual(self.render("decision", accepted), "For 101–120 at 2RE9: 0.6127 deg.")
        self.assertIn("Unresolved metric", self.render("unused", accepted))
        self.assertNotIn("0.6127", self.render("unused", accepted))
        self.assertIn("{{metric:", accepted)
        self.assertRaises(SchemaInvalid, v621_metrics._render, accepted, [self.metric])

    def test_mismatched_receipt_or_artifact_and_ambiguous_or_malformed_templates(self):
        mixed = "A {{metric:job-output:angle}}, B {{metric:other-output:angle}}, C {{metric:angle|unit=Å}}."
        result = self.render("decision", mixed)
        self.assertEqual(result.count("0.6127 deg"), 1)
        self.assertEqual(result.count("Unresolved metric"), 2)
        self.assertIn("Malformed", self.render("decision", "{{metric:unfinished"))
        other = {**self.metric, "artifact_ref":"other-output", "metric_ref":"metric_other"}
        self.assertIn("Unresolved metric", v621_metrics.render_accepted_text("{{metric:angle}}", [self.metric,other]))
        self.db.execute("UPDATE events SET actor='different-thread'")
        with self.assertRaisesRegex(ValueError, "delivery"):
            self.render("decision", mixed)


if __name__ == "__main__":
    unittest.main()
