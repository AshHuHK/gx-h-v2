"""A scoped reference may travel without its owner's private rationale."""
from pathlib import Path
import tempfile
import unittest

from acceptance.harness_refactor.closing.test_closeout import Study, insufficient
from acceptance.v622.test_close import close_decision


class ScopedFacts(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_lead_must_cite_the_actual_handoff_to_grade_a_researchers_located_method(self):
        from acceptance.v62a.interleavings_fixture import draft
        from gx_core.artifacts import ArtifactService
        from gx_core.errors import SchemaInvalid
        doc = self.work / "methods.xml"
        doc.write_text("<article><body><sec id='methods'><title>Methods</title>"
                       "<p>The readouts were recorded at the same time.</p></sec></body></article>")
        cfg = {"research_modules":["data_inventory@1"], "capabilities":["tl1a-coordinate-quality@1"],
               "plugins":["org.gx.protein@0.2.0"], "input_files":[{"role":"methods","path":str(doc)}]}
        with Study(self.work, spec_updates=cfg) as s:
            source = s.materials[0]
            actor = s.bind(s.open("method",inputs=[source],caps=[])["task_id"])
            doc = s.call(actor,"gx_read_source",{"source_ref":source},"list")
            segment = next(seg["segment_id"] for seg in doc["segments"] if seg["label"].endswith("methods/p"))
            read = s.call(actor,"gx_read_source",{"source_ref":source,"segment_id":segment},"inspect")
            s.call(s.lead,"gx_decide_revision",{"decision":draft()},"await")
            report = s.report(actor,source,text="The paper describes a paired readout.")
            lead = s.next_actor("run")
            claim = {"statement":"A paired readout is described.","scope":"The report's one study",
                     "criteria":"Method paragraph verified.","limitations":"One protocol.","level":"insufficient",
                     "rationale":"The investigator inspected the source.",
                     "evidence_check":{"grade":"body_method_checked","receipt_id":read["receipt_id"],"source_ref":source}}
            with self.assertRaises(SchemaInvalid):
                s.call(lead,"gx_stage_claim",{**claim,"evidence_refs":[source]},"uncited")
            staged = s.call(lead,"gx_stage_claim",{**claim,"evidence_refs":[source,report["report_ref"]]},"cited")
            grade = ArtifactService(s.conn).read_json(staged["claim_ref"])["evidence_grade_receipt"]
            self.assertEqual((grade["reader_scope"],grade["text_sha256"],grade["segment_id"]),
                             (actor.scope,read["text_sha256"],segment))

    def test_only_delegated_visible_reference_may_reach_child_delivery(self):
        cfg = {"research_modules":["data_inventory@1"], "capabilities":["tl1a-coordinate-quality@1"],
               "plugins":["org.gx.protein@0.2.0"]}
        with Study(self.work, spec_updates=cfg) as s:
            private = "The Lead is comparing results against a second unpublished channel."
            s.call(s.lead,"gx_link_core_material",{"ref":s.materials[0],"reason":private}, "link")
            member = s.open("window",inputs=s.materials,caps=[])
            worker = s.bind(member["task_id"])
            response = s.call(worker,"gx_submit_module_report",{"operation_key":"deliver","status":"PARTIAL",
                         "findings":[],"artifact_refs":[],"gaps":["The check was inconclusive."],"next_steps":[]},
                         "deliver")
            cue = response["delivery_advisory"]
            self.assertEqual([x["ref"] for x in cue["materials"]],s.materials)
            self.assertNotIn(private,str(response))

    def test_many_read_materials_cannot_hide_one_named_original(self):
        files = []
        for i in range(5):
            path = self.work / f"site-{i}.txt"
            path.write_text(f"Observed at site {i}.")
            files.append({"role":f"site_{i}","path":str(path)})
        cfg = {"input_files":files,"closing_terms":{"core_materials":[{"role":"site_4"}]}}
        with Study(self.work,spec_updates=cfg) as s:
            for i,ref in enumerate(s.materials[:-1]):
                s.call(s.lead,"gx_read_artifact",{"ref":ref},f"read-{i}")
            cue = s.call(s.lead,"gx_decide_revision",insufficient(),"close")
            self.assertEqual([x["ref"] for x in cue["materials"]], [s.materials[-1]])

    def test_resource_and_scientific_closures_keep_codex_reason(self):
        scenarios = [
            ("TIME_LIMIT","The collection window has ended."),
            ("BUDGET","Additional work exceeds the resource agreement."),
            ("OPERATOR","The operator ended this investigation."),
            ("SCIENTIFIC_COMPLETION","The observed direction decisively contradicts the premise."),
            ("SCIENTIFIC_COMPLETION","The interval spans zero and its direction remains uncertain."),
            ("SCIENTIFIC_COMPLETION","The comparison is invalid because the control failed."),
        ]
        for i,(cause, reason) in enumerate(scenarios):
            with self.subTest(cause=cause,reason=reason), Study(self.work/str(i), spec_updates={
                    "closing_terms":{"core_materials":[{"role":"local_pair"}]}}) as s:
                d = {**close_decision(), "stop_reason":cause, "rationale":reason}
                d["ops"][0]["reason"] = reason
                receipt = s.call(s.lead,"gx_decide_revision",{"decision":d},"close")
                self.assertEqual(receipt["resolved_ops"][0]["spec"]["reason"],reason)
                self.assertFalse(s.rows("SELECT 1 FROM events WHERE kind='research.close.material_prompt'"))
