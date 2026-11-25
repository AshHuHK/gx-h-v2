"""Replay the compact public product record and re-exercise both disposition errors."""
import json
from pathlib import Path
import tempfile
import unittest

from acceptance.v622.test_close import CloseStudy
from acceptance.v623.test_decision_action import comparison_open
from acceptance.v62a.interleavings_fixture import draft
from gx_core.domain import ids
from gx_core.errors import SchemaInvalid
from gx_core.p0 import v621_policy, v6_goal


RECEIPT = Path(__file__).with_name("evidence") / "product.json"


def probe(policy):
    with tempfile.TemporaryDirectory() as folder:
        spec = {"evidence_policy": policy, "historical_replay": policy != v621_policy.VERSION,
                "plugins": ["org.gx.protein@0.3.0"], "capabilities": ["deposited-chain-geometry@2"],
                "research_modules": []}
        with CloseStudy(Path(folder) / "study", spec_updates=spec) as study:
            opening = study.call(study.lead, "gx_decide_revision", {"decision": comparison_open()}, "opening")
            observed = v6_goal.record_observation(study.conn, study.run_id, study.materials[-1], source="input-review")
            actor = study.next_actor("run")
            body = dict(draft(), comparison_id=opening["comparison"]["comparison_id"], metric_refs=[])
            result = {"run_id": study.run_id, "opening_event": opening["event_id"],
                      "observation_event": observed["event_id"], "cases": {}}
            for key, form in (("empty_operation", body),
                              ("unknown_id", {**body, "comparison_id": "cmp_unrelated", "disposition": "MAINTAIN"})):
                try:
                    study.call(actor, "gx_decide_revision", {"decision": form}, key)
                except SchemaInvalid as err:
                    case = {"code": err.code, "message": err.message,
                            **{k: err.details[k] for k in ("reason", "field") if k in err.details}}
                    records = study.rows("SELECT result_json FROM p0_root_calls WHERE thread_id=? AND call_id=?",
                                         actor.thread, key)
                    if not records:
                        raise ValueError("Missing durable refusal")
                    case["native_refusal_sha256"] = ids.sha256_of_obj(json.loads(records[0]["result_json"]))
                    current = study.rows("SELECT event_id FROM events WHERE run_id=? AND kind=? AND "
                                         "json_extract(payload_json,'$.submission')=?", study.run_id,
                                         "v624.decision.rejected", ids.sha256_of_obj([actor.thread, actor.turn, key]))
                    case["rejection_event"] = current[0]["event_id"] if current else None
                    result["cases"][key] = case
                else:
                    raise ValueError("An empty operation or unknown comparison was accepted")
            result["accepted_decisions"] = len(study.rows("SELECT 1 FROM v6_decisions"))
            return result


class ProductReplay(unittest.TestCase):
    def test_historical_and_current_probes_use_their_actual_pinned_policy(self):
        archived = json.loads(RECEIPT.read_text())["disposition_probes"]
        for policy, expected in archived.items():
            result = probe(policy)
            self.assertEqual(result["accepted_decisions"], 1)
            for name, case in result["cases"].items():
                for field in ("code", "message", "field", "reason"):
                    self.assertEqual(case.get(field), expected["cases"][name].get(field))
                self.assertEqual(case["rejection_event"] is not None, policy == v621_policy.VERSION)
        historical = archived[v621_policy.V623]["cases"]
        self.assertEqual(historical["empty_operation"]["message"], historical["unknown_id"]["message"])
        current = archived[v621_policy.VERSION]["cases"]
        self.assertEqual(current["empty_operation"]["field"], "ops")
        self.assertEqual(current["unknown_id"]["field"], "comparison_id")

    def test_selected_rows_acceptance_effect_and_replay(self):
        saved = json.loads(RECEIPT.read_text())
        self.assertEqual(saved["model_inference_calls"], 0)
        for key in ("p4", "cheap_job"):
            record = saved[key]
            self.assertEqual(ids.sha256_of_obj(record["submitted"]), record["draft_sha256"])
            self.assertEqual(ids.sha256_of_obj(record["normalized"]), record["normalized_sha256"])
        p4 = saved["p4"]
        self.assertEqual(set(p4["submitted"]), {"ops", "metric_refs", "rationale"})
        self.assertEqual(p4["submitted"]["metric_refs"], [p4["delivered_row"]["key"]])
        self.assertEqual(p4["delivered_row"]["value"], "149/164 (0.909)")
        self.assertEqual(p4["normalized"]["metric_refs"], [p4["metric_ref"]])
        self.assertEqual(p4["normalized"]["evidence_refs"], [p4["source_ref"]])
        self.assertTrue(p4["metric_ref"].removeprefix("metric_").startswith(p4["delivered_row"]["key"]))
        self.assertEqual(p4["measured_endpoint"]["metric_ref"], p4["metric_ref"])
        self.assertEqual(p4["measured_endpoint"]["output_name"], p4["comparison"]["method"]["output_name"])
        self.assertEqual(p4["measured_endpoint"]["display"], p4["delivered_row"]["value"])
        self.assertEqual(p4["normalized"]["comparison_id"], p4["comparison"]["comparison_id"])
        self.assertEqual(p4["normalized"]["ops"][0]["inputs"], p4["comparison"]["reference_refs"])
        self.assertEqual(p4["normalized"]["disposition"], "MAINTAIN")
        self.assertLess(p4["delivery_event"], p4["decision_event"])
        self.assertLess(p4["decision_event"], p4["job_terminal_event"])
        self.assertEqual((p4["dispatch"]["node_id"], p4["dispatch"]["decision_ref"]),
                         (p4["follow_up_job"]["node_id"], p4["decision_ref"]))
        self.assertEqual(p4["follow_up_job"]["status"], "SUCCEEDED")
        self.assertEqual(p4["follow_up_job"]["job_id"], p4["terminal_job_id"])
        self.assertEqual(p4["proof"]["source_ref"], p4["source_ref"])
        self.assertEqual(p4["proof"]["event_id"], p4["delivery_event"])
        self.assertTrue(p4["first_post_delivery_accepted"])
        cheap = saved["cheap_job"]
        self.assertTrue(cheap["cold_replay_same_receipt"])
        self.assertEqual(cheap["jobs_after_replay"], 1)
        self.assertEqual(cheap["events"][1]["payload"]["decision_ref"], cheap["events"][0]["subject_id"])
        self.assertEqual(cheap["events"][1]["event_id"], cheap["wake"]["event_id"])
        self.assertEqual(cheap["events"][1]["payload"]["job_id"], cheap["job"]["job_id"])
        self.assertEqual(cheap["job"]["status"], "SUCCEEDED")
        self.assertEqual(cheap["job"]["result_ref"], cheap["wake"]["received_result"])
        self.assertIn({"event": "job_settled", "subject_ref": cheap["job"]["node_id"]},
                      cheap["wake"]["matched_watches"])
        measured = saved["module_row"]
        self.assertEqual(measured["row"]["value"], "49/164 (0.299)")
        self.assertEqual(measured["used_metric_refs"], [measured["metric_ref"]])
        self.assertTrue(measured["metric_ref"].removeprefix("metric_").startswith(measured["row"]["key"]))
        self.assertEqual(measured["evidence_refs"], [measured["report_ref"]])
        self.assertEqual(measured["proof"]["source_ref"], measured["report_ref"])
        self.assertLess(measured["proof"]["event_id"], measured["decision_event"])
