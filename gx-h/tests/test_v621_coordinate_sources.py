"""Public coordinate inputs travel through the operator, native decision and Job owners."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from gx_core.artifacts import ArtifactService
from gx_core.errors import IdempotencyConflict, SchemaInvalid
from gx_core.p0 import cli, manifest, v6_decisions, v6_goal, v6_jobs, v62_outputs
from gx_core.persistence import store
from tests.test_v6_decisions import bind_native_fixture, branch, draft


ROOT = Path(__file__).resolve().parents[2]
META = ROOT / "examples/v621-coordinate-analysis"
METHOD = "deposited-chain-geometry@2"
MATERIALS = {
    "2RE9": ROOT / "gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif",
    "2O0O": ROOT / "acceptance/v61/adaptive/data/2O0O.cif",
}


class CoordinateSources(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.enterContext(closing(store.open_store(self.tmp / "gx.db")))
        spec = {"mode": "goal", "project": "public-coordinates",
                "objective": "What is the coordinate coverage of the assigned materials?",
                "research_effort": "deep", "research_modules": [], "plugins": ["org.gx.protein@0.3.0"],
                "capabilities": [METHOD], "input_files": [
                    {"role": name, "path": str(path)} for name, path in MATERIALS.items()]}
        self.run_id = v6_goal.start(self.db, spec, run_key="public-source", current=None)["run_id"]
        inputs = json.loads(self.db.execute("SELECT inputs_json FROM runs WHERE run_id=?", (self.run_id,)).fetchone()[0])
        self.refs = {row["role"]: row for row in inputs["files"]}

    def observe(self, name, **overrides):
        pin = self.refs[name]
        args = dict(command="observe", state=self.tmp, run_id=self.run_id, source=name, scope="run",
                    artifact_ref=pin["artifact_ref"], sha256=pin["sha256"], input=None, acquisition=META / (name + ".json"))
        return cli.execute(SimpleNamespace(**(args | overrides)))

    def make_jobs(self, names):
        groups = [dict(branch(key, jobs=1), budget_limits={"job_submissions": 1, "cpu_seconds": 35})
                  for key in names]
        nodes = [dict(op="add_node", branch="local:"+key, node="local:m"+key, capability=METHOD,
                      inputs=[self.refs[key]["artifact_ref"]], parameters={"mask": "ordered_backbone"},
                      depends_on=[], question="How many residue positions meet the same atom selection?") for key in names]
        decision = v6_decisions.adopt(self.db, **bind_native_fixture(self.db, self.run_id, self.tmp),
                                      decision=draft(*groups, *nodes))
        deadline = time.monotonic()+25
        while time.monotonic()<deadline:
            v6_jobs.advance(self.db, self.run_id)
            rows = self.db.execute("SELECT node_id,status,job_id,result_ref FROM v6_nodes WHERE run_id=?", (self.run_id,)).fetchall()
            if rows and all(r["status"] in v6_jobs.NODE_TERMINAL for r in rows):
                break
            time.sleep(.05)
        self.assertEqual([r["status"] for r in rows], ["SUCCEEDED"]*len(names))
        rows = {r["node_id"]: r for r in rows}
        return {key: rows[decision["aliases"]["local:m"+key]] for key in names}

    def test_operator_receipts_real_cpu_jobs_and_admission_cursor(self):
        a = self.observe("2RE9")
        self.assertEqual(a,self.observe("2RE9"))
        changed = self.tmp / "different-source.json"
        changed.write_text(json.dumps(json.loads((META/"2RE9.json").read_text()) | {"publication": "another-publication"}))
        with self.assertRaises(IdempotencyConflict):
            self.observe("2RE9", acquisition=changed)
        with self.assertRaisesRegex(SchemaInvalid,"SHA-256"):
            self.observe("2O0O",sha256="0"*64)
        jobs=self.make_jobs(["2RE9","2O0O"])
        late = self.observe("2O0O")
        self.assertEqual(self.observe("2O0O"),late)
        with closing(store.open_store(self.tmp/"gx.db")) as cold:
            observed = {}
            for entry,row in jobs.items():
                m = manifest.read_manifest(cold,row["job_id"])
                self.assertEqual(m["admission"]["inputs"][0]["artifact_ref"],self.refs[entry]["artifact_ref"])
                self.assertEqual(m["admission"]["inputs"][0]["role"],"structure")
                info=v62_outputs.classify(cold,self.run_id,row["result_ref"])
                self.assertEqual(info["output_name"],"geometry.json")
                self.assertEqual(info["nature"],"analysis")
                self.assertFalse(info["independence_complete"])
                self.assertEqual(info["data_refs"],[{"artifact_ref":self.refs[entry]["artifact_ref"],"sha256":self.refs[entry]["sha256"]}])
                value=ArtifactService(cold).read_json(row["result_ref"])
                self.assertEqual(value["input_sha256"],self.refs[entry]["sha256"])
                self.assertEqual(value["entry"],entry)
                self.assertEqual(value["method"], METHOD)
                observed[entry]=v62_outputs.classify(cold,self.run_id,self.refs[entry]["artifact_ref"])
                if entry=="2RE9":
                    self.assertLessEqual(a["event_id"],m["admission"]["provenance_cursor"])
                    self.assertTrue(info["original_inputs_grounded"])
                    self.assertEqual(info["original_observations"],info["data_refs"])
                    self.assertIn("sampling",info["unknown_dependencies"])
                    self.assertEqual((value["selected_residues"],value["observed_union_residues"]),(49,164))
                else:
                    self.assertGreater(late["event_id"],m["admission"]["provenance_cursor"])
                    self.assertFalse(info["original_inputs_grounded"])
                    self.assertEqual(info["original_observations"],[])
            self.assertEqual(observed["2RE9"]["dependencies"]["publication"],
                             observed["2O0O"]["dependencies"]["publication"])
            self.assertTrue(observed["2RE9"]["dependencies"]["publication"])
            self.assertFalse(observed["2O0O"]["independence_complete"])

    def test_derived_control_and_fixture_cannot_supply_independent_originals(self):
        orig=self.observe("2RE9")
        raw = MATERIALS["2RE9"].read_bytes()+b"# retained coordinate table, revised envelope\n"
        path=self.tmp/"anon.cif"; path.write_bytes(raw)
        shuffled=self.tmp/"control.json"
        pin={k:orig[k] for k in ("artifact_ref","sha256")}
        template=json.loads((META/"2RE9.json").read_text())
        metadata = template | {"derived_from":[pin], "processing":["append-envelope-comment/v1"]}
        shuffled.write_text(json.dumps(metadata))
        args=dict(command="observe",state=self.tmp,run_id=self.run_id,source="control",scope="run",input=path,
                  sha256=hashlib.sha256(raw).hexdigest(),acquisition=shuffled,media_type="chemical/x-mmcif")
        registered=cli.execute(SimpleNamespace(**args))
        self.assertEqual(registered,cli.execute(SimpleNamespace(**args)))
        info=v62_outputs.classify(self.db,self.run_id,registered["artifact_ref"])
        self.assertEqual(info["nature"],"analysis")
        self.assertTrue(info["original_inputs_grounded"])
        self.assertEqual(info["original_observations"],[pin])
        self.assertFalse(info["independence_complete"])
        with self.assertRaisesRegex(SchemaInvalid,"derived bytes"):
            cli.execute(SimpleNamespace(**(args|{"source":"pretend-original","acquisition":META/"2RE9.json"})))
        with self.assertRaisesRegex(SchemaInvalid,"distinct acquired input"):
            shuffled.write_text(json.dumps(metadata|{"derived_from":[pin|{"sha256":"0"*64}]}))
            cli.execute(SimpleNamespace(**(args|{"source":"wrong-parent"})))

    def test_acquisition_only_in_another_branch_cannot_ground_the_measurement(self):
        pending = self.refs["2RE9"]["artifact_ref"]
        measurement = dict(op="add_node", branch="local:consumer", node="local:measure", capability=METHOD,
                           inputs=[pending], parameters={"mask": "common_ca"}, depends_on=[],
                           question="Which positions are comparable across the assigned chains?")
        maker = dict(branch("consumer"), budget_limits={"job_submissions": 1, "cpu_seconds": 35})
        observer = dict(branch("observer"), budget_limits={"job_submissions": 1, "cpu_seconds": 35})
        receipt = v6_decisions.adopt(self.db, **bind_native_fixture(self.db, self.run_id, self.tmp),
                                     decision=draft(maker, observer, measurement))
        observer_id = receipt["aliases"]["local:observer"]
        supplied = self.observe("2RE9", scope=observer_id)
        self.assertEqual(v62_outputs.classify(self.db, self.run_id, pending, scope=observer_id)["nature"], "observed")
        node_id = receipt["aliases"]["local:measure"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            v6_jobs.advance(self.db, self.run_id)
            row = self.db.execute("SELECT status,job_id,result_ref FROM v6_nodes WHERE node_id=?", (node_id,)).fetchone()
            if row["status"] in v6_jobs.NODE_TERMINAL:
                break
            time.sleep(.05)
        self.assertEqual(row["status"], "SUCCEEDED")
        frozen = manifest.read_manifest(self.db, row["job_id"])["admission"]
        self.assertLessEqual(supplied["event_id"], frozen["provenance_cursor"])
        with closing(store.open_store(self.tmp/"gx.db")) as cold:
            for reader in ("run", observer_id, receipt["aliases"]["local:consumer"]):
                self.assertEqual([], v62_outputs.classify(cold, self.run_id, row["result_ref"], scope=reader)
                                 ["original_observations"])

    def test_branch_binding_and_copy_does_not_qualify_the_original_job(self):
        pin=self.refs["2RE9"]
        copy_args=dict(source="different-ingest", artifact_ref=None, input=MATERIALS["2RE9"],media_type="chemical/x-mmcif")
        another=self.observe("2RE9",**copy_args)
        self.assertNotEqual(another["artifact_ref"],pin["artifact_ref"])
        job=self.make_jobs(["2RE9"])["2RE9"]
        result=v62_outputs.classify(self.db,self.run_id,job["result_ref"])
        self.assertEqual(result["original_observations"],[])
        with self.assertRaisesRegex(SchemaInvalid,"scope is unknown"):
            self.observe("2RE9",scope="branch_of_another_run")


class PartialAnalysisLineage(unittest.TestCase):
    def test_one_of_two_actual_inputs_is_acquired(self):
        from shutil import copytree
        from unittest.mock import patch
        from tests import test_v62_outputs as sample

        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        public = copytree(sample.FIXTURE, tmp/"paired-inputs")
        bundle_path = public/"series.json"
        bundle = json.loads(bundle_path.read_text())
        target = bundle["manifest"]
        target["entry"]["argv_template"].insert(-1, "{in.comparison}")
        target["outputs"][0]["source_roles"].append("comparison")
        bundle_path.write_text(json.dumps(bundle))
        worker = public/"measure.py"
        text = worker.read_text().replace("def main(input_path, output_dir)", "def main(input_path, other, output_dir)")
        text = text.replace('    output_dir = Path(output_dir)', '    readings += json.loads(Path(other).read_bytes())["readings"]\n    output_dir = Path(output_dir)')
        worker.write_text(text)

        other = tmp/"other.json"
        other.write_text('{"schema":"instrument/1","readings":[2,4]}')
        original_start = v6_goal.start

        def open_with_both(conn, spec, **kwargs):
            spec["input_files"].append({"role":"comparison","path":str(other)})
            return original_start(conn,spec,**kwargs)

        with closing(store.open_store(tmp/"gx.db")) as db:
            with patch.object(sample, "FIXTURE", public), patch.object(v6_goal, "start", open_with_both):
                rid, _ = sample.start_series_goal(self,db,tmp)
            declared = json.loads(db.execute("SELECT inputs_json FROM runs WHERE run_id=?", (rid,)).fetchone()[0])
            inputs = {f["role"]:f["artifact_ref"] for f in declared["files"]}
            actual = v6_goal.record_observation(db,rid,inputs["readings"],source="instrument-1",
                                                acquisition=sample.acquired("instrument-1"))
            one = v6_jobs.manifest.input_roles({"entry":target["entry"]})
            call = dict(op="add_node", branch="local:sample", node="local:mean", capability=sample.METHOD,
                        inputs=[inputs[role] for role in one], parameters={}, depends_on=[],
                        question="What is the paired summary?")
            adopted = v6_decisions.adopt(db,**bind_native_fixture(db,rid,tmp),decision=draft(
                dict(branch(), budget_limits={"job_submissions":1,"cpu_seconds":35}), call))
            node = adopted["aliases"]["local:mean"]
            deadline = time.monotonic()+15
            while time.monotonic()<deadline:
                v6_jobs.advance(db,rid)
                row = db.execute("SELECT status,result_ref,job_id FROM v6_nodes WHERE node_id=?", (node,)).fetchone()
                if row["status"] in v6_jobs.NODE_TERMINAL:
                    break
                time.sleep(.05)
            self.assertEqual(row["status"],"SUCCEEDED")
            mean = next(o["artifact_ref"] for o in v62_outputs.published_outputs(db,rid,row["job_id"])
                        if o["output_name"] == "mean.json")
            fact = v62_outputs.classify(db,rid,mean)
            self.assertEqual(fact["nature"],"analysis")
            self.assertEqual(fact["original_observations"],[{k:actual[k] for k in ("artifact_ref","sha256")}])
            self.assertFalse(fact["original_inputs_grounded"])
