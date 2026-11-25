"""The first decision, subscription approval, and operator review remain distinct."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from acceptance.science_execution import approval, witness
from gx_core.domain.ids import sha256_of_obj


class DiagnosticProof(unittest.TestCase):
    def test_first_after_measurement_includes_rejected_lead_turns(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE p0_roots(run_id,thread_id,task_id);
            CREATE TABLE p0_root_deliveries(run_id,thread_id,turn_id,method,state);
            CREATE TABLE v6_decision_tasks(run_id,task_id,scope);
            CREATE TABLE events(run_id,event_id,ts,subject_id,payload_json,kind,actor);
            CREATE TABLE v6_decisions(run_id,receipt_json);
            INSERT INTO p0_roots VALUES('r','a','t'),('r','b','u');
            INSERT INTO p0_root_deliveries VALUES('r','a','1','turn/start','acknowledged'),('r','b','2','turn/start','acknowledged');
            INSERT INTO v6_decision_tasks VALUES('r','t','run'),('r','u','module:m');
            INSERT INTO v6_decisions VALUES('r','{"scope":"run","event_id":24}');
        """)
        def reject(n,thread,turn):
            db.execute("INSERT INTO events VALUES('r',?,'t',?,?,'v624.decision.rejected','gx')",
                       (n,sha256_of_obj([thread,turn]),json.dumps({"field":"ops","reason":"AMBIGUOUS"})))
        reject(4,"a","1")
        reject(22,"b","2")
        self.assertTrue(witness.submission_record(db,"r",20,24)["first_post_delivery_accepted"])
        reject(23,"a","1")
        result = witness.submission_record(db,"r",20,24)
        self.assertFalse(result["first_post_delivery_accepted"])
        self.assertEqual(result["first_after_delivery"],23)

    def test_scripted_archive_cannot_freeze_or_take_operator_live_credit(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)
            (path/"registration.json").write_text('{"host":"offline"}')
            with self.assertRaisesRegex(ValueError,"subscription"):
                witness.freeze_ready(path,candidate="c",account_ref="x")
            with patch.object(witness,"evaluate",return_value={"live":False,"run_id":"r","mechanical_pass":True}):
                with self.assertRaisesRegex(ValueError,"subscription"):
                    witness.review(path,"01","operator","PASS","a measurement")

    def test_exact_original_user_scope_and_irreversible_risk_acceptance(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/"source"
            answer = {"questionItemId":json.dumps(["request_user_input_async","original",0]),
                      "question":approval.QUESTION,"answer":approval.CHOICES[1]}
            def event(role,doc):
                body="<send_user_message_question_reply>\n"+json.dumps([doc],ensure_ascii=False)+"\n</send_user_message_question_reply>"
                return json.dumps({"type":"response_item","timestamp":"t","payload":{"type":"message",
                    "role":role,"content":[{"text":body}]}},ensure_ascii=False)
            with patch.object(approval.original,"_source",return_value=path):
                path.write_text(event("assistant",answer)+"\n"+event("user",{**answer,"question":"original four cells"}))
                with self.assertRaises(ValueError):
                    approval.user_choice("thread")
                path.write_text(event("user",answer))
                self.assertEqual(approval.user_choice("thread")["choice"],approval.CHOICES[1])
            path.write_text('{"schema":"gx.pi.oauth-pilot/1"}')
            with self.assertRaisesRegex(ValueError,"four-cell"):
                approval.verify(path,work=folder,candidate="c",series=approval.DIAGNOSTIC,account_ref="x")


if __name__=="__main__":
    unittest.main()
