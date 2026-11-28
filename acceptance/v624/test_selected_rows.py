"""An accepted short citation is traceable to the numeric row that actually reached its Lead."""
import json
import unittest
from unittest.mock import patch

from acceptance.v622.test_close import close_decision
from acceptance.v623 import test_close_metrics as scope_fixtures
from gx_core.domain import ids
from gx_core.errors import SchemaInvalid, ScopeDenied
from gx_core.p0 import v623_endpoint_use


class SelectedRows(unittest.TestCase):
    def test_delivered_module_row_becomes_cited_source_and_replays_after_closure(self):
        fixture = scope_fixtures.EndpointClose()
        self.addCleanup(fixture.doCleanups)
        _, (study, assets, _, node, output, worker) = fixture.measured()
        report_ref, report, lead, packet = fixture.report(study, assets, output, worker)
        metric, = report["verified_metrics"]
        projected, = [r for r in packet["packet"]["module_reports"] if r["report_ref"] == report_ref]
        row, = projected["endpoint_rows"]
        self.assertEqual(row["value"], metric["display"])

        short = {"rationale": "Record the covered residues measured by the commissioned analyst.",
                 "metric_refs": [row["key"]], "ops": close_decision()["ops"]}
        for idx, wrong in enumerate((row["value"], row["id"], output + "#" + row["key"])):
            with self.assertRaises(ScopeDenied) as rejected:
                study.call(lead, "gx_decide_revision", {"decision": {**short, "metric_refs": [wrong]}}, f"bad-{idx}")
            self.assertEqual(rejected.exception.details["reason"], "endpoint_not_received")

        accepted = study.call(lead, "gx_decide_revision", {"decision": short}, "selected")
        use = accepted["endpoint_use"]
        self.assertEqual(use["used_metric_refs"], [metric["metric_ref"]])
        proof = use["receipts"][metric["metric_ref"]]
        self.assertEqual((proof["source_ref"], proof["thread_id"], proof["task_id"]),
                         (report_ref, lead.thread, lead.task_id))
        self.assertEqual(accepted["evidence_refs"], [report_ref])
        stored, = study.rows("SELECT request_json FROM v6_decisions WHERE decision_ref=?", accepted["decision_ref"])
        request = json.loads(stored["request_json"])
        self.assertEqual(request["original_decision"], short)
        self.assertEqual(request["decision"], assets.read_json(accepted["decision_ref"]))
        self.assertEqual(accepted["draft_sha256"], ids.sha256_of_obj(short))
        self.assertEqual(accepted["normalized_sha256"], ids.sha256_of_obj(request["decision"]))
        submission, = study.rows("SELECT content_sha256 FROM submissions WHERE submission_id=?", accepted["submission_id"])
        self.assertEqual(submission["content_sha256"], accepted["normalized_sha256"])
        study.advance()
        self.assertEqual(study.call(lead, "gx_decide_revision", {"decision": short}, "selected"), accepted)
        self.assertEqual(study.rows("SELECT job_id FROM jobs"), [{"job_id": node["job_id"]}])

    def test_same_short_key_from_two_sources_needs_exact_delivered_source(self):
        aliases = {"first#abcd": "metric_left", "second#abcd": "metric_right"}
        by_source = {"first": {"metric_left"}, "second": {"metric_right"}}
        proofs = {(source, ref): {"source_ref": source} for source, refs in by_source.items() for ref in refs}
        kwargs = (None, {"task_id": "t"}, {"run_id": "r", "lease_id": "l", "thread_id": "actor"}, "turn")
        with patch("gx_core.p0.v623_exposure.received", return_value=proofs):
            with self.assertRaises(SchemaInvalid) as ambiguous:
                v623_endpoint_use.resolve_delivered(*kwargs, {"metric_refs": ["abcd"], "evidence_refs": []},
                                                    (by_source, aliases))
            self.assertEqual(ambiguous.exception.details, {"reason": "endpoint_ambiguous",
                             "field": "metric_refs", "options": sorted(aliases)})
            selected = {"metric_refs": ["second#abcd"], "evidence_refs": []}
            v623_endpoint_use.resolve_delivered(*kwargs, selected, (by_source, aliases))
            self.assertEqual(selected, {"metric_refs": ["metric_right"], "evidence_refs": ["second"]})
