"""R4. Verify the formal measurement and qualitative interpretation at each publication boundary."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from acceptance.v621.test_verified_metrics import METHOD, SOURCE_METADATA, SCOPE
from acceptance.v62a.interleavings_fixture import Scenario, draft
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import SchemaInvalid, ScopeDenied
from gx_core.p0 import v6_goal, v6_native, v62_reports


class ReportEvidence(unittest.TestCase):
    def measured_output(self, directory, **config):
        source = json.loads(SOURCE_METADATA.read_bytes())
        s = self.enterContext(Scenario(directory/"run", spec_updates={
            "research_effort":"deep", "research_modules":["data_inventory@2"],
            "plugins":["org.gx.protein@0.3.0"], "capabilities":[METHOD], **config}))
        assets = ArtifactService(s.conn)
        original = s.materials[0]
        origin = v6_goal.record_observation(s.conn, s.run_id, original, source="public-archive",
                     acquisition=source)
        mod = s.call(s.lead, "gx_open_module", {**s.open_args("inventory", inputs=[original], caps=[METHOD]),
                                               "module_version":"2"}, "commission")
        worker = s.bind(mod["task_id"])
        s.call(s.lead, v6_native.DECIDE, {"decision":draft()}, "wait")
        scheduled = s.submit_node(worker,"mask",input_ref=original,capability=METHOD,
                                  parameters={"mask":"ordered_backbone"})
        node = s.settle(scheduled["aliases"]["local:mask"])
        output = node["result_ref"]
        raw = assets.read_json(output)
        self.assertEqual((raw["selected_residues"], raw["observed_union_residues"]),(49,164))
        worker = s.next_actor(mod["scope"])
        return s, assets, origin, node, output, worker

    def delivered_report(self, s, receipt):
        lead = s.next_actor("run")
        brief = v6_native.brief(s.roots._binding(lead.thread))
        if not any(r["report_ref"] == receipt["report_ref"] for r in brief["module_reports"]):
            s.call(lead,v6_native.DECIDE, {"decision":draft()}, "next-report")
            lead = s.next_actor("run")
            brief = v6_native.brief(s.roots._binding(lead.thread))
        delivered, = [r for r in brief["module_reports"] if r["report_ref"] == receipt["report_ref"]]
        return lead, brief, delivered

    def test_real_measurement_and_conflicting_qualitative_estimates(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        s, assets, origin, node, output, worker = self.measured_output(directory)
        notes = "三聚體的第一個結構域可能涵蓋九成。 The analyst estimates ninety percent."
        finding = {"text":notes+" Archive reading {{metric:coverage}}.", "source_refs":[output]}
        receipt = s.report(worker, output, findings=[finding])
        report = assets.read_json(receipt["report_ref"])
        lead, brief, delivered = self.delivered_report(s, receipt)
        claim = dict(statement=notes+" Observed {{metric:coverage}}.", level="open", scope=SCOPE,
                     criteria="The deposited archive selection.", evidence_refs=[receipt["report_ref"]],
                     limitations="This observation concerns the deposited coordinates.",
                     rationale="The mask determines the measured positions.")
        staged = s.call(lead,"gx_stage_claim",claim,"propose")
        final = s.call(lead,v6_native.DECIDE, {"decision":draft(evidence=[receipt["report_ref"],staged["claim_ref"]])},
                       "adopt")["claim_revisions"][0]
        from acceptance.v6.export import capture
        from acceptance.v6.project_window import Capture
        capture(s.state/"gx.db", s.run_id, directory/"export")
        exported = Capture(directory/"export").claim(final["hypothesis_id"])
        # An index can fit where the full measurement does not. No metric was delivered by this projection.
        summaries, index = v62_reports.report_views(s.conn, s.run_id, per_report_bytes=180, total_bytes=500)
        self.assertEqual(index[0]["report_ref"],receipt["report_ref"])
        self.assertFalse(any(r.get("verified_metrics") or r.get("metrics") for r in summaries))
        trace = {"run_id":s.run_id, "source":origin,"job_id":node["job_id"],"output_ref":output,
                 "output_sha256":assets.describe(output)["sha256"],"metric_from_raw":[49,164],
                 "report_ref":receipt["report_ref"],"report_sha256":receipt["report_sha256"],
                 "report":report, "delivered":delivered,"lead_brief_sha256":ids.sha256_of_obj(brief),
                 "claim":final, "exported":exported, "index_only_preview":summaries,"index":index,
                 "external_paid_calls":0}
        if file := os.getenv("GX_R4_TRACE"):
            Path(file).write_text(json.dumps(trace,indent=2,ensure_ascii=False)+"\n")
        self.assertIn("verified_metrics",report)
        reading, = report["verified_metrics"]
        self.assertEqual((reading["numerator"],reading["denominator"],reading["display"]),
                         (49,164,"49/164 (0.299)"))
        self.assertEqual(report["qualitative_notes"]["status"],"UNVERIFIED")
        self.assertIn(notes,report["qualitative_notes"]["findings"][0]["text"])
        self.assertEqual(delivered["verified_metrics"],[reading])
        for item in (report,delivered):
            self.assertNotIn("ninety percent",json.dumps(item["findings"],ensure_ascii=False))
            self.assertIn(reading["display"],item["findings"][0]["text"])
        for item in (final,exported):
            self.assertEqual(item["verified_metrics"],[reading])
            self.assertIn(reading["display"],item["statement"])
            self.assertNotIn("九成",item["statement"])
            self.assertEqual(item["qualitative_notes"]["status"],"UNVERIFIED")
        self.assertEqual(len(s.rows("SELECT job_id FROM jobs")),1)

    def test_index_only_requires_explicit_read_and_scoped_metric_selector(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        s, assets, origin, node, output, worker = self.measured_output(directory, module_policy={"report_bytes":512})
        notes = [{"text": "三聚體及第一個結構域。 IL-2 and pH 7.2. The analyst estimates ninety percent.",
                  "source_refs": [output]}]
        selector = {"artifact_ref": output, "metric_id": "coverage"}
        with self.assertRaises(ScopeDenied):
            s.report(worker, output, key="wrong-scope", findings=[], qualitative_notes=notes,
                     metric_refs=[{**selector, "artifact_ref": s.materials[-1]}])
        with self.assertRaises(SchemaInvalid):
            s.report(worker, output, key="unknown-field", findings=[], qualitative_notes=notes,
                     metric_refs=[{**selector, "metric_id":"not-a-metric"}])
        receipt = s.report(worker, output, key="selected", findings=[], metric_refs=[selector], qualitative_notes=notes)
        ref = receipt["report_ref"]
        lead, brief, delivered = self.delivered_report(s, receipt)
        self.assertTrue(delivered["truncated"])
        self.assertFalse(delivered.get("verified_metrics") or delivered.get("metrics"))
        self.assertNotIn("49/164", json.dumps(delivered))
        self.assertTrue(any(i["report_ref"] == ref for i in brief["module_report_index"]))
        report = s.call(lead, "gx_read_artifact", {"ref":ref}, "read-full")
        read = s.rows("SELECT call_id,thread_id,turn_id,request_sha FROM p0_root_calls WHERE call_id='read-full'")
        self.assertEqual(len(read),1)
        reading, = report["verified_metrics"]
        self.assertEqual((reading["artifact_ref"],reading["metric_id"],reading["display"]),
                         (output,"coverage","49/164 (0.299)"))
        self.assertEqual(report["qualitative_notes"]["findings"],notes)
        self.assertEqual(report["qualitative_notes"]["status"],"UNVERIFIED")
        self.assertEqual(report["findings"][0]["metric_refs"],[reading["metric_ref"]])
        self.assertEqual(len(s.rows("SELECT job_id FROM jobs")),1)
        if file := os.getenv("GX_R4_TRACE"):
            Path(file).with_name("R4-index-read.json").write_text(json.dumps({
                "run_id":s.run_id, "source":origin, "job_id":node["job_id"],
                "output_ref":output, "report_ref":ref, "report_sha256":assets.describe(ref)["sha256"],
                "actual_lead_brief_sha256":ids.sha256_of_obj(brief), "delivered_summary":delivered,
                "delivered_index":brief["module_report_index"], "read_receipt":read[0], "read_action":s.actions[-1],
                "verified_metrics_read":report["verified_metrics"], "unverified_notes_read":report["qualitative_notes"],
                "external_paid_calls":0}, indent=2,ensure_ascii=False)+"\n")


if __name__ == "__main__":
    unittest.main()
