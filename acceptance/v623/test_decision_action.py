"""The accepted action, durable comparison and enacted close share their event window."""
import json
from pathlib import Path
import tempfile
import unittest

from acceptance.v622.test_close import CloseStudy, actual_packet, close_decision
from acceptance.v62a.interleavings_fixture import draft
from gx_core.errors import SchemaInvalid, ScopeDenied
from gx_core.p0 import v6_goal
from tests.test_v6_decisions import branch


def comparison_open():
    return {**draft(branch("route")),
            "comparison_plan": {
                "branch": "local:route",
                "material_role": "structure",
                "method": {"capability": "deposited-chain-geometry@2", "parameters": {"mask": "common_ca"},
                           "output_name": "geometry.json", "metric_id": "coverage", "unit": "1"},
            }}


class AcceptedAction(unittest.TestCase):
    def study(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory())) / "action"

    def test_accepted_stop_binds_initial_plan_and_wrong_comparison_rolls_back(self):
        work = self.study()
        with CloseStudy(work, spec_updates={"plugins": ["org.gx.protein@0.3.0"],
                                            "capabilities": ["deposited-chain-geometry@2"],
                                            "research_modules": []}) as s:
            forged = comparison_open()
            forged["comparison_plan"]["method"]["output_name"] = "unregistered-coverage.json"
            with self.assertRaises((ScopeDenied, SchemaInvalid)):
                s.call(s.lead, "gx_decide_revision", {"decision": forged}, "unregistered-method")
            self.assertFalse(s.rows("SELECT 1 FROM events WHERE kind='v6.decision.committed'"))
            first = s.call(s.lead, "gx_decide_revision", {"decision": comparison_open()}, "open")
            binding = first["comparison"]
            self.assertEqual(binding["initial_event"], first["event_id"])
            self.assertEqual(binding["branch"], first["aliases"]["local:route"])
            v6_goal.record_observation(s.conn, s.run_id, s.materials[-1], source="input-review")
            lead = s.next_actor("run")
            packet = actual_packet(s, lead)
            self.assertIn(binding["comparison_id"], json.dumps(packet["packet"]))
            action = {**close_decision(), "comparison_id": binding["comparison_id"], "metric_refs": [],
                      "disposition": "STOP", "stop_reason": "INSUFFICIENT_DATA",
                      "affected_scope": "An independent second acquisition",
                      "rationale": "The supplied inventory cannot establish independent sampling."}
            wrong = {**action, "comparison_id": "cmp_unrelated"}
            with self.assertRaises(SchemaInvalid):
                s.call(lead, "gx_decide_revision", {"decision": wrong}, "wrong-comparison")
            self.assertEqual(s.rows("SELECT status FROM v6_goals"), [{"status": "OPEN"}])
            self.assertFalse(s.rows("SELECT 1 FROM events WHERE kind='v6.run.close_requested'"))
            accepted = s.call(lead, "gx_decide_revision", {"decision": action}, "adopt-stop")
            for subject in ("v6.decision.committed", "v6.run.close_requested"):
                row, = s.rows("SELECT payload_json FROM events WHERE kind=? "
                              "AND json_extract(payload_json,'$.decision_id')=?", subject, accepted["decision_ref"])
                payload = json.loads(row["payload_json"])
                for key in ("comparison_id", "disposition", "stop_reason", "affected_scope", "rationale", "metric_refs"):
                    self.assertEqual(payload[key], action[key])
                self.assertEqual(payload["endpoint_use"]["used_metric_refs"], [])
            s.advance()
            self.assertEqual(s.rows("SELECT status FROM v6_goals"), [{"status": "CLOSED"}])
        with CloseStudy(work, reopen=True) as s:
            result = s.call(lead, "gx_decide_revision", {"decision": action}, "adopt-stop")
            self.assertEqual(result, accepted)
            self.assertEqual(len(s.rows("SELECT 1 FROM events WHERE kind='v6.run.close_requested'")), 1)
            self.assertFalse(s.rows("SELECT job_id FROM jobs"))

    def test_direct_job_reference_requires_an_explicit_endpoint_for_measured_closure(self):
        method = "deposited-chain-geometry@2"
        with CloseStudy(self.study(), spec_updates={"research_effort": "quick", "research_modules": [],
                "capabilities": [method], "plugins": ["org.gx.protein@0.3.0"]}) as s:
            route = {**branch("direct"), "budget_limits": {"job_submissions": 1, "cpu_seconds": 90}}
            measure = s.node(s.lead, "coverage", s.materials[0], branch_id="local:direct",
                             capability=method, parameters={"mask": "ordered_backbone"})
            plan = draft(route, measure, watches=[{"event": "job_settled", "subject_ref": "local:coverage"}])
            issued = s.call(s.lead, "gx_decide_revision", {"decision": plan}, "direct")
            node = s.settle(issued["aliases"]["local:coverage"])
            actor = s.next_actor("run")
            packet = actual_packet(s, actor)["packet"]
            source = node["result_ref"]
            entry = next(obs for obs in packet["observations"] if obs["artifact_ref"] == source)
            target = next(row for row in entry["endpoint_rows"] if row["id"] == "coverage")
            unqualified = close_decision([source])
            with self.assertRaisesRegex(SchemaInvalid, "concrete received endpoint"):
                s.call(actor, "gx_decide_revision", {"decision": unqualified}, "only-output")
            qualified = {**unqualified, "metric_refs": [source + "#" + target["key"]]}
            closed = s.call(actor, "gx_decide_revision", {"decision": qualified}, "adopt-endpoint")
            used, = closed["endpoint_use"]["used_metric_refs"]
            self.assertIn(used, closed["endpoint_use"]["received_metric_refs"])
            self.assertEqual(closed["endpoint_use"]["receipts"][used]["source_ref"], source)
            self.assertEqual(s.rows("SELECT job_id FROM jobs"), [{"job_id": node["job_id"]}])

    def test_historical_replay_cannot_submit_new_claims_of_endpoint_use(self):
        with CloseStudy(self.study(), spec_updates={"evidence_policy": "gx.evidence/6.2.2",
                                                    "historical_replay": True}) as s:
            with self.assertRaises((ScopeDenied, SchemaInvalid)):
                s.call(s.lead, "gx_decide_revision", {"decision": comparison_open()}, "historical-with-plan")
            action = close_decision()
            accepted = s.call(s.lead, "gx_decide_revision", {"decision": action}, "historical-close")
            self.assertNotIn("endpoint_use", accepted)
            self.assertNotIn("comparison", accepted)
            self.assertNotIn("used_metric_refs", accepted["close_context"])


if __name__ == "__main__":
    unittest.main()
