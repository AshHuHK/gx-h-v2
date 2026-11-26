"""A scripted Lead preregisters a signed endpoint on generated coordinates and closes on a real Job."""
import hashlib
import json
import os
from statistics import median
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v62a import interleavings_fixture
from acceptance.v62a.interleavings_fixture import draft
from acceptance.v622.test_close import CloseStudy
from acceptance.v623.test_block_method import METHOD as FULL_TABLE, PACKAGE as FULL_TABLE_PLUGIN, synthetic_cif
from acceptance.v623 import test_close_metrics
from acceptance.v6.export import capture
from acceptance.v6.project_window import Capture
from gx_core.artifacts import ArtifactService
from gx_core.p0 import v6_goal, v621_metrics


METHOD = "deposited-block-signed@1"
PLUGIN = "org.gx.block-signed@1.0.0"
PARAMETERS = {"mask": "common_ca", "endpoint": "angle_deg", "effect_minimum": 0.3,
              "zero_limit": 0.1, "minimum_evaluable": 3}
SCOPE = "Generated coordinates, model one, entity one, label chains A, B and C."


def signed_claim(target):
    pred = {"target": "analysis", "observable": "/metrics/missed_present", "operator": "eq", "threshold": 1,
            "data_refs": [target], "method": {"capability": METHOD, "output": "signed-summary.json",
            "input_role": "structure", "metric_id": "missed_present", "unit": "1", "parameters": PARAMETERS}}
    return {"statement": "The median misses a qualifying chain-pair change in the target coordinates.",
            "criteria": "Evaluate the selected chain set and apply {{criterion}}.", "scope": SCOPE,
            "limitations": "Controlled synthetic coordinates only.", "rationale": "Inspect the declared scalar.",
            "evidence_refs": [target], "level": "open", "prediction": pred}


