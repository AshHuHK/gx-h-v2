"""Public, local engineering tests for per-output semantics and acquisition provenance."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gx_core import config
from gx_core.artifacts import ArtifactService
from gx_core.capabilities import manifest as capability_manifest
from gx_core.domain import ids
from gx_core.errors import SchemaInvalid, ScopeDenied
from gx_core.jobs.service import JobService
from gx_core.p0 import cli, manifest, v6_decisions, v6_goal, v6_jobs, v62_outputs as outputs
from gx_core.persistence import store
from gx_core.plugins import cli as plugin_cli
from gx_core.plugins.catalog import package_digest
from tests.test_v6_decisions import bind_native_fixture, branch, draft


FIXTURE = Path(__file__).parent / "fixtures/v62_output"
METHOD = "instrument-series@1"
SCOPE = "installed sensor at the registered temperature"


def acquired(sample, **more):
    return dict(data="dataset.v1/" + sample, sample=sample, acquisition="run-" + sample,
                source="archive/" + sample, sampling="series-" + sample,
                publication=[], processing=[], applicability=SCOPE) | more


def start_series_goal(case, conn, tmp, *, effort="quick", run_key="mixed", readings=None):
    deployment = tmp / "release"
    installed = deployment / "plugins/installed"
    shutil.copytree(config.CONFIG_DIR, deployment / "config")
    (deployment / "gx_core").symlink_to(config.HARNESS_ROOT / "gx_core", target_is_directory=True)
    (deployment / "adapters").symlink_to(config.HARNESS_ROOT / "adapters", target_is_directory=True)
    catalog = deployment / "config/workflow_packages.json"
    catalog.write_text('{"plugins":[]}')
    plugin_cli._install(FIXTURE, package_digest(FIXTURE), installed, catalog)
    case.enterContext(patch.object(config, "HARNESS_ROOT", deployment))
    case.enterContext(patch.object(config, "CONFIG_DIR", deployment / "config"))
    case.enterContext(patch.object(capability_manifest, "HARNESS_ROOT", deployment))
    path = tmp / "series.json"
    path.write_text(json.dumps(readings if readings is not None else {"schema": "instrument/1", "readings": [1, 3, 5]}))
    spec = {"mode": "goal", "project": "output-types", "research_effort": effort,
            "objective": "Summarize the observed series and estimate the next reading",
            "plugins": ["org.example.instrument-series@1.0.0"], "capabilities": [METHOD],
            "input_files": [{"role": "readings", "path": str(path)}]}
    run = v6_goal.start(conn, spec, run_key=run_key, current=None)["run_id"]
    raw = json.loads(conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run,)).fetchone()[0])
    return run, raw["files"][0]["artifact_ref"]


class OutputProvenance(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.tmp / "gx.db"
        self.conn = self.enterContext(closing(store.open_store(self.db)))
        self.assets = ArtifactService(self.conn)

    def publish(self, name, body, *, origin="INGEST", kind="instrument_measurement", role=None):
        return self.assets.publish_bytes(namespace=f"{self.run}/test", rel_path=name, run_id=self.run,
                                        kind=kind, data=json.dumps(body), origin=origin, actor="operator",
                                        provenance={"role": role})

    def test_real_mixed_outputs_survive_restart_without_the_package_or_mutable_labels(self):
        self.run, ref = start_series_goal(self, self.conn, self.tmp)
        v6_goal.record_observation(self.conn, self.run, ref, source="sample-s1", acquisition=acquired("s1"))
        call = bind_native_fixture(self.conn, self.run, self.tmp)
        node = dict(op="add_node", branch="local:sample", node="local:both", capability=METHOD,
                    inputs=[ref], parameters={}, depends_on=[], question="Mean and next reading?")
        adopted = v6_decisions.adopt(self.conn, **call, decision=draft(
            {**branch(), "budget_limits": {"job_submissions": 1, "cpu_seconds": 60}}, node))
        node_id = adopted["aliases"]["local:both"]
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            v6_jobs.advance(self.conn, self.run)
            state = self.conn.execute("SELECT status,job_id FROM v6_nodes WHERE node_id=?", (node_id,)).fetchone()
            if state["status"] in v6_jobs.NODE_TERMINAL:
                break
            time.sleep(.05)
        self.assertEqual(state["status"], "SUCCEEDED")
        job = state["job_id"]
        manifest_before = manifest.read_manifest(self.conn, job)
        self.assertEqual(manifest_before["admission"]["schema"], "gx.execution-manifest.admission/2")
        inventory = {v["output_name"]: v for v in outputs.published_outputs(self.conn, self.run, job)}
        self.assertEqual(set(inventory), {"mean.json", "next.json"})
        mean = inventory["mean.json"]["artifact_ref"]
        forecast = inventory["next.json"]["artifact_ref"]
        original = outputs.classify(self.conn, self.run, mean)
        self.assertEqual(self.assets.read_json(mean)["value"], 3)
        self.assertEqual(self.assets.read_json(forecast)["value"], 7)
        self.assertEqual(original["nature"], "analysis")
        self.assertEqual(original["original_observations"], [{"artifact_ref": ref, "sha256": self.assets.describe(ref)["sha256"]}])
        self.assertTrue(original["independence_complete"])
        self.assertEqual(original["dependencies"]["sample"], ["s1"])
        prediction = outputs.classify(self.conn, self.run, forecast)
        self.assertEqual(prediction["nature"], "model_prediction")
        self.assertFalse(prediction["independence_complete"])
        self.assertEqual(prediction["dependencies"]["processing"], original["dependencies"]["processing"])
        for log in manifest_before["completion"]["logs"]:
            self.assertEqual(outputs.classify(self.conn, self.run, log["artifact_ref"])["nature"], "unclassified")
        self.conn.execute("UPDATE artifacts SET provenance_json=? WHERE artifact_ref=?",
                          ('{"nature":"observed","acquisition":"some-other-sample"}', forecast))
        outcome = json.loads(self.conn.execute("SELECT outcome_json FROM jobs WHERE job_id=?", (job,)).fetchone()[0])
        for item in outcome["outputs"]:
            item["nature"] = "observed"
        self.conn.execute("UPDATE jobs SET outcome_json=? WHERE job_id=?", (json.dumps(outcome), job))
        shutil.rmtree(self.tmp / "release/plugins/installed")  # No live package at the cold read.
        with closing(store.open_store(self.db)) as fresh:
            self.assertEqual(manifest.read_manifest(fresh, job), manifest_before)
            self.assertEqual(outputs.classify(fresh, self.run, mean), original)
            self.assertEqual(outputs.classify(fresh, self.run, forecast), prediction)
            projected = {v["output_name"]: v for v in JobService(fresh).status(job)["outputs"]}
            self.assertEqual(projected["next.json"]["nature"], "model_prediction")
            wrong = self.assets.describe(forecast)
            raw_path = self.assets.resolve_path(wrong["namespace"], wrong["rel_path"])
            raw_path.write_bytes(b'{"nature":"observed","value":7}')
            with self.assertRaisesRegex(SchemaInvalid, "digest"):
                outputs.classify(fresh, self.run, forecast)

    def test_late_supply_copies_scope_and_trusted_source(self):
        self.run = v6_goal.start(self.conn, {"mode": "goal", "project": "chronology",
                                            "objective": "Compare sensor readings"}, run_key="mixed", current=None)["run_id"]
        body = {"schema": "sensor/1", "measurement": 4, "nature": "observed"}
        first = self.publish("first.json", body)
        initial = outputs.classify(self.conn, self.run, first.artifact_ref)
        self.assertEqual(initial["nature"], "unclassified")
        copy = self.publish("copy.json", body)
        descriptor = self.publish("description.json", {"sample": "s1"}, kind="run_input")
        metadata = acquired("s1", data_refs=[descriptor.artifact_ref])
        observed = v6_goal.record_observation(self.conn, self.run, copy.artifact_ref, source="external",
                                              acquisition=metadata)
        result = outputs.classify(self.conn, self.run, copy.artifact_ref)
        self.assertEqual(result["first_event_id"], initial["first_event_id"])
        self.assertLess(result["first_event_id"], observed["event_id"])
        self.assertEqual(result["measurement_schema"], "sensor/1")
        self.assertEqual(result["nature"], "observed")
        self.assertTrue(result["independence_complete"])
        self.assertEqual(result["data_refs"], [{"artifact_ref": descriptor.artifact_ref, "sha256": descriptor.sha256}])
        self.assertEqual(outputs.classify(self.conn, self.run, copy.artifact_ref,
                                          through_event=observed["event_id"] - 1)["nature"], "unclassified")
        for origin, kind in (("HOST_PUT", "instrument_measurement"), ("INGEST", "synthetic_observation")):
            forged = self.publish(origin + ".json", {"measurement": 12, "origin": origin}, origin=origin, kind=kind)
            with self.assertRaises(SchemaInvalid):
                v6_goal.record_observation(self.conn, self.run, forged.artifact_ref, source=origin, acquisition=metadata)
        with self.assertRaises(SchemaInvalid):
            outputs.acquisition_receipt(self.conn, self.run, copy.artifact_ref, dict(metadata, independent=True))
        with self.assertRaises(ScopeDenied):
            outputs.classify(self.conn, "different-run", copy.artifact_ref)
        self.assertEqual(outputs.classify(self.conn, self.run, first.artifact_ref)["nature"], "unclassified")
        incomplete = self.publish("unassigned.json", {"schema": "sensor/1", "measurement": 6})
        v6_goal.record_observation(self.conn, self.run, incomplete.artifact_ref, source="lab",
                                   acquisition={"source": "archive.v2", "sample": "unknown"})
        self.assertFalse(outputs.classify(self.conn, self.run, incomplete.artifact_ref)["independence_complete"])

    def test_operator_ingress_preserves_individual_samples_and_inherits_source_dependencies(self):
        self.run = v6_goal.start(self.conn, {"mode": "goal", "project": "aliquots",
                                            "objective": "Evaluate successive sensor readings"},
                                 run_key="samples", current=None)["run_id"]
        path, metadata = self.tmp / "reading.json", self.tmp / "provenance.json"
        path.write_text('{"schema":"portable-sensor/1","reading":5}')

        def supply(source_id, descriptor):
            metadata.write_text(json.dumps(descriptor))
            return cli.execute(SimpleNamespace(command="observe", state=self.tmp, run_id=self.run, scope="run",
                                               source=source_id, input=path, acquisition=metadata))

        observations = []
        for sample in ("one", "two"):
            aliquot = self.publish(sample + ".json", {"aliquot": sample})
            previous = {**acquired(sample), "sample": ["whole-specimen", sample],
                        "publication": "paper:v1", "source": "specimen-collection:v2"}
            v6_goal.record_observation(self.conn, self.run, aliquot.artifact_ref,
                                       source="aliquot-" + sample, acquisition=previous)
            receipt = acquired("sensor-" + sample, data_refs=[aliquot.artifact_ref])
            observed = supply(sample, receipt)
            self.assertEqual(observed, supply(sample, receipt))
            info = outputs.classify(self.conn, self.run, observed["artifact_ref"])
            self.assertEqual(info["measurement_schema"], "portable-sensor/1")
            self.assertEqual(info["dependencies"]["publication"], ["paper:v1"])
            self.assertIn("whole-specimen", info["dependencies"]["sample"])
            self.assertIn("specimen-collection:v2", info["dependencies"]["source"])
            self.assertIn(aliquot.artifact_ref, [item["artifact_ref"] for item in info["original_observations"]])
            observations.append(observed)
        self.assertNotEqual(observations[0]["artifact_ref"], observations[1]["artifact_ref"])
        self.assertEqual(observations[0]["sha256"], observations[1]["sha256"])
        for receipt in observations:
            self.assertEqual(outputs.classify(self.conn, self.run, receipt["artifact_ref"])["nature"], "observed")
        body = {"known": "scripted"}
        fixture = self.publish("fixture.json", body, kind="run_input", role="test_fixture")
        copy = self.publish("relabel.json", body)
        self.assertEqual(outputs.classify(self.conn, self.run, fixture.artifact_ref)["record_kind"], "test_fixture")
        with self.assertRaisesRegex(SchemaInvalid, "fixture"):
            v6_goal.record_observation(self.conn, self.run, copy.artifact_ref, source="relabel",
                                       acquisition=acquired("invented"))
        fresh = self.publish("other.json", {"schema": "temp/1", "reading": 38})
        with self.assertRaisesRegex(SchemaInvalid, "fixture"):
            outputs.acquisition_receipt(self.conn, self.run, fresh.artifact_ref, acquired("aliquot", data_refs=[copy.artifact_ref]))

    def test_legacy_exact_hash_and_declared_directory_members(self):
        from tests.test_v6_jobs import DynamicJobs
        legacy = DynamicJobs()
        self.addCleanup(legacy.doCleanups)
        legacy.setUp()
        legacy.new("legacy")
        job = legacy.settle("legacy")["job_id"]
        frozen = manifest.read_manifest(legacy.conn, job)
        self.assertNotIn("output_declarations", frozen["admission"])
        self.assertEqual(ids.sha256_of_obj(frozen["completion"]), frozen["manifest_hash"])
        for card in outputs.published_outputs(legacy.conn, legacy.run_id, job):
            self.assertEqual(card["nature"], "unclassified")
            result = outputs.classify(legacy.conn, legacy.run_id, card["artifact_ref"])
            self.assertEqual(result["record_kind"], "test_fixture")
            self.assertEqual(result["nature"], "unclassified")
        self.assertEqual(frozen, manifest.read_manifest(legacy.conn, job))
        admission = {"inputs": [], "output_declarations": [
            {"name": "statistics", "kind": "dir:summary", "required": True, "nature": "analysis"}]}
        output = {"output_name": "statistics/series/one.json", "kind": "summary"}
        self.assertEqual(outputs._binding(admission, output)["nature"], "analysis")
        with self.assertRaises(SchemaInvalid):
            outputs._binding(admission, {**output, "output_name": "statistics_copy/one.json"})

    def test_changed_submit_intent_cannot_replace_the_admitted_output_collector(self):
        from tests.test_v6_jobs import DynamicJobs
        scenario = DynamicJobs()
        self.addCleanup(scenario.doCleanups)
        scenario.setUp()
        scenario.new("changed-pin")
        v6_jobs.advance(scenario.conn, scenario.run_id)
        job = scenario.state("changed-pin")["job_id"]
        original = manifest.read_manifest(scenario.conn, job)["admission_hash"]
        scenario.conn.execute("UPDATE jobs SET submit_intent_json=json_set(submit_intent_json,"
                              "'$.manifest_sha256', ?) WHERE job_id=?", ("0" * 64, job))
        final = JobService(scenario.conn).wait(job, timeout_seconds=15)
        self.assertEqual(final["status"], "FAILED")
        self.assertIn("immutable capability pin", final["failure"]["summary"])
        after = manifest.read_manifest(scenario.conn, job)
        self.assertEqual(after["admission_hash"], original)
        self.assertEqual(after["completion"]["outputs"], [])
        diagnostic = final["recovery_manifest_ref"]
        self.assertEqual(outputs.classify(scenario.conn, scenario.run_id, diagnostic)["record_kind"], "execution_failure")


if __name__ == "__main__":
    unittest.main()
