"""A Lead keeps its frozen evidence and graph visibility while a module writes its own branch."""
import json
from pathlib import Path
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import Scenario, draft
from gx_core.errors import ScopeDenied, StaleRevision


class LeadGraphIsolation(unittest.TestCase):
    def scenario(self):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "case"
        return self.enterContext(Scenario(path))

    def test_private_decision_and_unreleased_report_stay_outside_the_original_lead_brief(self):
        s = self.scenario()
        lead, source = s.lead, s.materials[0]
        left = s.bind(s.open("left", inputs=[source])["task_id"])
        right = s.bind(s.open("right", inputs=[source])["task_id"])
        frozen = s.brief(lead)
        child = s.submit_node(left, "measurement")
        s.call(right, "gx_read_artifact", {"ref": source, "operation_key": "read"}, "read")
        report = s.report(right, source)
        self.assertIsNone(report["wake_event_id"])
        for ref in (child["decision_ref"], report["report_ref"]):
            with self.subTest(ref=ref):
                self.assertNotIn(ref, frozen["evidence_refs"])
                with self.assertRaises(ScopeDenied):
                    s.call(lead, "gx_read_artifact", {"ref": ref}, "read-" + ref)
                with self.assertRaises(ScopeDenied):
                    s.call(lead, "gx_decide_revision", {"decision": draft(evidence=[ref])}, "cite-" + ref)
        self.assertEqual(frozen, s.brief(lead))
        accepted = s.call(lead, "gx_decide_revision", {"decision": draft(evidence=[source])}, "cite-delivered")
        self.assertEqual(child["result_head"], accepted["result_head"])
        saved, = s.rows("SELECT request_json FROM v6_decisions WHERE task_id=? AND turn_id=?", lead.task_id, lead.turn)
        self.assertEqual([source], json.loads(saved["request_json"])["decision"]["evidence_refs"])

    def test_scope_refusal_reports_only_the_first_argument_path(self):
        s = self.scenario()
        lead, source = s.lead, s.materials[0]
        worker = s.bind(s.open("private", inputs=[source])["task_id"])
        child = s.submit_node(worker, "private-measurement")
        private = child["decision_ref"]
        frozen = s.brief(lead)
        self.assertNotIn(private, frozen["evidence_refs"])
        self.assertTrue(s.rows("SELECT decision_ref FROM v6_decisions WHERE decision_ref=?", private))
        invented = "caller-only fictional reference"
        secret = "PRIVATE-SENTINEL 未發布的研究筆記"
        never_echo = {*frozen["evidence_refs"], *s.brief(worker)["evidence_refs"], private, invented, secret,
                      child["aliases"]["local:private-measurement"]}
        for field in ("evidence_refs[1]", "ops[0].claim_refs[0]"):
            with self.subTest(field=field):
                errors = []
                for i, invalid in enumerate(([private, secret], [secret, private], [invented, secret])):
                    close = {"op": "close_run", "reason": "Conclude after reviewing the evidence.",
                             "claim_refs": list(invalid), "unresolved": []}
                    evidence = [source, *invalid] if field.startswith("evidence_refs") else [source]
                    with self.assertRaises(ScopeDenied) as refused:
                        s.call(lead, "gx_decide_revision", {"decision": draft(close, evidence=evidence)},
                               f"refusal-{field}-{i}")
                    payload = refused.exception.to_payload()
                    self.assertEqual({"field": field}, payload.get("details"))
                    for value in never_echo:
                        self.assertNotIn(value, json.dumps(payload, ensure_ascii=False))
                    errors.append(payload)
                self.assertEqual(errors[0], errors[1])
                self.assertEqual(errors[0], errors[2])
        self.assertEqual(frozen, s.brief(lead))
        self.assertEqual([], s.rows("SELECT decision_ref FROM v6_decisions WHERE task_id=?", lead.task_id))

    def test_unseen_child_node_cannot_be_cancelled_from_a_stale_lead_brief(self):
        s = self.scenario()
        lead = s.lead
        worker = s.bind(s.open("measurement")["task_id"])
        self.assertEqual([], s.brief(lead)["nodes"])
        child = s.submit_node(worker, "measurement")
        node = child["aliases"]["local:measurement"]
        cancel = {"op": "cancel_node", "node": node, "reason": "Withdraw this measurement."}
        with self.assertRaises((ScopeDenied, StaleRevision)):
            s.call(lead, "gx_decide_revision", {"decision": draft(cancel)}, "withdraw-unseen-work")
        self.assertEqual(child["result_head"], s.head())
        self.assertEqual("PENDING", s.rows("SELECT status FROM v6_nodes WHERE node_id=?", node)[0]["status"])


if __name__ == "__main__":
    unittest.main()