class SignedNative(unittest.TestCase):
    def run_case(self, case, target_bytes, observed_bytes, expected_value, expected_direction):
        receipts = os.environ.get("GX_SIGNED_NATIVE_RECEIPTS")
        if receipts:
            receipts = Path(receipts).resolve()
            receipts.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix=case + "-", dir=receipts))
        else:
            work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        raw = [target_bytes] if target_bytes == observed_bytes else [target_bytes, observed_bytes]
        inputs = []
        for i, data in enumerate(raw):
            material = work / f"generated-{i}.cif"
            material.write_bytes(data)
            inputs.append({"role": "structure", "path": str(material)})
        settings = {"research_effort": "deep", "research_modules": [], "plugins": [PLUGIN],
                    "capabilities": [METHOD], "input_files": inputs}
        goal = "Test whether a three-pair median misses a signed local rotation change in the generated coordinates."
        self.enterContext(patch.object(interleavings_fixture, "GOAL", goal))
        with CloseStudy(work / "goal", spec_updates=settings) as s:
            target, measured = s.materials[0], s.materials[-1]
            for ref, data in zip(s.materials, raw, strict=True):
                digest = hashlib.sha256(data).hexdigest()
                v6_goal.record_observation(s.conn, s.run_id, ref, source="generated-" + digest[:12],
                    acquisition={"source": "synthetic:" + digest, "acquisition": "deterministic coordinate generator",
                                 "data": "coordinate-bytes:" + digest, "applicability": SCOPE})
            claim = signed_claim(target)
            ref = s.call(s.lead, "gx_stage_claim", claim, "register-prediction")["claim_ref"]
            budget = {"op": "open_branch", "branch": "local:signed",
                      "hypothesis": claim["statement"], "alternative": "The median represents each qualifying change.",
                      "method_family": "coordinate geometry", "discriminating_observation": "The signed omission indicator.",
                      "budget_limits": {"cpu_seconds": 120, "job_submissions": 1},
                      "stop_condition": "Close after classifying the result or identifying insufficient evidence."}
            node = s.node(s.lead, "measure", measured, branch_id="local:signed", capability=METHOD,
                          parameters=PARAMETERS)
            node["question"] = "Does the median miss any qualifying signed local rotation change?"
            register = s.call(s.lead, "gx_decide_revision", {"decision": draft(budget, node,
                evidence=[*s.materials, ref], watches=[{"event": "job_settled", "subject_ref": "local:measure"}])},
                "adopt-and-measure")
            prior, = register["claim_revisions"]
            reg = prior["maturity"]["registration"]
            self.assertEqual([v["artifact_ref"] for v in reg["data"]], [target])
            self.assertFalse(any(reg["known_at_registration"].values()))
            spec = reg["method_pin"]["measurement_spec"]
            self.assertEqual((spec["capability"], spec["parameters"], spec["input_role"], spec["output_name"],
                              spec["metric_id"], spec["unit"], spec["observable"], spec["applicability"]),
                             (METHOD, PARAMETERS, "structure", "signed-summary.json", "missed_present", "1",
                              "/metrics/missed_present", SCOPE))
            result = s.settle(register["aliases"]["local:measure"])
            submitted, = s.rows("SELECT event_id FROM events WHERE subject_id=? AND kind='job.transition' "
                                "AND new_state='SUBMITTING'", result["job_id"])
            self.assertLess(reg["event_id"], submitted["event_id"])
            output = result["result_ref"]
            actor = s.next_actor("run")
            if output not in s.brief(actor)["evidence_refs"]:
                s.call(actor, "gx_decide_revision", {"decision": draft(
                    watches=[{"event": "job_settled", "subject_ref": output}])}, "await-published-output")
                actor = s.next_actor("run")
            self.assertIn(output, s.brief(actor)["evidence_refs"])
            document = s.call(actor, "gx_read_artifact", {"ref": output}, "inspect-signed-summary")
            self.assertEqual(document["missed_present"], expected_value)
            self.assertIs(type(document["missed_present"]), type(expected_value))
            self.assertEqual(document["input_sha256"], hashlib.sha256(observed_bytes).hexdigest())
            metrics = {v["metric_id"]: v for v in v621_metrics.from_output(s.conn, s.run_id, output)}
            scalar = metrics["missed_present"]
            self.assertEqual([v["artifact_ref"] for v in scalar["inputs"]], [measured])
            self.assertEqual(scalar["schema"], v621_metrics.MISSING if expected_value is None else v621_metrics.SCHEMA)
            if expected_value is None:
                self.assertEqual(document["blocks_evaluable"], 0)
                self.assertEqual(document["unavailable_reason"], "too_few_evaluable_blocks")
            else:
                self.assertEqual(scalar["value"], expected_value)
            # The host-confirmed page is the delivery used by the closing decision.
            used = metrics["blocks_evaluable"] if expected_value is None else scalar
            start = 0
            while start is not None:
                page = test_close_metrics.EndpointClose.read_page(self, s, actor, output, start, f"receive-{start}")
                if any(row["id"] == used["metric_id"] for row in page["endpoint_rows"]):
                    break
                start = page["next_endpoint_offset"]
            else:
                self.fail(f"Named endpoint {used['metric_id']} was never delivered")
            latest, = s.brief(actor)["claims"]
            maturity = latest["maturity"]
            self.assertEqual(maturity["direction"], expected_direction)
            coverage, = maturity["material_coverage"]
            if expected_direction in ("supported", "opposed"):
                self.assertEqual(coverage["qualification"], "prospective")
                self.assertLess(reg["event_id"], coverage["endpoint_first_available_event_id"])
                self.assertEqual([r["artifact_ref"] for r in maturity["results"]], [output])
            else:
                self.assertFalse(maturity["results"])
                self.assertIsNone(coverage["endpoint_first_available_event_id"])
            reason = {"supported": "The measured target supports the registered prediction.",
                      "opposed": "The measured target refutes the registered prediction.",
                      "undetermined": "The registered target has no qualifying measurement."}[expected_direction]
            close = draft({"op": "close_run", "reason": reason,
                          "claim_refs": [latest["claim_revision_id"]],
                          "unresolved": [reason] if expected_direction == "undetermined" else []},
                          evidence=[output, latest["claim_revision_id"]])
            close.update(rationale=reason, metric_refs=[used["metric_ref"]])
            closed = s.call(actor, "gx_decide_revision", {"decision": close}, "close-on-reassessment")
            s.advance()
            self.assertEqual(s.rows("SELECT status FROM v6_goals"), [{"status": "CLOSED"}])
            self.assertEqual(closed["close_context"]["used_metric_refs"], [used["metric_ref"]])
            self.assertEqual(s.rows("SELECT status FROM jobs"), [{"status": "SUCCEEDED"}])
            out = work / "export"
            capture(s.state / "gx.db", s.run_id, out)
            saved = json.loads(Capture(out).decisions[closed["decision_ref"]]["receipt_json"])
            self.assertEqual(saved["close_context"]["used_metric_refs"], [used["metric_ref"]])
            if receipts:
                evidence = {"case": case, "run_id": s.run_id, "registration": reg, "measured_ref": measured,
                            "measured_input_sha256": document["input_sha256"], "job_id": result["job_id"],
                            "output_ref": output, "output_sha256": ArtifactService(s.conn).describe(output)["sha256"],
                            "summary": document, "scalar_reader": scalar, "claim_direction": maturity["direction"],
                            "claim_material_coverage": coverage, "reassessment_gaps": maturity["gaps"],
                            "delivered_metric": used, "closing_decision": closed["decision_ref"],
                            "closed_metric_refs": saved["close_context"]["used_metric_refs"],
                            "close_events": s.rows("SELECT event_id,kind FROM events WHERE kind IN "
                                "('v6.claim.adopted','v62.claim.reassessed','v623.endpoints.received','v6.run.closed')"),
                            "state": str(s.state.relative_to(receipts)), "export": str(out.relative_to(receipts)),
                            "registered_event": reg["event_id"], "job_submission_event": submitted["event_id"],
                            "scripted_transport": True, "paid_model_calls": 0}
                (receipts / (case + ".json")).write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")

    def test_positive(self):
        raw = synthetic_cif()
        self.run_case("positive", raw, raw, 1, "supported")

    def test_negative(self):
        raw = synthetic_cif(deformed=False)
        self.run_case("negative", raw, raw, 0, "opposed")

    def test_missing(self):
        raw = synthetic_cif(deformed=False, collinear=True)
        self.run_case("missing", raw, raw, None, "undetermined")

    def test_positive_output_on_wrong_structure(self):
        self.run_case("wrong-structure", synthetic_cif(deformed=False), synthetic_cif(), 1, "undetermined")

    def test_existing_full_table_only_dates_a_derivable_signed_value(self):
        controls = (("known", synthetic_cif(), True),
                    ("missing", synthetic_cif(deformed=False, collinear=True), False))
        for name, raw, known in controls:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                source = directory / "material.cif"
                source.write_bytes(raw)
                settings = {"research_effort": "deep", "research_modules": [],
                            "plugins": [FULL_TABLE_PLUGIN, PLUGIN], "capabilities": [FULL_TABLE, METHOD],
                            "input_files": [{"role": "structure", "path": str(source)}]}
                with CloseStudy(directory / "goal", spec_updates=settings) as s:
                    target, = s.materials
                    digest = hashlib.sha256(raw).hexdigest()
                    v6_goal.record_observation(s.conn, s.run_id, target, source=name, acquisition={
                        "source": "generated:" + digest, "acquisition": "coordinate generator",
                        "data": "coordinate-bytes:" + digest, "applicability": SCOPE})
                    path = {"op": "open_branch", "branch": "local:prior",
                            "hypothesis": "A chain may rotate differently from the median.",
                            "alternative": "All pairs and the median change together.",
                            "method_family": "coordinate geometry",
                            "discriminating_observation": "The complete pairwise fits for each interval.",
                            "budget_limits": {"cpu_seconds": 120, "job_submissions": 1},
                            "stop_condition": "Record the fit before interpreting its implications."}
                    node = s.node(s.lead, "table", target, branch_id="local:prior", capability=FULL_TABLE,
                                  parameters={"mask": PARAMETERS["mask"]})
                    plan = s.call(s.lead, "gx_decide_revision", {"decision": draft(path, node, watches=[
                        {"event": "job_settled", "subject_ref": "local:table"}])}, "measure-full-table")
                    job = s.settle(plan["aliases"]["local:table"])
                    table = ArtifactService(s.conn).read_json(job["result_ref"])
                    self.assertEqual((table["input_sha256"], table["eligible_blocks"]), (digest, 5))
                    pairs = ("AB", "AC", "BC")
                    before = [table["baseline"][pair]["angle_deg"] for pair in pairs]
                    if known:
                        after = [table["blocks"][3]["pairs"][pair]["angle_deg"] for pair in pairs]
                        # Actual old table yields an opposed interval. The median and AC decrease, BC increases.
                        self.assertLess(median(after) - median(before), -PARAMETERS["effect_minimum"])
                        self.assertGreater(after[2] - before[2], PARAMETERS["effect_minimum"])
                        self.assertTrue(all(b["pairs"][p]["angle_deg"] is not None for b in table["blocks"] for p in pairs))
                    else:
                        self.assertEqual(before, [None] * 3)
                        self.assertTrue(all(b["pairs"][p]["angle_deg"] is None for b in table["blocks"] for p in pairs))
                    actor = s.next_actor("run")
                    proposed = s.call(actor, "gx_stage_claim", signed_claim(target), "late-proposal")["claim_ref"]
                    adopted = s.call(actor, "gx_decide_revision", {"decision": draft(evidence=[target, proposed])},
                                     "register-after-full-table")
                    claim, = adopted["claim_revisions"]
                    timing, = claim["maturity"]["material_coverage"]
                    produced, = s.rows("SELECT event_id FROM events WHERE kind='job.transition' AND new_state='SUCCEEDED' "
                                       "AND subject_id=?", job["job_id"])
                    self.assertLess(produced["event_id"], timing["registration_event_id"])
                    self.assertEqual(timing["known_at_registration"], known)
                    if known:
                        self.assertEqual(timing["qualification"], "retrospective")
                        self.assertLessEqual(produced["event_id"], timing["endpoint_first_available_event_id"])
                        self.assertLess(timing["endpoint_first_available_event_id"], timing["registration_event_id"])
                    else:
                        self.assertEqual(timing["qualification"], "unmeasured")
                        self.assertIsNone(timing["endpoint_first_available_event_id"])
                    self.assertFalse(claim["maturity"]["results"])
                    self.assertEqual(s.rows("SELECT capability_id,status FROM jobs"),
                                     [{"capability_id": "deposited-block-influence", "status": "SUCCEEDED"}])
                    if folder := os.environ.get("GX_SIGNED_NATIVE_RECEIPTS"):
                        folder = Path(folder)
                        folder.mkdir(parents=True, exist_ok=True)
                        receipt = {"source_sha256": digest, "run_id": s.run_id, "legacy_job": job["job_id"],
                                   "legacy_full_table_sha256": ArtifactService(s.conn).describe(job["result_ref"])["sha256"],
                                   "legacy_completion_event": produced["event_id"], "eligible": table["eligible_blocks"],
                                   "angle": {"baseline": before, "after_by_block": [[b["pairs"][p]["angle_deg"]
                                             for p in pairs] for b in table["blocks"]]},
                                   "signed_registration": claim["maturity"]["registration"]["prediction"],
                                   "signed_coverage": timing, "paid_model_calls": 0}
                        (folder / f"legacy-full-table-{name}.json").write_text(json.dumps(receipt, indent=2) + "\n")
