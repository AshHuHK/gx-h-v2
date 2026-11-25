import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v62a.interleavings_fixture import Scenario, branch, draft
from acceptance.science_execution.study import operate, protocol
from gx_core.errors import GxError
from gx_core.p0 import v621_assignment


def proposal(n=1, language="zh", interval="21-40"):
    method = {"capability":protocol.METHOD,"parameters":{"interval":interval,"mask":"common_ca","endpoint":"angle_deg"},
              "output_name":"segment.json","metric_id":"median_delta_angle_deg","unit":"deg"}
    prediction = {"blind_id":"blind","expected":"遮蔽例外" if language=="zh" else "An individual pair may exceed the limit",
        "on_expected":"逐對報告" if language=="zh" else "Identify the outlying pair",
        "on_alternative":"維持限定範圍的代表值" if language=="zh" else "Retain a local median summary"}
    plan = {"branch":"local:plan","material_role":"structure","method":method,"prediction":prediction}
    binding = {"method":copy.deepcopy(method),"material_role":"structure","initial_event":n,"decision_ref":"d"+str(n),
             "goal_version":1,"branch":"b"+str(n),"comparison_id":"c"+str(n),"method_manifest_sha256":"sha","prediction":dict(prediction)}
    return {"decision_ref":"d"+str(n),"event_id":n,"utc":"now","receipt":{"comparison":binding},
            "decision":{"comparison_plan":plan,"evidence_refs":["card"],"rationale":"No prefix or phrasing required."}}


