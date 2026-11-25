"""Read-only cohort export, partial data, no observational credit for source-only fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from acceptance.v622 import test_reports as report_tests
from acceptance.v623.oauth import export_results as public
from acceptance.v623.oauth import judge, pilot
from acceptance.v623 import test_oauth_judge as judge_tests


class Export(unittest.TestCase):
    def setUp(self):
        self.tmp=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.work=self.tmp/"original"
        self.work.mkdir()
        p=pilot.protocol()
        self.reg={**p,"source_commit":"a"*40,"scope":"PUBLIC_OAUTH_SUBSCRIPTION","host":"direct_chatgpt_oauth",
                  "account_ref":"secret-person@example.test","official_binary_sha256":"b"*64,
                  "protocol_sha256":pilot.registration.sha(p),"binary":"/private/home/bin/probe"}
        public.save(self.work/"registration.json",self.reg)
        self.result={"series":p["series"],"registration_sha256":public.digest(self.work/"registration.json"),
                     "cases":{n:{"status":"REGISTERED"} for n in pilot.SLOTS}}
        public.save(self.work/"result.json",self.result)

    def test_registered_partial_is_private_and_fixed_denominator(self):
        slot=self.work/"01"
        slot.mkdir()
        public.save(slot/"first-attempt.json",{"run_id":"gxr_"+"1"*18})
        public.save(slot/"summary.json",{"run_id":"gxr_"+"1"*18,"slot":"01","series":self.reg["series"],
                                    "architecture":"single","status":"CLOSED","untrusted":"secret-person@example.test"})
        self.result["cases"]["01"].update(status="HELD",scientific_tokens=10000)
        public.save(self.work/"result.json",self.result)
        state={p:p.read_bytes() for p in self.work.rglob("*.json")}
        folder=self.tmp/"portable"
        result=public.export(self.work,folder)
        self.assertEqual((result["original_assignments"],result["research_started"],result["start_unconfirmed"]),(4,0,1))
        self.assertEqual(result["research_terminal"],{})
        self.assertIsNone(result["total_verified_science_and_judge_tokens"])
        self.assertEqual(public.read(folder/"01.json")["evidence_status"],"NO_VERIFIED_NATIVE_ROUTE")
        self.assertEqual(public.read(folder/"02.json")["evidence_status"],"NOT_STARTED")
        for entry in result["slots"]:
            self.assertEqual(public.digest(folder/entry["file"]),entry["sha256"])
        self.assertEqual(state,{p:p.read_bytes() for p in self.work.rglob("*.json")})
        portable=" ".join(p.read_text() for p in folder.iterdir())
        for private in ("example.test","/private/home","account_ref","10000"):
            self.assertNotIn(private,portable)
        for text in ("stored at /private/home/bin","secret-person@example.test", "Token ghp_1234567890"):
            self.assertEqual(public.excerpt(text),"[private text withheld]")
        self.assertIn("49/164",public.excerpt("49/164 residues met the rule."))
        with self.assertRaises(ValueError):
            public.export(self.work,self.work/"overwrite")
        self.reg["slots"].reverse()
        public.save(self.work/"registration.json",self.reg)
        with self.assertRaises(ValueError):
            public.export(self.work,self.tmp/"reassigned")

    def test_measured_job_is_distinct_from_delivery_and_grade(self):
        case=report_tests.ReportEvidence()
        self.addCleanup(case.doCleanups)
        s,_,_,_,_,_=case.measured_output(self.tmp/"measured")
        self.assertEqual(public.quantified(s.directory,s.run_id,self.reg["slots"][0],self.reg),[])
        reg=deepcopy(self.reg); reg["assay"]["parameters"]["mask"]="ordered_backbone"
        readings=public.quantified(s.directory,s.run_id,reg["slots"][0],reg)
        self.assertEqual([(r["numerator"],r["denominator"]) for r in readings],[(49,164)])
        self.assertFalse(s.rows("SELECT 1 FROM v62_module_reports"))
        self.assertFalse((s.directory/"judging").exists())

    def test_read_only_judge_reconciliation_cannot_repair_tampered_receipt(self):
        fixture=judge_tests.NativeJudgeControls()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.review("NO_ASSIGNMENT")
        original={p:p.read_bytes() for p in fixture.d.rglob("*.json")}
        actual=judge._evaluate(fixture.work,"01",{},write_receipt=public.file_proof)
        self.assertIsNone(actual["science_pass"])
        self.assertEqual({p:p.read_bytes() for p in fixture.d.rglob("*.json")},original)
        target=fixture.d/"opportunity-native.json"
        target.write_text(json.dumps({"invented":True}))
        with self.assertRaisesRegex(ValueError,"native replay"):
            judge._evaluate(fixture.work,"01",{},write_receipt=public.file_proof)
        self.assertEqual(json.loads(target.read_bytes()),{"invented":True})
