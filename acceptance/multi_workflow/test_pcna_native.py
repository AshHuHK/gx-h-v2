"""Register original-bound PCNA endpoints and run the installed Jobs on frozen calibrations."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import PRODUCT, draft
from acceptance.v622.test_close import CloseStudy
from acceptance.v623 import test_close_metrics
from gx_core.artifacts import ArtifactService
from gx_core.p0 import manifest, v6_goal, v621_metrics


PLUGIN = "org.gx.pcna-structure@1.0.0"
GEOMETRY = "pcna-local-geometry@1"
INTERFACE = "pcna-interface@1"
MATERIALS = {
    "1VYM": ("data", "22b94186ce87e4d0b4a8e48c47ed129cea5b9900d5f6b4f9820833e879490b02"),
    "3TBL": ("open-data", "05a1488d313d9926f556f495ad155e3ebf7859b2fa3014bc68ae55465acdb776"),
}
GEOMETRY_PARAMETERS = {"mask": "common_ca", "endpoint": "angle_deg", "effect_minimum": 0.3,
                       "zero_limit": 0.1, "minimum_evaluable": 3}


def prediction(source, entry, capability, output, metric, unit, parameters):
    pair = {"12": "first and second", "23": "second and third"}[parameters["pair"]]
    region = "whole chains" if parameters["block_index"] == -1 else "first label window"
    scope = f"{entry} deposited PCNA trimer, {pair} subunits, {region}."
    return {"statement": f"The measured {metric} is positive in {scope}",
            "rationale": "Test the value on the specified deposited coordinates.",
            "criteria": "Apply {{criterion}} to the declared output.", "scope": scope,
            "limitations": "A measurement on one deposited asymmetric unit.",
            "evidence_refs": [source], "level": "open",
            "prediction": {"target": "analysis", "data_refs": [source], "observable": "/metrics/" + metric,
                           "operator": "gt", "threshold": 0, "method": {"capability": capability,
                           "input_role": "structure", "output": output, "metric_id": metric, "unit": unit,
                           "parameters": parameters}}}


class NativePCNA(unittest.TestCase):
    def check_method(self, entry, capability):
        subdir, digest = MATERIALS[entry]
        path = PRODUCT / "examples" / "pcna" / subdir / (entry + ".cif")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        settings = {"research_effort": "deep", "research_modules": [], "plugins": [PLUGIN],
                    "capabilities": [capability], "input_files": [{"role": "structure", "path": str(path)}]}
        parameters = {"pair": "12", "block_index": -1}
        if capability == GEOMETRY:
            parameters.update(GEOMETRY_PARAMETERS)
            output, table, metric, unit = "geometry.json", "geometry-table.json", "baseline_pair_deg", "deg"
        else:
            output, table, metric, unit = "interface.json", "interface-table.json", "n_contact_residue_pairs", "pairs"
        with tempfile.TemporaryDirectory() as tmp, CloseStudy(Path(tmp) / "goal", spec_updates=settings) as s:
            source, = s.materials
            v6_goal.record_observation(s.conn, s.run_id, source, source=entry,
                                       acquisition=json.loads(path.with_suffix(".json").read_bytes()))
            claim = prediction(source, entry, capability, output, metric, unit, parameters)
            first = s.call(s.lead, "gx_stage_claim", claim, "register")["claim_ref"]
            branch = {"op": "open_branch", "branch": "local:pcna", "hypothesis": claim["statement"],
                      "alternative": "The selected deposited pair has no measurable positive endpoint.",
                      "method_family": capability, "discriminating_observation": metric,
                      "budget_limits": {"cpu_seconds": 400, "job_submissions": 1},
                      "stop_condition": "Read the registered endpoint and the supporting physical table."}
            node = s.node(s.lead, "measure", source, branch_id="local:pcna",
                          capability=capability, parameters=parameters)
            node["question"] = claim["statement"]
            adopted = s.call(s.lead, "gx_decide_revision", {"decision": draft(branch, node, evidence=[source, first],
                watches=[{"event": "job_settled", "subject_ref": "local:measure"}])}, "adopt")
            revision, = adopted["claim_revisions"]
            reg = revision["maturity"]["registration"]
            pin = reg["method_pin"]["measurement_spec"]
            self.assertEqual((pin["capability"], pin["input_role"], pin["parameters"], pin["output_name"],
                              pin["metric_id"], pin["unit"]), (capability, "structure", parameters, output, metric, unit))
            self.assertEqual([d["artifact_ref"] for d in reg["data"]], [source])
            self.assertFalse(any(reg["known_at_registration"].values()))
            result = s.settle(adopted["aliases"]["local:measure"])
            job = result["job_id"]
            sealed = manifest.read_manifest(s.conn, job)
            completion = sealed["completion"]
            self.assertEqual((completion["status"], completion["exit_code"]), ("SUCCEEDED", 0))
            self.assertTrue(completion["started_at"] and completion["finished_at"])
            outputs = {o["output_name"]: o for o in completion["outputs"]}
            self.assertEqual(set(outputs), {output, table})
            self.assertEqual(outputs[output]["artifact_ref"], result["result_ref"])
            assets = ArtifactService(s.conn)
            for published in outputs.values():
                self.assertTrue(assets.verify(published["artifact_ref"])["ok"])
                bound, = published["source_inputs"]
                self.assertEqual((bound["role"], bound["artifact_ref"], bound["sha256"]), ("structure", source, digest))
            summary = assets.read_json(result["result_ref"])
            details = assets.read_json(outputs[table]["artifact_ref"])
            for doc in (summary, details):
                self.assertEqual((doc["input_sha256"], doc["entry"], doc["method"]), (digest, entry, capability))
            self.assertEqual((summary["pair"], summary["block_index"]), ("12", -1))
            if capability == GEOMETRY:
                self.assertEqual(summary[metric], details["baseline"]["12"])
                alternate_value = details["baseline"]["23"]
                secondary_metric, secondary_unit = "noise_resolved_missed_present", "1"
            else:
                self.assertEqual(summary[metric], len(details["pairs"]["12"]["contacting_label_seq_id_pairs"]))
                self.assertAlmostEqual(summary["buried_area_A2"], sum(summary["buried_area_by_chain_A2"]))
                alternate_value = details["pairs"]["23"]["windows"][0][metric]
                secondary_metric, secondary_unit = "buried_area_A2", "Å²"
            self.assertIsNotNone(alternate_value)
            readings = {v["metric_id"]: v for v in v621_metrics.from_output(s.conn, s.run_id, result["result_ref"])}
            endpoint = readings[metric]
            self.assertEqual((endpoint["schema"], endpoint["unit"], endpoint["value"], endpoint["job_id"]),
                             (v621_metrics.SCHEMA, unit, summary[metric], job))
            self.assertEqual((endpoint["scope"]["input_sha256"], endpoint["scope"]["pair"],
                              endpoint["scope"]["block_index"]), (digest, "12", -1))
            secondary = readings[secondary_metric]
            if summary[secondary_metric] is None:
                self.assertEqual(secondary["schema"], v621_metrics.MISSING)
            else:
                self.assertEqual((secondary["schema"], secondary["value"], secondary["unit"]),
                                 (v621_metrics.SCHEMA, summary[secondary_metric], secondary_unit))
            self.assertEqual(s.rows("SELECT status FROM jobs"), [{"status": "SUCCEEDED"}])
            submitted, = s.rows("SELECT event_id FROM events WHERE kind='job.transition' AND subject_id=? "
                                "AND new_state='SUBMITTING'", job)
            self.assertLess(reg["event_id"], submitted["event_id"])
            actor = s.next_actor("run")
            if result["result_ref"] not in s.brief(actor)["evidence_refs"]:
                s.call(actor, "gx_decide_revision", {"decision": draft(watches=[
                    {"event": "job_settled", "subject_ref": result["result_ref"]}])}, "await-output")
                actor = s.next_actor("run")
            latest, = s.brief(actor)["claims"]
            coverage, = latest["maturity"]["material_coverage"]
            available = coverage["endpoint_first_available_event_id"]
            self.assertEqual(coverage["qualification"], "prospective")
            self.assertLess(submitted["event_id"], available)
            self.assertEqual(latest["maturity"]["direction"], "supported" if summary[metric] > 0 else "opposed")
            start = 0
            while start is not None:
                read_key = f"read-{start}"
                page = test_close_metrics.EndpointClose.read_page(self, s, actor, result["result_ref"], start, read_key)
                if any(row["id"] == metric for row in page["endpoint_rows"]):
                    break
                start = page["next_endpoint_offset"]
            else:
                self.fail(f"Selected metric {metric} missing from all delivered pages")
            received, = s.rows("SELECT event_id FROM events WHERE kind='v623.endpoints.received' AND actor=? "
                               "AND json_extract(payload_json,'$.receipt')=?", actor.thread, read_key)
            self.assertGreater(received["event_id"], available)

            # A different pair/window has never been submitted as a scalar Job. The published table
            # already measures it, so a subsequent registration must be identified as retrospective.
            alternate = dict(parameters, pair="23", block_index=-1 if capability == GEOMETRY else 0)
            late = prediction(source, entry, capability, output, metric, unit, alternate)
            staged = s.call(actor, "gx_stage_claim", late, "post-table-prediction")["claim_ref"]
            adopted = s.call(actor, "gx_decide_revision", {"decision": draft(evidence=[source, staged])}, "post-table")
            new, = adopted["claim_revisions"]
            timing, = new["maturity"]["material_coverage"]
            self.assertTrue(timing["known_at_registration"])
            self.assertEqual(timing["qualification"], "retrospective")
            self.assertLess(timing["endpoint_first_available_event_id"], timing["registration_event_id"])
            self.assertFalse(new["maturity"]["results"])
            if folder := os.environ.get("GX_PCNA_NATIVE_RECEIPTS"):
                target = Path(folder)
                target.mkdir(parents=True, exist_ok=True)
                evidence = {"entry": entry, "original_sha256": digest, "run_id": s.run_id, "job_id": job,
                            "capability": capability, "parameters": parameters, "typed_metric": metric,
                            "unit": unit, "value": endpoint["value"], "metric_ref": endpoint["metric_ref"],
                            "secondary_metric": {"id": secondary_metric, "value": summary[secondary_metric],
                                                 "unit": secondary_unit, "reader_schema": secondary["schema"]},
                            "output_hashes": {name: doc["sha256"] for name, doc in outputs.items()},
                            "registered_event": reg["event_id"], "submitted_event": submitted["event_id"],
                            "first_available_event": available, "received_event": received["event_id"],
                            "assessment": latest["maturity"]["direction"],
                            "post_table_parameters": alternate, "post_table_cell": alternate_value,
                            "post_table_coverage": timing, "physical_job_status": completion["status"],
                            "transport": "scripted local native acknowledgement", "paid_model_calls": 0}
                (target / f"native-{entry}-{capability}.json").write_text(json.dumps(evidence, indent=2) + "\n")

    def test_geometry(self):
        for entry in MATERIALS:
            with self.subTest(entry=entry):
                self.check_method(entry, GEOMETRY)

    def test_interface(self):
        for entry in MATERIALS:
            with self.subTest(entry=entry):
                self.check_method(entry, INTERFACE)
