"""Normal research CLI admission and native callbacks, with only the Codex transport scripted."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from acceptance.v622.test_close import SurveyedStudy, close_decision
from acceptance.v62a.interleavings_fixture import PRODUCT
from types import SimpleNamespace
from gx_core.errors import GxError
from acceptance.v62a.interleavings_fixture import draft


class Study(SurveyedStudy):
    def _start_goal(self):
        config = self.directory / "config.json"
        file = self.directory / "local.csv"
        file.write_text("date,paired_error\n2026-01-01,2.1\n")
        limits = {"job_submissions":20, "worker_tasks":48, "cpu_seconds":1200, "api_calls":48,
                  "tokens":120000, "gpu_jobs":0, "gpu_minutes":0, "wall_hours":2}
        config.write_text(json.dumps({
            "workspace_budget":limits, "run_budget":limits, "turn_token_budget":1000,
            "research_effort": "deep", "research_modules": [],
            "capabilities": [], "evidence_policy": "gx.evidence/6.2.4",
            "input_files": [{"path": str(file), "role": "local_pair"}], **self.spec_updates}))
        result = subprocess.run([sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "research", "--state",
                        str(self.state), "--goal", "Which neighborhood comparisons are supported?",
                        "--run-key", "neighborhood-study", "--goal-config", str(config), "--timeout", "0.001"],
                       env={**os.environ, "GX_CODEX_OFFLINE": "1"}, capture_output=True, text=True)
        if result.returncode and (not result.stdout.strip().startswith("{") or
                                  json.loads(result.stdout).get("driver") != "time_limit"):
            raise RuntimeError(result.stderr + result.stdout)


def insufficient(slot=None):
    decision = dict(close_decision(), stop_reason="INSUFFICIENT_DATA")
    if slot:
        decision["ops"][0]["core_question"] = slot
    return {"decision": decision}


class Closeout(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_unread_pi_designated_pair_prompted_once_even_after_restart(self):
        with Study(self.work, spec_updates={"closing_terms": {
                "core_materials": [{"question": "goal", "role": "local_pair"}]}}) as s:
            actor = s.lead
            self.assertEqual(s.brief(actor)["core_materials"][0]["ref"],s.materials[0])
            wire = s.roots._callback(SimpleNamespace(message={"method":"item/tool/call","params":{
                "threadId":actor.thread,"turnId":actor.turn,"callId":"closeA","tool":"gx_decide_revision",
                "arguments":insufficient(),"namespace":"gx"}}))
            self.assertTrue(wire["success"])
            answer = json.loads(wire["contentItems"][0]["text"])
            self.assertEqual(answer["outcome"], "core_evidence_available")
            self.assertEqual(answer["materials"][0]["state"], "acquired_unread")
            self.assertEqual(answer["materials"][0]["ref"], s.materials[0])
            self.assertEqual(s.call(actor, "gx_decide_revision", insufficient(), "closeA"), answer)
            self.assertEqual(s.conn.execute("SELECT status FROM v6_goals").fetchone()[0], "OPEN")
        with Study(self.work, reopen=True) as s:
            s.roots.active_turns[actor.thread] = actor.turn
            self.assertIn("decision_ref", s.call(actor, "gx_decide_revision", insufficient(), "closeB"))
            self.assertEqual(len(s.rows("SELECT 1 FROM events WHERE kind='research.close.material_prompt'")), 1)

    def test_read_material_and_other_question_do_not_prompt(self):
        for name, read in (("other", False), ("read", True)):
            with self.subTest(name=name), Study(self.work/name, spec_updates={
                    "closing_terms":{"core_materials":[{"question": "goal" if read else "elsewhere",
                                                       "role":"local_pair"}]}}) as s:
                if read:
                    s.call(s.lead, "gx_read_artifact", {"ref":s.materials[0]}, "read")
                self.assertIn("decision_ref", s.call(s.lead, "gx_decide_revision", insufficient(), "close"))
                self.assertFalse(s.rows("SELECT 1 FROM events WHERE kind='research.close.material_prompt'"))

    def test_scientific_completion_does_not_prompt_and_no_signature_is_inferred(self):
        from gx_core.context import ENV_SUBJECT
        from unittest.mock import patch
        terms = {"signoff":"PI must endorse the released comparison",
                 "signer":os.environ.get(ENV_SUBJECT) or "gx-p0-operator",
                 "core_materials":[{"question":"goal","role":"local_pair"}]}
        with Study(self.work,spec_updates={"closing_terms":terms}) as s:
            receipt = s.call(s.lead, "gx_decide_revision", {"decision":close_decision()}, "close")
            self.assertTrue(receipt["decision_ref"])
            self.assertFalse(s.rows("SELECT 1 FROM events WHERE kind='research.close.material_prompt'"))
            self.assertFalse(s.rows("SELECT 1 FROM events WHERE kind='research.close.pi_signed'"))

            from gx_core.p0 import operator, research_closeout, research_report
            from gx_core.errors import ScopeDenied
            s.advance()
            signer = operator.local_context(s.conn)
            def sign(ref):
                return operator.submit(signer, s.run_id, {"kind":"sign_off", "decision_ref":ref})["result"]
            self.assertEqual(research_closeout.signoff_status(s.conn,s.run_id)["status"],"AWAITING_PI")
            with self.assertRaises(ScopeDenied):
                sign("different decision")
            with patch.dict(os.environ,{ENV_SUBJECT:"independent-reader"}), self.assertRaises(ScopeDenied):
                operator.submit(operator.local_context(s.conn),s.run_id,
                                {"kind":"sign_off","decision_ref":receipt["decision_ref"]})
            signed = sign(receipt["decision_ref"])
            self.assertEqual(signed["status"], "SIGNED")
            self.assertEqual(signed, sign(receipt["decision_ref"]))
            self.assertEqual(len(s.rows("SELECT 1 FROM events WHERE kind='research.close.pi_signed'")),1)
            report = research_report.export(s.conn,s.run_id,self.work,{"action":"test export"})
            self.assertEqual(json.loads(Path(report["result"]).read_text())["pi_signoff"],signed)
            self.assertIn(signer.principal.subject, Path(report["report"]).read_text())
            rid, state = s.run_id, s.state
        via_cli = subprocess.run([sys.executable,"-B",str(PRODUCT/"bin/gx-p0"),"operator","sign-off",
            "--state",str(state),"--run-id",rid,"--decision-ref",receipt["decision_ref"]],
            capture_output=True,text=True)
        self.assertEqual(via_cli.returncode,0,via_cli.stderr)
        self.assertIn(signed["decision_ref"], via_cli.stdout)

    def test_commissioned_local_pair_report_is_available_to_lead_at_close(self):
        terms = {"research_modules":["data_inventory@1"], "capabilities":["tl1a-coordinate-quality@1"], "plugins":["org.gx.protein@0.2.0"]}
        with Study(self.work,spec_updates=terms) as s:
            args = s.open_args("paired", inputs=s.materials, caps=[])
            args["expected_delivery"] = "Compare the local co-located instruments and report the observed difference."
            opened = s.call(s.lead,"gx_open_module",args,"commission")
            worker = s.bind(opened["task_id"])
            s.call(worker,"gx_link_core_material",{"ref":opened["module_instance_id"], "question":"local controls",
                "reason":"The paired observation tests the central missing comparison."},"nomination")
            s.call(worker, "gx_read_artifact", {"ref": s.materials[0], "operation_key":"open"}, "open-local")
            s.call(s.lead,"gx_decide_revision",{"decision":draft()},"wait")
            report = s.report(worker,s.materials[0],text="The local instrument readings were paired.",
                              gaps=["One neighborhood remains unmeasured."])
            lead = s.next_actor("run")
            designated = s.brief(lead)["core_materials"][0]
            self.assertEqual(designated["ref"],report["report_ref"])
            request = insufficient(designated["question"])
            reply = s.call(lead,"gx_decide_revision",request,"close")
            self.assertEqual([(r["ref"],r["state"]) for r in reply["materials"]],
                             [(report["report_ref"],"report_delivered_unread")])
            s.call(lead,"gx_read_artifact",{"ref":report["report_ref"]},"inspect")
            request["decision"]["ops"][0]["reason"] = "Local paired readings are available for one location only."
            request["decision"]["evidence_refs"] = [report["report_ref"]]
            receipt = s.call(lead,"gx_decide_revision",request,"final")
            s.advance()
            self.assertEqual(s.rows("SELECT status FROM v6_goals"),[{"status":"CLOSED"}])
            self.assertEqual(len(s.rows("SELECT 1 FROM events WHERE kind='research.close.material_prompt'")),1)
            if out := os.getenv("GX_CLOSEOUT_RECEIPT"):
                Path(out).write_text(json.dumps({"run_id":s.run_id,"transport":"scripted Codex, real native callbacks",
                    "module":opened["module_instance_id"],"report":report["report_ref"],"fact":reply,
                    "closed_decision":receipt["decision_ref"],"events":s.rows(
                    "SELECT event_id,kind,subject_id FROM events WHERE kind IN ('research.close.material_linked',"
                    "'research.close.material_prompt','v6.run.closed','v6.decision.committed')")},indent=2)+"\n")

    def test_objective_grades_require_body_or_actual_model_and_validation(self):
        paper = self.work/"methods.xml"
        paper.write_text("<article><front><abstract><p>Outdoor co-location was studied.</p></abstract></front>"
            "<body><sec id='methods'><title>Methods</title><p>We paired readings at the same time.</p></sec>"
            "<sec id='results'><title>Results</title><p>One comparison remains inconclusive.</p></sec></body></article>")
        with Study(self.work,spec_updates={"input_files":[{"path":str(paper),"role":"local_paper"}]}) as s:
            ref = s.materials[0]
            claim = {"statement":"The paper describes paired monitoring.", "scope":"This paper", "criteria":"Method described",
                     "limitations":"One study","rationale":"Inspect the paired comparison.","level":"insufficient","evidence_refs":[ref]}
            def submit(check,key):
                return s.call(s.lead,"gx_stage_claim",{**claim,"evidence_check":check},key)
            def read(**kwargs):
                return s.call(s.lead,"gx_read_source",{"source_ref":ref,**kwargs},"src-"+str(kwargs))
            paragraphs = read()["segments"]
            abstract = read(segment_id=next(p["segment_id"] for p in paragraphs if p["kind"]=="abstract"))
            body = read(segment_id=next(p["segment_id"] for p in paragraphs if p["label"].endswith("/p") and p["kind"]=="body"))
            with self.assertRaises(GxError) as wrong:
                submit({"grade":"body_method_checked","source_ref":ref,"receipt_id":abstract["receipt_id"]},"wrong-section")
            self.assertIn("body passage",str(wrong.exception))
            unrelated = read(segment_id=next(p["segment_id"] for p in paragraphs if p["label"]=="body/results/p"))
            with self.assertRaises(GxError):
                submit({"grade":"body_method_checked","source_ref":ref,"receipt_id":unrelated["receipt_id"]},"unrelated-body")
            with self.assertRaises(GxError) as missing_model:
                submit({"grade":"model_executed","source_ref":ref},"no-model")
            self.assertIn("prediction Job",str(missing_model.exception))
            with self.assertRaises(GxError):
                submit({"grade":"model_evaluation_checked","source_ref":ref,"reference_ref":ref,"evaluation_ref":ref},"no-validation")
            applied = submit({"grade":"body_method_checked","source_ref":ref,"receipt_id":body["receipt_id"]},"method")
            end = close_decision([ref,applied["claim_ref"]])
            end["ops"][0]["claim_refs"] = [applied["claim_ref"]]
            receipt = s.call(s.lead,"gx_decide_revision",{"decision":end},"publish-and-close")
            verified = receipt["claim_revisions"][0]["evidence_grade_receipt"]
            self.assertEqual(verified["text_sha256"],body["text_sha256"])
            self.assertEqual(verified["grade"],"body_method_checked")

    def test_equivalent_located_read_requires_same_publication_and_passage(self):
        for case, same_paper, same_text in (("equivalent",True,True), ("other",False,True), ("amended",True,False)):
            with self.subTest(case=case):
                folder = self.work/case
                folder.mkdir()
                files = []
                for name, extra, text, doi in (
                        ("original","","Co-located instruments were read concurrently.","10.1234/local"),
                        ("copy","<title>Reformatted</title>",
                         "Co-located instruments were read concurrently." if same_text else "Different instruments were used.",
                         "10.1234/local" if same_paper else "10.1234/foreign")):
                    path = folder/(name + ".xml")
                    path.write_text(f"<article><front><article-meta><article-id pub-id-type='doi'>{doi}</article-id>"
                        f"</article-meta></front><body>{extra}<sec id='methods'><p>{text}</p></sec></body></article>")
                    files.append({"path":str(path),"role":name})
                with Study(folder,spec_updates={"input_files":files}) as s:
                    original, copy = s.materials
                    def passage(ref):
                        listing = s.call(s.lead,"gx_read_source",{"source_ref":ref},"list-"+ref)
                        return next(p["segment_id"] for p in listing["segments"] if p["label"].endswith("/p"))
                    designated, equivalent = passage(original), passage(copy)
                    s.call(s.lead,"gx_link_core_material",{"ref":original,"segment_id":designated,
                           "reason":"How the instruments were paired could change the comparison."},"designate")
                    s.call(s.lead,"gx_read_source",{"source_ref":copy,"segment_id":equivalent},"inspect")
                    out = s.call(s.lead,"gx_decide_revision",insufficient(),"close")
                    if same_paper and same_text:
                        self.assertIn("decision_ref",out)
                    else:
                        self.assertEqual(out["materials"][0]["ref"],original)

    def test_trailing_bytes_do_not_clear_unread_flag(self):
        with Study(self.work,spec_updates={"closing_terms":{"core_materials":[{"role":"local_pair"}]}}) as s:
            ref = s.materials[0]
            excerpt = s.call(s.lead,"gx_read_artifact",{"ref":ref,"offset":16},"tail")
            self.assertFalse(excerpt["truncated"])
            self.assertEqual(s.call(s.lead,"gx_decide_revision",insufficient(),"close")["materials"][0]["ref"],ref)

    def test_required_join_delivery_can_designate_its_original_question(self):
        terms = {"research_modules":["data_inventory@1"], "capabilities":["tl1a-coordinate-quality@1"],
                 "plugins":["org.gx.protein@0.2.0"]}
        with Study(self.work,spec_updates=terms) as s:
            opened = s.open("paired",inputs=s.materials,caps=[])
            worker = s.bind(opened["task_id"])
            s.call(s.lead,"gx_watch_join",{"key":"local", "question":"Are local controls missing?",
                "mode":"all","ports":[{"slot":"paired-record","required":True,"alternatives":[
                    {"module_instance_id":opened["module_instance_id"],"output_name":"report"}]}]},"wait-for-pair")
            s.call(s.lead,"gx_decide_revision",{"decision":draft()},"review-later")
            s.call(worker,"gx_read_artifact",{"ref":s.materials[0],"operation_key":"pair"},"inspect")
            report = s.report(worker,s.materials[0],text="The paired local readings are available.")
            lead = s.next_actor("run")
            join = s.brief(lead)["research_joins"][0]
            self.assertEqual(join["state"],"SATISFIED")
            reply = s.call(lead,"gx_decide_revision",insufficient(join["question"]),"close")
            self.assertEqual(reply["materials"][0]["linked_by"],"join:local")
            self.assertEqual(reply["materials"][0]["ref"],report["report_ref"])
