"""Local judge transport and adjudication controls. All responses here are test fixtures."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from acceptance.v623.oauth import judge
from acceptance.v623.live.action_witness import visible_text


class NativeJudgeControls(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work=Path(self.tmp.name)
        self.d=judge._slot(self.work,"01")
        self.home=self.d/"host/codex"
        (self.home/"sessions").mkdir(parents=True)
        self.native=self.home/"sessions/control-thread.jsonl"
        self.row("session_meta",id="control-thread",model_provider="openai")
        self.put("thread",{"thread_id":"control-thread"})
        self.put("opportunity-packet",{"lines":{"P":"test plan"}})
        self.put("outcome-packet",{"lines":{"A":"effect","R":"reasons","I":"intent"}})

    def row(self,kind,**payload):
        with self.native.open("a") as stream:
            stream.write(json.dumps({"type":kind,"payload":payload})+"\n")

    def put(self,name,data):
        judge.save(self.d/(name+".json"),data)

    def turn(self,stage,response,total,credits,*,terminal=True,usage=True):
        prompt="test-only "+stage
        self.put(stage+"-intent",{"thread_id":"control-thread","model":"test-model","effort":"max","prompt":prompt})
        self.put(stage+"-accepted",{"turn_id":stage})
        self.row("event_msg",type="task_started",turn_id=stage)
        self.row("turn_context",turn_id=stage,model="test-model",effort="max")
        self.row("response_item",type="message",role="user",content=[{"text":prompt}])
        self.row("response_item",type="message",role="assistant",content=[{"text":json.dumps(response)}])
        if usage:
            self.row("event_msg",type="token_count",info={"total_token_usage":{"total_tokens":total}},
                     rate_limits=None if credits is None else {"credits":credits})
        if terminal:
            self.row("event_msg",type="task_complete",turn_id=stage)

    def review(self,state,marks=None):
        self.put("private-opportunity",{"state":state})
        self.put("private-outcome",{"scientifically_interpretable":False,"state":state,"endpoint_cited":False,
            "independent_match":False,"action_matches_intent":False,"intent":None,"enacted":"NO_ACCEPTED_ACTION",
            "delegation":"UNKNOWN","decision_event":None,"stop_reason":None})
        self.turn("opportunity",{"state":state,"measurement_lines":[],"basis":"No supplied reading."},35,None)
        self.turn("reason",{"criteria":{c:{"mark":(marks or {}).get(c,"UNCLEAR"),"lines":["R","A"],
            "basis":"The test packet contains no usable comparison."} for c in judge.rating.CRITERIA}},95,{})
        self.account("READY","INCLUDED_AVAILABLE")
        return judge.reconcile(self.work,1,{})

    def account(self,status,funding):
        path=self.d/"host/oauth-account.json"
        path.write_text(json.dumps({"status":status,"subscription_usage_state":funding,"admission":{"status":"READY"}}))

    def test_no_assignment_unclear_does_not_create_disagreement(self):
        result=self.review("NO_ASSIGNMENT")
        self.assertEqual(result["judgment_status"],"NO_INTERPRETABLE_DECISION")
        self.assertIsNone(result["science_pass"])
        self.assertIsNone(result["agreement"])
        self.assertEqual(result["total_tokens"],95)
        self.assertEqual(judge.rating.read(self.d/"reason-native.json")["total_tokens"],60)
        self.assertEqual(result,judge.reconcile(self.work,1,{}))

    def test_no_opening_unclear(self):
        self.assertEqual(self.review("NO_OPENING_COMPARISON")["judgment_status"],"NO_INTERPRETABLE_DECISION")

    def test_no_delivery_unclear(self):
        self.assertEqual(self.review("NO_ELIGIBLE_DELIVERY")["judgment_status"],"NO_INTERPRETABLE_DECISION")

    def test_reviewer_claims_endpoint_with_no_assignment(self):
        result=self.review("NO_ASSIGNMENT",{"saw_endpoint":"PASS"})
        self.assertEqual(result["judgment_status"],"PENDING_AMBIGUITY")
        self.assertIsNone(result["science_pass"])

    def test_new_extra_charge_or_absent_postflight_cannot_unlock_next_slot(self):
        self.review("NO_ASSIGNMENT")
        self.account("BLOCKED","ADDITIONAL_CHARGE")
        result=judge.reconcile(self.work,1,{})
        self.assertEqual((result["status"],result["total_tokens"],result["settled_tokens"]),("ADDITIONAL_CHARGE",None,95))
        (self.d/"host/oauth-account.json").unlink()
        self.assertEqual(judge.reconcile(self.work,1,{})["status"],"UNKNOWN")

    def test_incomplete_native_terminal_or_usage_has_no_known_settlement(self):
        self.turn("opportunity",{},0,None,terminal=False,usage=False)
        self.assertEqual(judge.reconcile(self.work,1,{})["status"],"UNKNOWN")
        self.row("event_msg",type="task_complete",turn_id="opportunity")
        self.assertEqual(judge.reconcile(self.work,1,{})["status"],"UNKNOWN")
        self.assertFalse((self.d/"opportunity-native.json").exists())

    def test_private_labels_removed_but_chemical_selection_and_reason_remain(self):
        text="P4 / K7: In the delegation_available arm GPT-6.2-astra asked data_inventory@3. Coverage 49/164 is below threshold."
        masked=visible_text(text)
        for label in ("P4","K7","delegation_available","GPT-6.2-astra","data_inventory"):
            self.assertNotIn(label,masked)
        self.assertIn("Coverage 49/164 is below threshold.",masked)


if __name__=="__main__":
    unittest.main()
