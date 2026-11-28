"""Core evidence is keyed to visible scientific questions across spellings and turns."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from acceptance.harness_refactor.closing.test_closeout import Study, insufficient
from acceptance.v62a.interleavings_fixture import draft
from gx_core.errors import SchemaInvalid

QUESTION = "Which neighborhood comparisons are supported?"
TERMS = {"closing_terms":{"core_materials":[{"role":"local_pair"}]}}


class CoreQuestions(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def record(self, case, s, expected, reply):
        path = os.getenv("GX_CORE_CLOSE_TRACE")
        if not path:
            return
        file = Path(path)
        cases = json.loads(file.read_text()) if file.exists() else {}
        cases[case] = {"transport":"controlled native callbacks", "run_id":s.run_id,
                       "expected_intervention":expected, "actual_intervention":"outcome" in reply and
                            reply["outcome"] == "core_evidence_available",
                       "events":s.rows("SELECT event_id,kind FROM events WHERE kind='research.close.material_prompt'"),
                       "reply":{k:reply[k] for k in ("outcome","question_id","goal_version","event_id","materials","decision_ref")
                                if k in reply}, "external_jobs":s.rows("SELECT job_id FROM jobs")}
        file.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n")

    def test_exact_pi_text_matches_same_core_source_as_goal(self):
        for alias in (QUESTION, "goal", None):
            with self.subTest(alias=alias), Study(self.work/(alias or "omitted"), spec_updates=TERMS) as s:
                response = s.call(s.lead, "gx_decide_revision", insufficient(alias), "close-first")
                self.assertEqual(response["outcome"], "core_evidence_available")
                self.record("root-" + ("pi-words" if alias == QUESTION else (alias or "omitted")), s, True, response)
                core = s.brief(s.lead)["core_materials"][0]
                self.assertEqual((response["question_id"],core["ref"]), (core["question_id"],s.materials[0]))
                self.assertEqual(response["goal_version"], 1)
                self.assertIn("decision_ref",s.call(s.lead,"gx_decide_revision",insufficient(core["question_id"]),"close-again"))
                self.assertEqual(len(s.rows("SELECT 1 FROM events WHERE kind='research.close.material_prompt'")),1)

    def test_unknown_text_cannot_remove_the_root_check(self):
        with Study(self.work, spec_updates=TERMS) as s:
            with self.assertRaises(SchemaInvalid) as error:
                s.call(s.lead,"gx_decide_revision",insufficient("unregistered issue"),"misspelled")
            self.assertEqual(error.exception.details["reason"],"unknown_core_question")
            self.assertEqual(s.conn.execute("SELECT status FROM v6_goals").fetchone()[0],"OPEN")
            self.assertEqual(s.call(s.lead,"gx_decide_revision",insufficient(),"retry")["outcome"],"core_evidence_available")

    def test_same_text_for_two_intents_requires_visible_identity(self):
        with Study(self.work) as s:
            text = "Is the calibration traceable?"
            routes = s.call(s.lead,"gx_decide_revision",{"decision":draft(*[
                dict(op="open_intent", intent="local:"+alias, question=text) for alias in ("north","south")])},"open")
            ids = [routes["aliases"]["local:"+x] for x in ("north","south")]
            actor = s.next_actor("run")
            with self.assertRaises(SchemaInvalid) as error:
                s.call(actor,"gx_link_core_material",{"question":text,"ref":s.materials[0],"reason":"Calibration record."},"ambiguous")
            self.assertEqual(set(ids),{x["question_id"] for x in error.exception.details["options"]})
            nom = s.call(actor,"gx_link_core_material",{"question":ids[0],"ref":s.materials[0],"reason":"Northern record."},"exact")
            self.assertEqual(nom["question_id"],ids[0])
            with self.assertRaises(SchemaInvalid):
                s.call(actor,"gx_decide_revision",insufficient(text),"unspecified-issue")
            answer=s.call(actor,"gx_decide_revision",insufficient(ids[0]),"north-only")
            self.assertEqual(answer["materials"][0]["ref"],s.materials[0])
            self.assertEqual(answer["question_id"],ids[0])

    def test_revision_new_material_and_cold_restart_do_not_reset_run_notice(self):
        from acceptance.v621.test_goal_versions import GoalVersions
        with Study(self.work, spec_updates=TERMS) as s:
            first = s.call(s.lead,"gx_decide_revision",insufficient(),"first")
            GoalVersions().change_goal(s)
        with Study(self.work, reopen=True) as s:
            actor = s.next_actor("run")
            nom = s.call(actor, "gx_link_core_material", {"ref":s.materials[0],
                         "reason":"The new question also needs these comparisons."}, "new-nomination")
            self.assertEqual(nom["goal_version"],2)
            self.assertNotEqual(nom["question_id"],first["question_id"])
            self.assertIn("decision_ref",s.call(actor,"gx_decide_revision",insufficient(nom["question_id"]),"final"))
            self.assertEqual(len(s.rows("SELECT 1 FROM events WHERE kind='research.close.material_prompt'")),1)

    def test_hint_for_unknown_id_omits_a_siblings_unreleased_question(self):
        cfg = {"research_modules":["data_inventory@1"], "capabilities":["tl1a-coordinate-quality@1"],
               "plugins":["org.gx.protein@0.2.0"]}
        with Study(self.work, spec_updates=cfg) as s:
            def open_for(label):
                return s.bind(s.open(label,inputs=s.materials,caps=[])["task_id"])
            first, peer = open_for("first"), open_for("peer")
            secret = "Is the undisclosed collection window biased?"
            private = s.call(first,"gx_link_core_material",{"ref":s.materials[0], "question":secret,
                             "reason":"This question was raised inside the first commission."},"first")
            with self.assertRaises(SchemaInvalid) as rejected:
                s.call(peer,"gx_link_core_material",{"ref":s.materials[0],"question":private["question_id"],
                       "reason":"Use the previously observed scope."},"peer")
            self.assertNotIn(private["question_id"], str(rejected.exception.details["options"]))
            self.assertNotIn(secret, str(rejected.exception.details["options"]))

    def test_two_prepared_native_closes_receive_one_notice_then_a_fresh_close_commits(self):
        from unittest.mock import patch
        from gx_core.p0 import v6_decisions
        with Study(self.work, spec_updates=TERMS) as s:
            prepare = v6_decisions.prepare
            overlaps = []
            def interleave(*args, **kwargs):
                plan = prepare(*args, **kwargs)
                if kwargs["call_id"] == "early":
                    overlaps.append(s.call(s.lead,"gx_decide_revision",insufficient(),"concurrent"))
                return plan
            with patch.object(v6_decisions,"prepare",side_effect=interleave):
                original = s.call(s.lead,"gx_decide_revision",insufficient(),"early")
            self.assertEqual(overlaps, [original])
            self.record("concurrent-close", s, True, original)
            self.assertEqual(s.rows("SELECT status FROM v6_goals"), [{"status":"OPEN"}])
            self.assertEqual(s.call(s.lead,"gx_decide_revision",insufficient(),"early"), original)
            self.assertIn("decision_ref", s.call(s.lead,"gx_decide_revision",insufficient(),"after"))
            self.assertEqual(len(s.rows("SELECT 1 FROM events WHERE kind='research.close.material_prompt'")),1)

    def test_researcher_read_requires_released_report_citing_the_same_original(self):
        # Two files represent independent serializations of the identified paper.
        for publishes_source in (False, True):
            case = self.work / ("released" if publishes_source else "private")
            case.mkdir()
            inputs = []
            for role, spacing in (("original", ""), ("scan", "\n")):
                doc = case / (role + ".xml")
                doc.write_text("<article><front><article-meta><article-id pub-id-type='doi'>10.1234/local</article-id>"
                    "</article-meta></front><body>" + spacing + "<sec><title>Methods</title><p>Read both instruments "
                    "at the same time.</p></sec></body></article>")
                inputs.append({"path":str(doc),"role":role})
            cfg = {"research_modules":["data_inventory@1"], "capabilities":["tl1a-coordinate-quality@1"],
                   "plugins":["org.gx.protein@0.2.0"], "input_files":inputs}
            with self.subTest(released=publishes_source), Study(case,spec_updates=cfg) as s:
                original, copy = s.materials
                def body(actor, ref):
                    doc = s.call(actor, "gx_read_source", {"source_ref":ref}, "list-" + actor.scope)
                    return next(seg["segment_id"] for seg in doc["segments"] if seg["label"].endswith("/p"))
                target = body(s.lead, original)
                s.call(s.lead, "gx_link_core_material", {"ref":original,"segment_id":target,
                       "reason":"The comparison depends on the readout method."}, "designate")
                opened = s.open("methods", inputs=[copy], caps=[])
                worker = s.bind(opened["task_id"])
                receipt = s.call(worker,"gx_read_source",{"source_ref":copy, "segment_id":body(worker,copy)}, "read")
                self.assertTrue(receipt["text_sha256"])
                s.call(s.lead,"gx_decide_revision",{"decision":draft()},"await")
                if publishes_source:
                    s.report(worker, copy, text="The readout times were paired.")
                else:
                    s.call(worker,"gx_submit_module_report",{"status":"PARTIAL","findings":[],"artifact_refs":[],
                           "gaps":["An alternate archive remains unassessed."],"next_steps":[],
                           "operation_key":"report"}, "report")
                actor = s.next_actor("run")
                answer = s.call(actor, "gx_decide_revision", insufficient(), "close")
                self.record("child-" + ("released-original" if publishes_source else "undisclosed"),
                            s, not publishes_source, answer)
                if publishes_source:
                    self.assertIn("decision_ref", answer)
                else:
                    self.assertEqual(answer["materials"][0]["ref"], original)

if __name__ == "__main__":
    unittest.main()