class Commitments(unittest.TestCase):
    card = {"blind_id":"blind","windows":{i:{"eligible_masks":["common_ca"]} for i in protocol.INTERVALS}}

    def test_only_formally_adopted_choices_open_in_both_languages(self):
        for lang in ("en","zh"):
            d = proposal(language=lang)
            self.assertEqual(protocol.primary(d,self.card,"card")["prediction"],d["decision"]["comparison_plan"]["prediction"])
            d["receipt"].clear()
            self.assertIsNone(protocol.primary(d,self.card,"card"))

    def test_unrelated_setup_missing_endpoint_and_another_specimen_do_not_open(self):
        first = proposal()
        first["decision"]["comparison_plan"]["method"]["parameters"].pop("endpoint")
        wrong = proposal(2)
        wrong["decision"]["comparison_plan"]["prediction"]["blind_id"] = "different"
        self.assertIsNone(protocol.first_primary([first,wrong],self.card,"card"))
        correct = proposal(3,language="en")
        self.assertEqual(protocol.first_primary([first,wrong,correct],self.card,"card")["decision_ref"],"d3")
        self.assertIsNone(protocol.primary(correct,self.card,"unrelated-card"))

    def test_same_event_later_equally_valid_prediction_and_post_open_method_change(self):
        initial = proposal()
        later = proposal(4,"en","101-120")
        self.assertEqual(protocol.primary(later,self.card,"card")["method"]["parameters"]["interval"],"101-120")
        self.assertEqual(protocol.first_primary([initial,initial,later],self.card,"card"),protocol.primary(initial,self.card,"card"))
        initial["decision"]["comparison_plan"]["method"]["parameters"]["mask"]="common_backbone"
        self.assertIsNone(protocol.primary(initial,self.card,"card"))

    def test_companion_failure_rolls_back_first_assignment_and_resend_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            store = work/"files"
            store.mkdir()
            raw = operate.fixture(store)
            with Scenario(work/"gx") as s:
                plan = s.call(s.lead,"gx_decide_revision",{"decision":draft()},"seal")["decision_ref"]
                a = {"source":"blind","path":raw["source_path"],"sha256":raw["source_sha256"],"role":"structure",
                     "metadata_path":raw["descriptor_path"],"media_type":"chemical/x-mmcif"}
                b = {**a,"role":"mapping","path":raw["mapping_path"],"sha256":raw["mapping_sha256"],"media_type":"application/json"}
                def assign(items):
                    return v621_assignment.assign_group(s.conn,s.run_id,after_decision=plan,actor="operator",items=items)
                with self.assertRaises(GxError):
                    assign([a,b])  # Same idempotency key but a different immutable payload.
                self.assertFalse(s.rows("SELECT 1 FROM events WHERE kind='v621.material.assigned'"))
                b["source"]="blind-mapping"
                both = assign([a,b])
                self.assertEqual(both,assign([a,b]))
                self.assertEqual(len(s.rows("SELECT 1 FROM events WHERE kind='v621.material.assigned'")),2)

    def test_later_accepted_plan_cannot_replace_or_prevent_original_group_opening(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            store = work/"files"
            store.mkdir()
            raw = operate.fixture(store)
            cardfile = store/"card.json"
            cardfile.write_text(json.dumps(self.card))
            spec = {"evidence_policy":"gx.evidence/6.2.4","plugins":[protocol.PLUGIN],"research_modules":[],
                    "capabilities":[protocol.METHOD],"input_files":[{"role":"workflow_spec","path":str(cardfile)}]}
            with Scenario(work/"gx",spec_updates=spec) as s:
                card_ref, = s.materials
                def commit(actor, name, plan):
                    body = draft(branch(name),evidence=[card_ref])
                    body["comparison_plan"] = {**plan["decision"]["comparison_plan"],"branch":"local:"+name}
                    return s.call(actor,"gx_decide_revision",{"decision":body},name)
                first = commit(s.lead,"first",proposal())
                extra = {"source":"extra","path":str(cardfile),"sha256":operate.digest(cardfile),"role":"context",
                         "metadata_path":raw["descriptor_path"],"media_type":"application/json"}
                v621_assignment.assign(s.conn,s.run_id,after_decision=first["decision_ref"],actor="operator",**extra)
                later = commit(s.next_actor("run"),"later",proposal(2,"en","101-120"))
                self.assertNotEqual(later["decision_ref"],first["decision_ref"])
                first_primary = protocol.first_primary(protocol.accepted(s.conn,s.run_id),self.card,card_ref)
                self.assertEqual(first_primary["decision_ref"],first["decision_ref"])
                data = {**extra,"source":"original","path":raw["source_path"],"sha256":raw["source_sha256"],"role":"structure",
                        "media_type":"chemical/x-mmcif"}
                def group(compare):
                    return v621_assignment.assign_group(s.conn,s.run_id,after_decision=first["decision_ref"],
                            comparison_id=compare,actor="operator",items=[data])
                with self.assertRaises(GxError):
                    group(later["comparison"]["comparison_id"])
                self.assertEqual(group(first_primary["comparison_id"])[0]["decision_ref"],first["decision_ref"])

    def test_rollout_window_is_retained_across_restarts(self):
        with tempfile.TemporaryDirectory() as temp:
            slot=Path(temp)
            registration={"series":"Science","new_work_seconds":1800,"drain_seconds":360,"reconcile_seconds":120}
            first=operate.first_attempt(slot,registration,100.)
            self.assertEqual(first["research_window"],{"start":100.,"new_work_until":1900.,"drain_until":2260.,"reconcile_until":2380.})
            self.assertEqual(operate.first_attempt(slot,registration,4000.),first)

    def test_actual_negative_custody_never_creates_lead_or_host(self):
        evidence = operate.ROOT/"acceptance/v624/custody/receipts/eligibility.json"
        with tempfile.TemporaryDirectory() as tmp, patch.object(operate.host,"session",side_effect=AssertionError):
            work = Path(tmp)/"new"
            sealed = operate.seal(work,Path(tmp)/"missing",custody=evidence)
            self.assertEqual(sealed["qualification"],"NO_ELIGIBLE")
            self.assertEqual(operate.run(work,Path(tmp)/"missing",None,None)["status"],"NOT_ENROLLED")
            self.assertFalse((work/"lead").exists())


if __name__=="__main__":
    unittest.main()
