"""The public score distinguishes an acted-on comparison from the label a researcher gave it."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from acceptance.v62c.test_scoring import ASSAY, example
from acceptance.v622.public import scoring, witness


def scenario(*, enough=False, delegated=False, offered=True):
    direction = "MAINTAIN" if enough else "CHANGE"
    trace = example(delegated=delegated,offered=offered,condition=direction)
    trace["initial_plan"]["ops"][0]["method_family"]="registered reference comparison"
    trace["claim_publications"] = []
    trace["completed_jobs"] = []
    trace["initial_plan"]["decision_ref"] = "proposal"
    trace["independent_count"] = {"selected":9 if enough else 2,"observed":10,"mask":"complete",
                                  "sha256":trace["assignment"]["sha256"]}
    for w in trace["lead_windows"]:
        w["automatic_nodes"] = []
        for c in w["candidates"]:
            m = c["metric"]
            m["scope"].update(model="1",entity="1",chains=["A","B","C"],input_sha256=trace["assignment"]["sha256"])
            trace["measurements"]["out"]["verified_metrics"]=[deepcopy(m)]
    action=trace["lead_windows"][-1]["action"]
    action.update(decision_ref="decide",rationale="Coverage is sufficient." if enough else "Coverage is insufficient.",ops=[])
    protocol = {"assay":ASSAY,"threshold":{"numerator":3,"denominator":5,"upper_action":"MAINTAIN","lower_action":"CHANGE"},
                "materials":{"parent_sha256":"reference-file"}}
    effect = action["effects"][0]
    if enough:
        effect.update(capability=ASSAY["capability"],parameters=ASSAY["parameters"],input_sha256=["reference-file"])
    else:
        effect["ref"]="original"
    return trace,protocol


def evaluate(trace,p):
    _,lock=scoring.opportunity(trace)
    return scoring.rate(trace,lock,p)[2]


class PublicActionScore(unittest.TestCase):
    def test_relabel_or_rename_the_original_comparison(self):
        trace,proto=scenario()
        action=trace["lead_windows"][-1]["action"]
        action["rationale"]="CHANGE, because coverage is below the threshold."
        action["effects"]=[{"ref":"renamed","branch":"new-id","kind":"completed_job","capability":ASSAY["capability"],
                            "parameters":ASSAY["parameters"],"input_sha256":["reference-file"]}]
        outcome=evaluate(trace,proto)
        self.assertEqual(("CHANGE","MAINTAIN",False),
                         (outcome["scientific_judgment"],outcome["decision"],outcome["science_pass"]))
        # Withdrawal and restarting the same work in a later turn cannot buy change credit.
        trace["completed_jobs"]=action["effects"]
        action["effects"]=[{"kind":"withdrawn_branch","ref":"original"}]
        self.assertFalse(evaluate(trace,proto)["science_pass"])

    def test_scoped_early_close_without_second_job(self):
        trace,proto=scenario()
        a=trace["lead_windows"][-1]["action"]
        a["effects"]=[{"kind":"closed_run","ref":"run"}]
        a["ops"]=[{"op":"close_run","unresolved":["The planned comparison has insufficient coverage."]}]
        result=evaluate(trace,proto)
        self.assertTrue(result["science_pass"])
        self.assertEqual(result["effect"]["kind"],"closed_run")
        a["ops"]=[{"op":"close_run"}]
        self.assertFalse(evaluate(trace,proto)["science_pass"])
        trace["closed"]=False
        self.assertFalse(evaluate(trace,proto)["science_pass"])

    def test_direct_maintain_and_authenticated_report(self):
        for enough,delegated,offered,classification in (
                (True,False,True,"NOT_USED"),(False,True,True,"DEMONSTRATED"),(False,False,False,"NOT_APPLICABLE")):
            trace,proto=scenario(enough=enough,delegated=delegated,offered=offered)
            result=evaluate(trace,proto)
            self.assertTrue(result["science_pass"])
            self.assertEqual(classification,result["delegation"])
            trace["lead_windows"][-1]["action"]["cited"]=[]
            self.assertFalse(evaluate(trace,proto)["science_pass"])

    def test_unrelated_raw_counts_or_unverified_cue_cannot_support_the_score(self):
        trace,proto=scenario()
        trace["independent_count"]["selected"]=1
        self.assertFalse(evaluate(trace,proto)["science_pass"])
        trace["lead_windows"][-1]["candidates"]=[{"metric":{"text":"ninety percent"}}]
        self.assertEqual("NO_ELIGIBLE_DELIVERY",evaluate(trace,proto)["opportunity"])

    def test_released_index_only_stays_unseen_until_a_successful_tool_output(self):
        def line(payload,kind="response_item"):
            return json.dumps({"type":kind,"payload":payload})+"\n"
        def log(case,after):
            return (line({"turn_id":"t"},"turn_context")+
                    line({"type":"function_call","call_id":"read","name":"gx_read_artifact"})+
                    line({"type":"function_call_output","call_id":"read","output":json.dumps(case)})+
                    (line({"type":"function_call","call_id":"decide","name":"gx_decide_revision"}) if after else ""))
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/"rollout.jsonl"
            turn={"turn_id":"t","decision":{"native_call_line":4}}
            calls={"read":("gx_read_artifact",{"ref":"report"},2)}
            path.write_text(log({"report_ref":"report","verified_metrics":[{"value":0.2}]},True))
            self.assertEqual(list(witness.read_outputs(path,turn,calls))[0][2]["verified_metrics"],[{"value":0.2}])
            path.write_text(log({"report_ref":"report","verified_metrics":[{"value":0.2}]},False))
            self.assertEqual(list(witness.read_outputs(path,turn,calls)),[])
            path.write_text(log({"report_ref":"report","qualitative_notes":{"text":"ninety percent"}},True))
            self.assertFalse(list(witness.read_outputs(path,turn,calls))[0][2].get("verified_metrics"))

    def test_scoped_positive_claim_requires_its_own_clock_method_and_raw_numeric_support(self):
        trace,p=scenario(enough=True)
        metric=trace["lead_windows"][-1]["candidates"][0]["metric"]
        metric["display"]="9/10"
        trace["measurements"]["out"]["verified_metrics"]=[deepcopy(metric)]
        reg={"event_id":5,"data":[{"sha256":trace["assignment"]["sha256"]}],"claim_scope":"this file",
             "prediction":{"operator":"ge","threshold":0.6},
             "method_pin":{"measurement_spec":{**ASSAY,"input_role":"structure"}}}
        claim={"claim_revision_id":"claim@2","sha256":"claim-hash","scope":"this file",
               "maturity":{"registration":reg},"evidence_refs":["out"],"level":"limited_support",
               "publication":{"scope":"this file","level":"limited_support"},"verified_metrics":[metric]}
        trace["claim_publications"]=[claim]
        self.assertTrue(evaluate(trace,p)["science_pass"])
        reg["event_id"]=trace["measurements"]["out"]["settled_event"]
        self.assertTrue(evaluate(trace,p)["unsupported_support"])
        reg["event_id"]=5
        reg["prediction"]["threshold"]=0.95
        self.assertTrue(evaluate(trace,p)["unsupported_support"])

    def test_a_preplanned_job_running_after_a_noop_cannot_earn_maintenance(self):
        trace,proto=scenario(enough=True)
        a=trace["lead_windows"][-1]["action"]
        a["effects"][0]["kind"]="maintained_job"
        outcome=evaluate(trace,proto)
        self.assertEqual("AUTOMATIC_ONLY",outcome["decision"])
        self.assertFalse(outcome["science_pass"])

    def test_no_registered_comparison_in_original_plan(self):
        trace,p=scenario()
        trace["initial_plan"]["ops"][0]["method_family"]="different question"
        outcome=evaluate(trace,p)
        self.assertEqual("NO_REGISTERED_COMPARISON",outcome["opportunity"])
        self.assertFalse(outcome["science_pass"])

    def test_late_module_requires_its_own_opening_and_exact_scoped_task(self):
        trace,p=scenario(delegated=True)
        j=trace["measurements"]["out"]
        j.update(actor_task="first", actor_cursor=6,grant_event=8)
        self.assertFalse(evaluate(trace,p)["science_pass"])
        proof={"task_id":"first","scope":j["scope"],"grant_event":8,"grant_sha256":"digest-of-original"}
        j["first_module_grant"]={**proof,"task_id":"other"}
        self.assertFalse(evaluate(trace,p)["science_pass"])
        j["first_module_grant"]=proof
        self.assertTrue(evaluate(trace,p)["science_pass"])

    def test_preplanned_or_unpublished_effect_and_changed_lock(self):
        trace,proto=scenario()
        trace["lead_windows"][-1]["action"]=None
        trace["lead_windows"][-1]["automatic_nodes"]=["scheduled"]
        result=evaluate(trace,proto)
        self.assertEqual(result["decision"],"AUTOMATIC_ONLY")
        self.assertFalse(result["science_pass"])
        _,locked=scoring.opportunity(trace)
        trace["lead_windows"][-1]["candidates"]=[]
        with self.assertRaises(ValueError):scoring.rate(trace,locked,proto)
