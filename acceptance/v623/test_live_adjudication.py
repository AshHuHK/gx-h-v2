"""Synthetic reader-contract tests; test keys and opinions never enroll a real series."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from acceptance.v622.test_public_score import scenario
from acceptance.v623.live import action_witness as aw, adjudication as jury


def case(enough=False):
    t,p=scenario(enough=enough)
    branch=t["initial_plan"]["ops"][0];branch["method_family"]="左岸 alias XX"
    t["binding"]={"comparison_id":"cmp_123", "initial_event":1, "decision_ref":"proposal","branch":branch["branch"],
                  "method":p["assay"],"reference_sha256":["reference-file"]}
    t["status"]="CLOSED"
    t["initial_plan"]["rationale"]="Read the specimen first; plan a comparison with the reference."
    w=t["lead_windows"][-1];a=w["action"]
    intent={"comparison_id":"cmp_123","disposition":"MAINTAIN" if enough else "CHANGE",
            "rationale":"共同骨架並非不足，維持現有比較。" if enough else "The coverage is not sufficient for the comparison.",
            "affected_scope":"observed coordinate positions"}
    for other in t["lead_windows"]:other["accepted"]={}
    w["accepted"]={"comparison_action":intent,"endpoint_use":{"used_metric_refs":["metric_a"]},"event_id":15,
                   "decision_ref":a["decision_ref"]}
    return t,p


def packets(t,p):
    b,lock=aw.reader_packet(t)
    packet,result=aw.outcome_packet(t,p,lock)
    return b,packet,result


class ScientificWitness(unittest.TestCase):
    def test_free_names_negations_and_tool_order_are_not_a_scientific_judge(self):
        for enough in (False,True):
            t,p=case(enough)
            blind,result,machine=packets(t,p)
            self.assertEqual("DELIVERED",machine["state"])
            self.assertEqual(machine["intent"],machine["enacted"])
            self.assertTrue(machine["action_matches_intent"])
            self.assertEqual(result["lines"]["R"], t["lead_windows"][-1]["accepted"]["comparison_action"]["rationale"])
            self.assertNotIn("R",blind["lines"])
            for x in (blind,result):
                for omitted in ("offered","cost","delegation","actor","run_id","scope_appropriate","branch","role","reviewer"):
                    self.assertNotIn(omitted,x)
            self.assertNotIn("science_pass",machine)

    def test_wrong_reason_guessed_enum_and_mislabeled_job(self):
        t,p=case()
        w=t["lead_windows"][-1]
        w["accepted"]["comparison_action"]["rationale"]="Coverage is ample; complete the unchanged comparison."
        _,packet,r=packets(t,p)
        self.assertTrue(r["action_matches_intent"])
        self.assertEqual(packet["lines"]["R"],"Coverage is ample; complete the unchanged comparison.")
        self.assertNotIn("science_pass",r)
        w["action"]["effects"]=[{"kind":"completed_job","capability":p["assay"]["capability"],
            "parameters":p["assay"]["parameters"],"input_sha256":["reference-file"],"branch":"rename","ref":"new"}]
        _,_,r=packets(t,p);self.assertFalse(r["action_matches_intent"]);self.assertEqual(r["enacted"],"MAINTAIN")

    def test_unread_and_administrative_stop_preserves_intent_and_original_slot(self):
        t,p=case();w=t["lead_windows"][-1]
        w["candidates"]=[];w["accepted"]["comparison_action"].update(disposition="STOP",stop_reason="UNREAD_EVIDENCE")
        w["action"]["effects"]=[{"kind":"closed_run","ref":"run"}]
        b,_,m=packets(t,p)
        self.assertFalse(any(k.startswith("M") for k in b["lines"]))
        self.assertEqual((m["state"],m["enacted"],m["stop_reason"]),("NO_ELIGIBLE_DELIVERY","STOP","UNREAD_EVIDENCE"))
        self.assertFalse(m["scientifically_interpretable"])
        t,p=case();w=t["lead_windows"][-1]
        w["action"]["effects"]=[{"kind":"closed_run","ref":"run"}]
        for why in ("BUDGET","TIME_LIMIT","OPERATOR","INSUFFICIENT_DATA"):
            w["accepted"]["comparison_action"].update(disposition="STOP",stop_reason=why)
            _,_,m=packets(t,p);self.assertEqual(m["stop_reason"],why)
            self.assertEqual(m["scientifically_interpretable"],why=="INSUFFICIENT_DATA")

    def test_citation_cannot_derive_from_attachment_and_automatic_dispatch(self):
        t,p=case(True);w=t["lead_windows"][-1];w["accepted"]["endpoint_use"]["used_metric_refs"]=[]
        _,_,m=packets(t,p);self.assertFalse(m["endpoint_cited"])
        w["action"]["effects"][0]["kind"]="maintained_job"
        _,_,m=packets(t,p);self.assertEqual("AUTOMATIC_ONLY",m["enacted"])
        self.assertFalse(m["action_matches_intent"])
        w["candidates"]=[];self.assertEqual(packets(t,p)[2]["state"],"NO_ELIGIBLE_DELIVERY")

    def test_binding_actual_plan_and_first_delivered_value_cannot_be_reassigned(self):
        t,p=case()
        _,frozen=aw.reader_packet(t);later=deepcopy(t)
        later["binding"]["comparison_id"]="another-comparison"
        with self.assertRaisesRegex(ValueError,"frozen"):aw.outcome_packet(later,p,frozen)
        t["binding"]=None
        self.assertEqual(aw.reader_packet(t)[1]["state"],"NO_OPENING_COMPARISON")


class SignedJudgments(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        roles=[]
        for number in range(4):
            key=self.base/str(number)
            subprocess.run(["ssh-keygen","-q","-t","ed25519","-N","","-f",str(key)],check=True)
            roles.append({"id":"SYNTHETIC_TEST_"+str(number),"kind":"human","independent_of_design_development_execution":True,
                          "public_key":" ".join(Path(str(key)+".pub").read_text().split()[:2])})
        roster={"schema":"gx.live-review-roster/1","series":jury.read(jury.PROTOCOL)["series"],
                "opportunity_reader":roles[0],"reviewers":roles[1:3],"arbitrator":roles[3]}
        jury.validate_roster(roster)
        self.reg={"host":"live","roster":roster,"series":roster["series"],"rubric_sha256":jury.file_hash(jury.RUBRIC)}
        self.roles=roles
        _,packet,machine=packets(*case())
        self.packet=packet;self.machine=machine
        self.path=self.base/"rubric-test";self.path.mkdir();jury.freeze(self.path/"outcome.json",packet)
        self.machine["classification"]="TEST"
        for name,val in (("_bound",(self.reg,self.path)),("_audit_opportunity",{}),
                         ("_cost_and_host",{}),("outcome",(self.packet,self.machine))):
            ctx=patch.object(jury,name,return_value=val);ctx.start();self.addCleanup(ctx.stop)

    def marked(self,num,mark,kind="reason",originals=None):
        body={"schema":"gx.live-review/1","reviewer":self.roles[num]["id"],"stage":kind,"series":self.reg["series"],
              "packet_sha256":jury.digest(self.packet),"rubric_sha256":self.reg["rubric_sha256"],
              "criteria":{k:{"mark":mark if k=="reason_scope" else "PASS",
                            "basis":"synthetic rubric test only","lines":["P","M1","R","I","A","C"]} for k in jury.CRITERIA}}
        if originals is not None:body["originals"]=originals
        path=self.base/("submission"+str(num));path.write_text(json.dumps(body))
        sig=Path(str(path)+".sig");sig.unlink(missing_ok=True)
        subprocess.run(["ssh-keygen","-Y","sign","-q","-f",str(self.base/str(num)),
                        "-n",jury.NAMESPACE,str(path)],check=True)
        return {"text":path.read_text(),"signature":sig.read_text()}

    def test_disagreement_preserves_both_originals_and_requires_registered_arbitrator(self):
        a=self.marked(1,"PASS");b=self.marked(2,"FAIL")
        first=jury.submit(self.base,"01",a);self.assertIsNone(first["agreement"])
        split=jury.submit(self.base,"01",b)
        self.assertEqual(split["agreement"],{"matching":3,"total":4});self.assertIsNone(split["science_pass"])
        originals={self.roles[1]["id"]:jury.digest(a),self.roles[2]["id"]:jury.digest(b)}
        resolution=self.marked(3,"FAIL","resolution",originals)
        final=jury.submit(self.base,"01",resolution,resolve=True)
        self.assertFalse(final["science_pass"]);self.assertTrue(final["interpretable"])
        self.assertEqual(jury.read(self.path/"reviews"/(self.roles[1]["id"]+".json")),a)
        self.assertEqual(final,jury.score(self.base,"01"))

    def test_one_reader_no_agreement_wrong_reason_cannot_pass_and_tampering_rejected(self):
        self.reg["roster"]["reviewers"]=self.roles[1:2]
        a=self.marked(1,"FAIL")
        with self.assertRaisesRegex(ValueError,"signature"):
            jury.submit(self.base,"01",{**a,"text":a["text"].replace('"FAIL"','"PASS"')})
        final=jury.submit(self.base,"01",a)
        self.assertFalse(final["science_pass"]);self.assertIsNone(final["agreement"])
        with self.assertRaisesRegex(ValueError,"differs"):jury.submit(self.base,"01",self.marked(1,"PASS"))

    def test_roster_rejects_overlapping_roles_and_principals(self):
        bad=deepcopy(self.reg["roster"])
        bad["arbitrator"]["public_key"]=bad["reviewers"][0]["public_key"]
        with self.assertRaises(ValueError):jury.validate_roster(bad)

    def test_offline_machine_cannot_be_reported_as_reviewed_science(self):
        self.reg["host"]="offline";result=jury.score(self.base,"01")
        self.assertIsNone(result["science_pass"]);self.assertEqual(result["status"],"PENDING_INDEPENDENT_REVIEW")
        with self.assertRaisesRegex(ValueError,"Offline"):jury.submit(self.base,"01",self.marked(1,"PASS"))


if __name__=="__main__":unittest.main()
