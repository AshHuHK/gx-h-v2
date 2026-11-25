"""Frozen readout identity and default parameters through one detached two-input Job."""
from contextlib import closing
from fractions import Fraction
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v62a.interleavings_fixture import Scenario, branch, draft
from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid
from gx_core.native_release import native_state
from gx_core.p0 import manifest, v621_gaps, v622_measurement, v62_outputs, v6_goal
from gx_core.persistence import store
from tests import test_v62_outputs as instrument


def paired_instrument(directory):
    fixture = directory / "paired-instrument"
    shutil.copytree(instrument.FIXTURE, fixture)
    card = fixture / "series.json"
    package = json.loads(card.read_bytes())
    method = package["manifest"]
    method["parameter_defaults"] = {"window": "all"}
    method["entry"]["argv_template"] += ["{in.reference}", "{param.window}"]
    definition = {"op": "decimal/1", "reader": "json/1", "pointer": "/value", "unit": "K", "places": 2}
    scaled = {**definition, "source_unit": "K", "unit": "mK", "scale": 1000}
    method["outputs"][0]["metric_specs"] = {"mean": definition, "scaled": scaled}
    method["outputs"][1].update(source_roles=["readings", "reference"], nature="analysis",
                                metric_specs={"difference": definition})
    method["outputs"].append({"name": "sensor.csv", "kind": "file:series_statistic", "nature": "analysis",
        "required": True, "source_roles": ["readings"], "metric_specs": {
            "reading": {**definition, "reader": "csv-row/1", "pointer": "/reading"}}})
    card.write_text(json.dumps(package))
    # The primary scalar comes from one sensor. The difference depends on both sensors.
    (fixture / "measure.py").write_text('''import csv
import json
from pathlib import Path
import sys

def mean(path):
    readings = json.loads(Path(path).read_bytes())["readings"]
    return sum(readings) / len(readings)

source, out, reference, window = sys.argv[1:]
assert window == "all"
out = Path(out)
value = mean(source)
(out / "mean.json").write_text(json.dumps({"schema": "sensor/1", "value": value, "same_number_other_property": value}))
(out / "next.json").write_text(json.dumps({"schema": "difference/1", "value": value - mean(reference)}))
with (out / "sensor.csv").open("w", newline="") as handle:
    csv.writer(handle).writerows([["schema", "reading"], ["sensor/1", value]])
''')
    return fixture


