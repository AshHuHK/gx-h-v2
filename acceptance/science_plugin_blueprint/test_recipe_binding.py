"""Portable recipes retain the provider that bound a source measurement."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from unittest.mock import patch
from gx_core import config
from gx_core.artifacts import ArtifactService
from gx_core.capabilities import manifest, shipped
from gx_core.persistence import store
from gx_core.errors import ScopeDenied
from gx_core.p0 import v6_decisions, v6_goal, v6_jobs, v6_recipes, v62_catalog
from gx_core.plugins import cli as plugin_cli
from gx_core.plugins import goal as plugin_goal
from gx_core.plugins.catalog import package_digest
from tests import test_v62_outputs as series
from tests import test_v6_named_ports as named_ports
from tests.test_v6_decisions import bind_native_fixture, branch, draft
import unittest


class RecipeProviders(unittest.TestCase):
    graph = named_ports.NamedOutputs.graph
    op = named_ports.NamedOutputs.op
    settle = named_ports.NamedOutputs.settle

    def setUp(self):
        self.tmp = Path(self.enterContext(TemporaryDirectory()))
        self.conn = self.enterContext(closing(store.open_store(self.tmp / "gx.db")))
        release = self.tmp / "release"
        shutil.copytree(config.CONFIG_DIR, release / "config")
        (release / "gx_core").symlink_to(config.HARNESS_ROOT / "gx_core", target_is_directory=True)
        (release / "adapters").symlink_to(config.HARNESS_ROOT / "adapters", target_is_directory=True)
        self.enterContext(patch.object(config, "HARNESS_ROOT", release))
        self.enterContext(patch.object(config, "CONFIG_DIR", release / "config"))
        self.enterContext(patch.object(manifest, "HARNESS_ROOT", release))
        settings = release / "config/workflow_packages.json"
        settings.write_text('{"plugins":[]}')
        # This isolated release ships a classified instrument method. Both *real*
        # plugin providers reference those exact bytes and contribute their own bindings.
        shutil.copytree(series.FIXTURE, release / "plugins/installed/instrument-test")
        manifest_path = release / "plugins/installed/instrument-test/series.json"
        bundle = json.loads(manifest_path.read_text())
        bundle["entry_script"] = "plugins/installed/instrument-test/measure.py"
        self.offset_file = manifest_path.with_name("offset.json")
        self.offset_file.write_text("0")
        bundle["manifest"]["required_data"] = ["{harness}/plugins/installed/instrument-test/offset.json"]
        script_path = release / bundle["entry_script"]
        script_path.write_text(script_path.read_text().replace("main(input_path, output_dir)", "main(input_path, output_dir, offset)")
                               .replace("sum(readings) / len(readings)", "sum(readings) / len(readings) + float(Path(offset).read_text())"))
        bundle["manifest"]["entry"]["argv_template"].append("{data.0}")
        script_hash = hashlib.sha256((release / bundle["entry_script"]).read_bytes()).hexdigest()
        bundle["manifest"]["source"] = {"path":"{harness}/plugins/installed/instrument-test/measure.py", "sha256":script_hash}
        manifest_path.write_text(json.dumps(bundle))
        release_pin = shipped.portable_digest({**bundle,"entry_script":{"path":bundle["entry_script"],"sha256":script_hash}})
        self.enterContext(patch.dict(shipped.MANIFESTS, {series.METHOD: "plugins/installed/instrument-test/series.json"}))
        pins_path = release / "config/shipped.json"
        pins = json.loads(pins_path.read_text())
        pins["capabilities"][series.METHOD] = release_pin
        pins_path.write_text(json.dumps(pins))
        primary = self.tmp / "primary"
        primary.mkdir()
        owner = json.loads((series.FIXTURE / "gx_plugin.json").read_text())
        owner["extensions"].append({"id": "readings", "contract": "goal_method", "version": "1.0"})
        (primary / "gx_plugin.json").write_text(json.dumps(owner))
        (primary / "plugin.py").write_text(
            'def bind(ctx, cap, refs, parameters):\n    return {"readings":refs[0]}, parameters\n'
            'def register(ctx):\n    ctx.capabilities.add_manifest("instrument-series@1")\n'
            '    ctx.goal_methods.register("instrument-series@1", bind=bind)\n')
        plugin_cli._install(primary, package_digest(primary), release / "plugins/installed", settings)
        specimen = self.tmp / "original.json"
        specimen.write_text('{"schema":"instrument/1","readings":[0,2,4]}')
        first = {"mode":"goal","project":"output-types","research_effort":"quick",
                 "plugins":["org.example.instrument-series@1.0.0"],"capabilities":[series.METHOD],
                 "objective":"Assess the sensor readings.","input_files":[{"role":"readings","path":str(specimen)}]}
        self.run = v6_goal.start(self.conn, first, run_key="original", current=None)["run_id"]
        self.raw, = self.conn.execute("SELECT json_extract(inputs_json,'$.files[0].artifact_ref') FROM runs WHERE run_id=?", (self.run,)).fetchone()
        self.call = bind_native_fixture(self.conn, self.run, self.tmp)
        self.first = first

    def test_same_method_manifest_different_registered_provider(self):
        source = v6_decisions.adopt(self.conn, **self.call, decision=self.graph([]))["aliases"]["local:split"]
        self.settle([source], self.conn)
        original = {"recipe_id": "detector-envelope", "version": "1", "title": "Bounds of readings",
                    "id_alignment": "Use this instrument series.", "missing_values": "Omit none.",
                    "units": "counts", "nodes": [{"slot": "summarize", "node_id": source}],
                    "inputs": [{"name": "readings", "artifact_ref": self.raw}],
                    "decision_point": {"step": "summarize", "output": "mean.json", "question": "Is a repeat needed?",
                                       "actions": ["repeat", "retain"], "stop_condition": "The observations suffice."}}
        card = v6_recipes.save(self.conn, self.run, original)
        # Re-save a previously published definition whose digest predates provider pins.
        prior_fields = {**original, "recipe_id": "legacy-detector-envelope"}
        old_definition, old_sources = v6_recipes.prepare(self.conn, self.run, prior_fields)
        old_definition.pop("provider_pins")
        legacy = v6_recipes.publish(self.conn, self.run, old_definition, old_sources)
        self.assertEqual(legacy, v6_recipes.save(self.conn, self.run, prior_fields))
        target = self.tmp / "paired-series-provider"
        target.mkdir()
        donor = json.loads((series.FIXTURE / "gx_plugin.json").read_text())
        donor.update(id="org.example.paired-series", extensions=[*donor["extensions"],
                     {"id":"paired-series","contract":"goal_method","version":"1.0"}])
        (target / "gx_plugin.json").write_text(json.dumps(donor))
        (target / "plugin.py").write_text(
            'def check(ctx, method, inputs, parameters, *, producers):\n'
            '    if len(inputs) != 1:\n        raise ValueError("Supply one instrument record")\n'
            '    if inputs[0] not in producers and len(ctx.read_json(inputs[0])["readings"]) != 2:\n'
            '        raise ValueError("The comparison requires exactly two readings")\n'
            'def register(ctx):\n    ctx.capabilities.add_manifest("instrument-series@1")\n'
            '    ctx.goal_methods.register("instrument-series@1", validate=check)\n')
        plugin_cli._install(target, package_digest(target), config.HARNESS_ROOT / "plugins/installed",
                            config.CONFIG_DIR / "workflow_packages.json")
        specimen = self.tmp / "paired.json"
        specimen.write_text('{"readings":[0,4]}')
        goal = {"mode": "goal", "project": "output-types", "research_effort": "quick",
                "plugins": ["org.example.paired-series@1.0.0"], "capabilities": [series.METHOD],
                "objective": "Assess the sensor readings.", "input_files": [{"role":"readings","path":str(specimen)}]}
        rid = v6_goal.start(self.conn, goal, run_key="replacement-provider", current=None)["run_id"]
        raw, = self.conn.execute("SELECT json_extract(inputs_json,'$.files[0].artifact_ref') FROM runs WHERE run_id=?", (rid,)).fetchone()
        left, = plugin_goal.catalog(self.conn, self.run)
        right, = plugin_goal.catalog(self.conn, rid)
        self.assertEqual(left["portable_sha256"], right["portable_sha256"])
        self.assertNotEqual(left["plugin"], right["plugin"])
        args = {"recipe": card["reference"], "digest": card["digest"], "branch": "local:sample",
                "prefix":"second", "inputs":{"readings":raw}}
        with self.assertRaisesRegex(ScopeDenied, "provider"):
            v6_recipes.instantiate(self.conn, rid, args)
        brief = {"scope":"run","run_id":rid,"evidence_policy":"gx.evidence/6.2.4"}
        matches = v62_catalog.query(self.conn, rid, brief, {"kind":"recipe","reference":card["reference"]})
        self.assertEqual(matches["spec"]["status"], "requires_method")
        marker = {"recipe":card["reference"],"digest":card["digest"],"step":"summarize"}
        op = dict(self.op("read", self.raw), inputs=[raw], recipe_decision=marker)
        with self.assertRaisesRegex(ScopeDenied, "provider"):
            v6_decisions.adopt(self.conn, **bind_native_fixture(self.conn, rid, self.tmp), decision=draft(branch(), op))

    def test_calibration_with_an_ordinary_read_only_binder(self):
        change = self.graph([])
        other = self.op("calibrate", self.raw)
        other["independent_repeat"] = True
        change["ops"].append(other)
        aliases = v6_decisions.adopt(self.conn, **self.call, decision=change)["aliases"]
        self.settle([aliases["local:split"], aliases["local:calibrate"]], self.conn)
        recipe = v6_recipes.save(self.conn, self.run, {
            "recipe_id":"sensor-reference", "version":"1", "title":"Check the sensor and compare",
            "units":"counts", "id_alignment":"Ordered readings", "missing_values":"Omit none.",
            "nodes":[{"slot":s, "node_id":aliases["local:"+s]} for s in ("calibrate","split")],
            "inputs":[{"name":"series","artifact_ref":self.raw}],
            "calibration":{"step":"calibrate","scope":"reference instrument"}})
        rid = v6_goal.start(self.conn, self.first, run_key="next-shift", current=None)["run_id"]
        ref, = self.conn.execute("SELECT json_extract(inputs_json,'$.files[0].artifact_ref') FROM runs WHERE run_id=?", (rid,)).fetchone()
        request = {"recipe":recipe["reference"],"digest":recipe["digest"],"branch":"local:sample",
                   "inputs":{"series":ref}, "prefix":"replay", "calibration_scope":"reference instrument"}
        expansion = v6_recipes.instantiate(self.conn, rid, request)
        self.assertEqual("referenced", expansion["calibration"]["status"])
        self.assertEqual(["local:replay-split"], [step["node"] for step in expansion["ops"]])
        self.assertEqual(2, len(v6_recipes.instantiate(self.conn, rid, {**request, "independent_repeat":True})["ops"]))

    def test_parked_measurement_detects_changed_calibration_asset(self):
        change = self.graph([])
        change["ops"].append(self.op("parked", self.raw))
        aliases = v6_decisions.adopt(self.conn, **self.call, decision=change)["aliases"]
        split, parked = aliases["local:split"], aliases["local:parked"]
        original = v6_jobs._prospective

        def hold_pending(db, request):
            if request["binding"]["node_id"] == parked:
                from gx_core.errors import HoldActive
                raise HoldActive("Held after graph compilation")
            return original(db, request)

        with patch.object(v6_jobs, "_prospective", hold_pending):
            self.settle([split], self.conn)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM v6_job_attempts WHERE node_id=?", (parked,)).fetchone())
        self.offset_file.write_text("11")
        with closing(store.open_store(self.tmp / "gx.db")) as resumed:
            v6_jobs.advance(resumed, self.run)
            status, ref = resumed.execute("SELECT status,result_ref FROM v6_nodes WHERE node_id=?", (parked,)).fetchone()
            self.assertEqual((status, ref), ("FAILED", None))
            self.assertEqual(1, resumed.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?", (self.run,)).fetchone()[0])

    def test_unfingerprinted_data_runs_a_fresh_scientific_job(self):
        # No stable digest is computed for a mutable data file above the small-file limit.
        # Its effect on the instrument record can still be observed in a new measurement.
        self.offset_file.write_text("0" + " " * (1024 * 1024))
        change = self.graph([])
        change["ops"].append(self.op("followup", self.raw))
        aliases = v6_decisions.adopt(self.conn, **self.call, decision=change)["aliases"]
        source, followup = aliases["local:split"], aliases["local:followup"]
        original = v6_jobs._prospective

        def delayed(db, request):
            if request["binding"]["node_id"] == followup:
                from gx_core.errors import HoldActive
                raise HoldActive("Wait for the source measurement")
            return original(db, request)

        with patch.object(v6_jobs, "_prospective", delayed):
            self.settle([source], self.conn)
        self.offset_file.write_text("11" + " " * (1024 * 1024))
        next_result = self.settle([followup], self.conn)[followup]
        self.assertEqual("SUCCEEDED", next_result["status"])
        self.assertEqual(13, ArtifactService(self.conn).read_json(next_result["result_ref"])["value"])
        self.assertEqual(2, self.conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?", (self.run,)).fetchone()[0])

    def test_changed_data_before_first_job_admission(self):
        node = v6_decisions.adopt(self.conn, **self.call, decision=self.graph([]))["aliases"]["local:split"]
        from gx_core.errors import HoldActive
        with patch.object(v6_jobs, "_prospective", side_effect=HoldActive("Wait for a measurement slot")):
            v6_jobs.advance(self.conn, self.run)
        self.offset_file.write_text("11")
        v6_jobs.advance(self.conn, self.run)
        status, ref = self.conn.execute("SELECT status,result_ref FROM v6_nodes WHERE node_id=?", (node,)).fetchone()
        self.assertEqual((status, ref), ("FAILED", None))
        self.assertEqual(0, self.conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?", (self.run,)).fetchone()[0])
