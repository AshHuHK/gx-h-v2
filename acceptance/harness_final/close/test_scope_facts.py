"""A scoped reference may travel without its owner's private rationale."""
from pathlib import Path
import tempfile
import unittest

from acceptance.harness_refactor.closing.test_closeout import Study, insufficient
from acceptance.v622.test_close import close_decision


class ScopedFacts(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))

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