class FrozenMeasurement(unittest.TestCase):
    def test_default_role_multi_metric_reuse_and_gap_identity_survive_reopening(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        study = tmp / "study"
        state = study / "state"
        source = paired_instrument(tmp)
        reference_file = tmp / "reference.json"
        reference_file.write_text('{"readings":[1,3]}')
        with native_state(state), closing(store.open_store(state / "gx.db")) as conn:
            with patch.object(instrument, "FIXTURE", source):
                run, raw = instrument.start_series_goal(self, conn, tmp, effort="deep",
                                                         readings={"readings": [0.06439, 0.06439]},
                    extra_files=[{"role": "reference", "path": str(reference_file)}])
            inputs = json.loads(conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run,)).fetchone()[0])
            reference, = [f["artifact_ref"] for f in inputs["files"] if f["role"] == "reference"]
            for ref, material in ((raw, "sensor"), (reference, "reference")):
                v6_goal.record_observation(conn, run, ref, source=material, acquisition=instrument.acquired(material))

        def prediction(key, *, explicit=False, output="mean.json", schema="sensor/1", observable=None):
            method = {"capability": instrument.METHOD, "output": output, "metric_id": key}
            if explicit:
                method["parameters"] = {"window": "all"}
            return {"method": method, "measurement_schema": schema, "observable": observable or "/metrics/" + key}

        with Scenario(study, reopen=True) as s:
            def submit(actor, tag, params, *, new_branch=False):
                branch_ref = "local:measure" if new_branch else opened["aliases"]["local:measure"]
                node = s.node(actor, tag, raw, branch_id=branch_ref, capability=instrument.METHOD, parameters=params)
                node["inputs"] = [raw, reference]
                ops = [{**branch("measure"), "budget_limits": {"job_submissions": 2, "cpu_seconds": 60}}] if new_branch else []
                event = {"event": "job_settled", "subject_ref": "local:" + tag}
                return s.call(actor, "gx_decide_revision", {"decision": draft(*ops, node, watches=[event])}, tag)

            lead = s.next_actor("run")
            implied = v622_measurement.pin(s.conn, run, prediction("mean"), applicability=instrument.SCOPE)
            explicit = v622_measurement.pin(s.conn, run, prediction("mean", explicit=True), applicability=instrument.SCOPE)
            self.assertEqual(implied, explicit)
            alias = prediction("mean", observable="/value")
            self.assertEqual(v622_measurement.pin(s.conn, run, alias, applicability=instrument.SCOPE), implied)
            coincident = prediction("mean", observable="/same_number_other_property")
            with self.assertRaises(SchemaInvalid):
                v622_measurement.pin(s.conn, run, coincident, applicability=instrument.SCOPE)
            for field, value in (("unit", "kg"), ("metric_id", "unregistered"), ("input_role", "reference")):
                with self.subTest(field=field), self.assertRaises(SchemaInvalid):
                    invalid = prediction("mean")
                    invalid["method"][field] = value
                    v622_measurement.pin(s.conn, run, invalid, applicability=instrument.SCOPE)
            gap = {"question": "Is the sensor readable?", "data_ref": raw, "applicability": instrument.SCOPE,
                   "measurement_schema": "sensor/1", "observable": "/metrics/mean", "nature": "analysis",
                   "op": "gt", "threshold": 0.064, "method": instrument.METHOD, "output_name": "mean.json",
                   "metric_id": "mean", "unit": "K"}
            sources = [{"artifact_ref": raw, "sha256": ArtifactService(s.conn).describe(raw)["sha256"]}]
            criteria = v621_gaps.register(s.conn, {"run_id": run},
                                          {"gaps": [gap["question"]], "gap_criteria": [gap]}, sources)
            criterion = criteria["gap_criteria"][gap["question"]]
            self.assertEqual(criterion["method_pin"], implied)
            opened = submit(lead, "implicit", {}, new_branch=True)
            completed = s.settle(opened["aliases"]["local:implicit"])
            repeat = submit(s.next_actor("run"), "explicit", {"window": "all"})
            reused = s.settle(repeat["aliases"]["local:explicit"])
            self.assertEqual((reused["status"], reused["exec_key"], reused["job_id"]),
                             ("REUSED", completed["exec_key"], completed["job_id"]))
            self.assertEqual(len(s.rows("SELECT job_id FROM jobs WHERE run_id=?", run)), 1)

        with Scenario(study, reopen=True) as s:
            job_id = completed["job_id"]
            proof = manifest.read_manifest(s.conn, job_id)
            self.assertEqual(proof["admission"]["parameters"], {"window": "all"})
            names = {o["output_name"]: o["artifact_ref"] for o in proof["completion"]["outputs"]}

            def measure(spec):
                pin = v622_measurement.pin(s.conn, run, spec, applicability=instrument.SCOPE)
                fact = v62_outputs.classify(s.conn, run, names[spec["method"]["output"]])
                return v622_measurement.match(s.conn, {"method_pin": pin}, fact)

            reason, primary = measure(prediction("mean"))
            self.assertIsNone(reason)
            self.assertEqual(primary["unit"], "K")
            self.assertEqual(v622_measurement.comparison_value(primary), Fraction(6439, 100000))
            self.assertEqual([i["artifact_ref"] for i in primary["material_inputs"]], [raw])
            self.assertEqual([i["role"] for i in primary["source_inputs"]], ["readings"])
            fact = v62_outputs.classify(s.conn, run, names["mean.json"])
            reason, receipt = v621_gaps._match(s.conn, fact, criterion, 0)
            self.assertEqual(reason, "matched")
            self.assertEqual(receipt["measurement"], primary)
            elsewhere = v622_measurement.pin(s.conn, run, prediction("mean"), applicability="another apparatus")
            self.assertEqual(v622_measurement.match(s.conn, {"method_pin": elsewhere}, fact),
                             ("out_of_scientific_scope", None))
            reason, scaled = measure(prediction("scaled", observable="/value"))
            self.assertIsNone(reason)
            self.assertEqual((scaled["value"], scaled["unit"]), (64.39, "mK"))
            self.assertNotEqual(scaled["metric_ref"], primary["metric_ref"])
            reason, pooled = measure(prediction("difference", output="next.json", schema="difference/1"))
            self.assertIsNone(reason)
            self.assertEqual({i["artifact_ref"] for i in pooled["source_inputs"]}, {raw, reference})
            self.assertEqual(pooled["material_inputs"], [])
            reason, csv = measure(prediction("reading", output="sensor.csv"))
            self.assertIsNone(reason)
            self.assertEqual(csv["raw_value"], primary["raw_value"])
            self.assertEqual(measure(prediction("reading", output="sensor.csv", schema="incompatible/1")),
                             ("missing_observable_or_schema", None))
            if destination := os.getenv("GX_MEASUREMENT_DEFAULTS_RECEIPT"):
                Path(destination).write_text(json.dumps({
                    "run_id": run, "job_id": job_id, "exec_key": reused["exec_key"],
                    "reuse_status": reused["status"], "claim_pin": implied, "gap": receipt,
                    "reading": primary, "paired": pooled, "scaled": scaled, "csv": csv,
                    "source_inputs": proof["admission"]["inputs"]}, indent=2) + "\n")
