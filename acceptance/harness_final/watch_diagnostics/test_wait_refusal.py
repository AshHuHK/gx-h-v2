"""Exercise the first archive Run's refused wait shapes through the ordinary native Goal callback."""
from pathlib import Path
import tempfile
import unittest

from acceptance.harness_refactor.modules.research_study import ResearchStudy
from gx_core.errors import SchemaInvalid
from gx_core.p0 import v6_native


class WaitRefusal(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.study = self.enterContext(ResearchStudy(folder,spec_updates={"capabilities":[],"research_modules":[]}))
        self.actor = self.study.lead

    def call(self,key,**fields):
        body = {"rationale":"Reassess the scope of the archived readings if their methods arrive.","ops":[],**fields}
        return self.study.call(self.actor,v6_native.DECIDE,{"decision":body},key)

    def test_conflicting_mode_offers_an_ordinary_wait_without_choosing_it(self):
        with self.assertRaises(SchemaInvalid) as conflict:
            self.call("empty-amend",mode="amend")
        self.assertEqual(conflict.exception.details["field"],"mode")
        self.assertIn("mode=amend",conflict.exception.message)
        self.assertIn("reconsider_when",conflict.exception.message)
        self.assertEqual(self.study.rows("SELECT 1 FROM v6_decisions"),[])
        accepted = self.call("ordinary-wait")
        self.assertEqual({w["event"] for w in accepted["reconsider_when"]},{"evidence_added","budget_changed"})
        self.assertIsNone(self.study.head())
        self.assertEqual(self.study.rows("SELECT job_id FROM jobs"),[])

    def test_text_description_and_unregistered_watch_offer_the_actual_schema(self):
        s = self.study
        text = "The original measurement returns with its units and method."
        cases = (([text],"reconsider_when[0]"),([{"description":text}],"reconsider_when[0].event"),
                 ([{"event":"research.catalog.updated","subject_ref":s.run_id}],"reconsider_when[0].event"))
        tool = next(x for x in v6_native.tool_specs([],s.brief(self.actor),conn=s.conn,run_id=s.run_id)
                    if x["name"] == v6_native.DECIDE)
        watch = tool["inputSchema"]["properties"]["decision"]["properties"]["reconsider_when"]
        events = watch["items"]["properties"]["event"]["enum"]
        for i,(condition,field) in enumerate(cases):
            with self.subTest(condition=i):
                args = dict(mode="resolve",reconsider_when=condition,disposition="MAINTAIN",affected_scope="archive")
                # Preserve the original comparison choice; fixing one field cannot settle another.
                for attempt in range(2):
                    with self.assertRaises(SchemaInvalid) as refused:
                        self.call("form-"+str(i),**args)
                    error = refused.exception
                    self.assertEqual(error.details["field"],field)
                    self.assertEqual(error.details.get("allowed_events"),events)
                    self.assertIn("rationale",error.message)
                    self.assertIn("registered comparison",error.message)
                    self.assertIn('{"event":"evidence_added","subject_ref":"run"}',error.message)
                    if attempt == 0:
                        first = error.to_payload()
                    else:
                        self.assertEqual(first,error.to_payload())
        self.assertEqual(s.rows("SELECT 1 FROM v6_decisions"),[])
        self.assertEqual(len(s.rows("SELECT 1 FROM events WHERE kind='v624.decision.rejected'")),3)
        explicit = [{"event":"evidence_added","subject_ref":s.run_id}]
        with self.assertRaises(SchemaInvalid) as scientific_choice:
            self.call("select-comparison",mode="resolve",reconsider_when=explicit,
                      disposition="MAINTAIN",affected_scope="archive")
        self.assertEqual(scientific_choice.exception.details["reason"],"comparison_action_ambiguous")
        accepted = self.call("choose-wait",mode="resolve",reconsider_when=explicit)
        self.assertEqual(accepted["reconsider_when"],explicit)
        self.assertIsNone(s.head())

    def test_module_retains_its_original_validation_surface(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        study = self.enterContext(ResearchStudy(folder,spec_updates={
            "capabilities":[],"research_modules":["ad_hoc@1"],"research_effort":"deep"}))
        grant = study.open_args("record",inputs=study.materials,caps=[])
        grant.update(module_id="ad_hoc",expected_delivery="Review the station archive.")
        module = study.bind(study.call(study.lead,"gx_open_module",grant,"record")["task_id"])
        with self.assertRaises(SchemaInvalid) as refused:
            study.call(module,v6_native.DECIDE,{"decision":{
                "rationale":"Wait for the archive.","ops":[],"reconsider_when":["a source arrives"]}},"bad-watch")
        self.assertNotIn("allowed_events",refused.exception.details)
        self.assertNotIn("default wakes",refused.exception.message)


if __name__ == "__main__":
    unittest.main()
