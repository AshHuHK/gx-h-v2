"""Exercise the two consumers of a three-output scientific Job on the actual Goal graph."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid, VersionImmutable
from gx_core.p0 import manifest, v6_decisions, v6_goal, v6_jobs, v6_native, v6_recipes, v621_policy, v62_catalog, v62_outputs
from gx_core.persistence import store
from tests import test_v62_outputs as series
from tests.test_v6_decisions import bind_native_fixture, branch, draft


class NamedOutputs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.tmp / "gx.db"
        self.conn = self.enterContext(closing(store.open_store(self.db)))
        # All three files satisfy the consumer's series schema and deliberately have distinct values.
        fixture = self.tmp / "plugin"
        shutil.copytree(series.FIXTURE, fixture)
        (fixture / "measure.py").write_text(
            'import json, sys\nfrom pathlib import Path\n'
            'values = json.loads(Path(sys.argv[1]).read_bytes())["readings"]\n'
            'for name, value in zip(("mean", "next", "optional"), (sum(values), min(values), max(values))):\n'
            '    (Path(sys.argv[2]) / (name + ".json")).write_text(json.dumps({"readings":[value]}))\n')
        path = fixture / "series.json"
        desc = json.loads(path.read_bytes())
        for out in desc["manifest"]["outputs"]:
            out.update(required=True, nature="analysis", source_roles=["readings"])
        path.write_text(json.dumps(desc))
        self.enterContext(patch.object(series, "FIXTURE", fixture))
        self.run, self.raw = series.start_series_goal(self, self.conn, self.tmp)
        self.call = bind_native_fixture(self.conn, self.run, self.tmp)

    def op(self, name, ref, output=None):
        spec = dict(op="add_node", branch="local:sample", node="local:" + name, capability=series.METHOD,
                    inputs=[ref], parameters={}, depends_on=[] if ref == self.raw else [ref],
                    question="What are the numerical bounds?")
        if output:
            spec["input_ports"] = [{"index": 0, "output": output}]
        return spec

    def graph(self, ports):
        return draft({**branch(), "budget_limits": {"job_submissions": 3, "cpu_seconds": 60}},
                     self.op("split", self.raw), *(self.op(label, "local:split", name) for label, name in ports))

    def settle(self, nodes, conn):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            v6_jobs.advance(conn, self.run)
            rows = {r["node_id"]: r for r in conn.execute("SELECT * FROM v6_nodes WHERE run_id=?", (self.run,))}
            if all(rows[n]["status"] in v6_jobs.NODE_TERMINAL for n in nodes):
                return rows
            time.sleep(.05)
        self.fail("Graph failed to settle within deadline")

    def test_named_outputs_recover_and_preserve_original_job(self):
        body = self.graph([("second", "next.json"), ("third", "optional.json")])
        answer = v6_decisions.adopt(self.conn, **self.call, decision=body)
        nodes = {key.removeprefix("local:"): nid for key, nid in answer["aliases"].items()}
        nodes.pop("sample")
        # Admit the producer, stop polling and reopen the store with one active Job.
        v6_jobs.advance(self.conn, self.run)
        self.conn.close()
        with closing(store.open_store(self.db)) as db:
            # The Job writer seals the reordered publication list normally, including its digest.
            publish = v62_outputs.bind_published
            def reverse_output_inventory(admission, outputs):
                outputs.reverse()
                publish(admission, outputs)
            with patch.object(v62_outputs, "bind_published", side_effect=reverse_output_inventory):
                rows = self.settle(nodes.values(), db)
            proof = manifest.read_manifest(db, rows[nodes["split"]]["job_id"])
            self.assertNotEqual([out["output_name"] for out in proof["completion"]["outputs"]],
                                [out["name"] for out in proof["admission"]["output_declarations"]])
            for slot, expected in (("second", 1), ("third", 5)):
                row = rows[nodes[slot]]
                self.assertEqual(row["status"], "SUCCEEDED")
                selected, = manifest.read_manifest(db, row["job_id"])["admission"]["inputs"]
                self.assertEqual(json.loads(ArtifactService(db).read_bytes(selected["artifact_ref"])),
                                 {"readings": [expected]})
            self.assertEqual(v6_decisions.adopt(db, **self.call, decision=body)["decision_id"], answer["decision_id"])
            v6_jobs.advance(db, self.run)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE run_id=?", (self.run,)).fetchone()[0], 3)

    def test_saved_recipe_pins_materials_ports_and_dependencies(self):
        change = self.graph([("second", "next.json"), ("third", "optional.json")])
        graph = v6_decisions.adopt(self.conn, **self.call, decision=change)
        slots = [{"slot": slot, "node_id": graph["aliases"]["local:" + slot]}
                 for slot in ("split", "second", "third")]
        self.settle([row["node_id"] for row in slots], self.conn)
        fields = {"recipe_id": "bounds", "version": "1", "title": "Detector series bounds",
                  "id_alignment": "Use the admitting sample identity.", "missing_values": "Reject missing series.",
                  "units": "Instrument counts", "nodes": slots,
                  "inputs": [{"name": "series", "artifact_ref": self.raw}]}
        card = v6_recipes.save(self.conn, self.run, fields)
        quick = {"scope": "run", "run_id": self.run, "evidence_policy": v621_policy.VERSION}
        menu = {t["name"] for t in v6_native.tool_specs([], quick, conn=self.conn, run_id=self.run)}
        self.assertTrue({"gx_instantiate_recipe", "gx_query_catalog"} <= menu)
        discovered = v62_catalog.query(self.conn, self.run, quick, {"kind": "recipe", "reference": card["reference"]})
        self.assertEqual(discovered["spec"]["units"], fields["units"])
        reorder = v6_recipes.save(self.conn, self.run, dict(fields, nodes=list(reversed(slots))))
        self.assertEqual(reorder["digest"], card["digest"])
        with self.assertRaises(VersionImmutable):
            v6_recipes.save(self.conn, self.run, dict(fields, units="milliseconds"))
        args = {"recipe": card["reference"], "digest": card["digest"], "branch": "local:next",
                "inputs": {"series": self.raw}, "prefix": "qc"}
        steps = {s["node"]: s for s in v6_recipes.instantiate(self.conn, self.run, args)["ops"]}
        for label, name in (("second", "next.json"), ("third", "optional.json")):
            op = steps["local:qc-" + label]
            self.assertEqual(op["inputs"], op["depends_on"])
            self.assertEqual(op["inputs"], ["local:qc-split"])
            self.assertEqual(op["input_ports"], [{"index": 0, "output": name}])
        with self.assertRaisesRegex(SchemaInvalid, "digest"):
            v6_recipes.instantiate(self.conn, self.run, dict(args, digest="0" * 64))
        with self.assertRaisesRegex(SchemaInvalid, "every named"):
            v6_recipes.instantiate(self.conn, self.run, dict(args, inputs={}))

    def test_unknown_output_rejected_before_graph_or_job(self):
        with self.assertRaisesRegex(SchemaInvalid, "declared.*output|output.*declared") as error:
            v6_decisions.adopt(self.conn, **self.call, decision=self.graph([("wrong", "missing.json")]))
        details = error.exception.details
        self.assertEqual((details["node"], details["input_index"], details["requested_output"]),
                         ("local:wrong", 0, "missing.json"))
        self.assertEqual(details["expected_type"], "named_file_output")
        self.assertCountEqual([out["name"] for out in details["available_file_outputs"]],
                              ["mean.json", "next.json", "optional.json"])
        self.assertTrue(all(out["kind"] for out in details["available_file_outputs"]))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM v6_nodes").fetchone()[0], 0)


class ModelRecipe(unittest.TestCase):
    def test_prediction_rebinds_new_material_and_controlled_repeat_survives_restart(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db_file = tmp / "gx.db"
        db = self.enterContext(closing(store.open_store(db_file)))
        fixture = tmp / "detector-release"
        shutil.copytree(series.FIXTURE, fixture)
        method = json.loads((fixture / "series.json").read_text())
        method["manifest"]["outputs"].append({"name": "NOTICE.txt", "kind": "file:terms", "required": True})
        (fixture / "series.json").write_text(json.dumps(method))
        program = fixture / "measure.py"
        with program.open("a") as script:
            script.write('\n(Path(sys.argv[2]) / "NOTICE.txt").write_text("CC0 public test fixture")\n')
        self.enterContext(patch.object(series, "FIXTURE", fixture))
        run_a, ref_a = series.start_series_goal(self, db, tmp)
        call = bind_native_fixture(db, run_a, tmp)
        node = dict(op="add_node", branch="local:sample", node="local:model", capability=series.METHOD,
                    inputs=[ref_a], parameters={}, depends_on=[], question="What does the extrapolation predict?")
        route = {**branch(), "budget_limits": {"job_submissions": 3, "cpu_seconds": 60}}
        graph = v6_decisions.adopt(db, **call, decision=draft(route, node))
        source = graph["aliases"]["local:model"]
        self.run = run_a
        NamedOutputs.settle(self, [source], db)
        fields = {"recipe_id": "sensor-extrapolation", "version": "1", "title": "Archived sensor extrapolation",
                  "id_alignment": "Keep the original order", "missing_values": "Reject incomplete time series",
                  "units": "Instrument counts", "nodes": [{"slot": "estimate", "node_id": source}],
                  "inputs": [{"name": "series", "artifact_ref": ref_a}],
                  "decision_point": {"step": "estimate", "output": "next.json", "question": "Is a follow-up indicated?",
                                     "actions": ["ask for new observations", "retain the current interval"],
                                     "stop_condition": "A new reading resolves the ambiguity."}}
        card = v6_recipes.save(db, run_a, fields)
        self.assertNotIn("NOTICE.txt", [out["name"] for out in card["outputs"]["estimate"]])
        prediction = next(out for out in card["outputs"]["estimate"] if out["name"] == "next.json")
        self.assertEqual((prediction["nature"], prediction["source_roles"]), ("model_prediction", ["readings"]))
        replacement = tmp / "secondary.json"
        replacement.write_text('{"schema":"instrument/1","readings":[5,3,1]}')
        spec = {"mode": "goal", "project": "output-types", "research_effort": "quick",
                "objective": "Summarize the observed series and estimate the next reading",
                "plugins": ["org.example.instrument-series@1.0.0"], "capabilities": [series.METHOD],
                "input_files": [{"role": "readings", "path": str(replacement)}]}
        self.run = v6_goal.start(db, spec, run_key="new-sample", current=None)["run_id"]
        ref = json.loads(db.execute("SELECT inputs_json FROM runs WHERE run_id=?", (self.run,)).fetchone()[0])["files"][0]["artifact_ref"]
        params = {"recipe": card["reference"], "digest": card["digest"], "branch": "local:sample", "prefix": "fresh",
                  "inputs": {"series": ref}}
        expansion = v6_recipes.instantiate(db, self.run, params)
        ops = expansion["ops"]
        self.assertIn("stop_condition", expansion["submission"]["required_open_branch_fields"])
        self.assertEqual(1, expansion["submission"]["minimum_budget_limits"]["job_submissions"])
        repeat = v6_recipes.instantiate(db, self.run, dict(params, prefix="replicate", independent_repeat=True))["ops"]
        call_b = bind_native_fixture(db, self.run, tmp)
        change = draft(route, *ops, *repeat)
        answer = v6_decisions.adopt(db, **call_b, decision=change)
        nodes = [answer["aliases"][op["node"]] for op in [*ops, *repeat]]
        self.assertTrue(all({"event": "job_settled", "subject_ref": node} in answer["reconsider_when"] for node in nodes))
        v6_jobs.advance(db, self.run)
        db.close()
        with closing(store.open_store(db_file)) as reopened:
            rows = NamedOutputs.settle(self, nodes, reopened)
            jobs = {rows[n]["job_id"] for n in nodes}
            self.assertEqual(len(jobs), 2)
            source_job, = reopened.execute("SELECT job_id FROM v6_nodes WHERE node_id=?", (source,)).fetchone()
            self.assertNotIn(source_job, jobs)
            for n in nodes:
                point = v6_recipes.settled_decision(reopened, self.run, rows[n], json.loads(rows[n]["spec_json"]))
                self.assertEqual(point["evidence"]["nature"], "model_prediction")
                read = ArtifactService(reopened).read_json(point["evidence"]["artifact_ref"])
                self.assertEqual(read["value"], -1)
                receipt = manifest.read_manifest(reopened, rows[n]["job_id"])
                self.assertEqual(receipt["admission"]["inputs"][0]["artifact_ref"], ref)
            v6_decisions.adopt(reopened, **call_b, decision=change)
            v6_jobs.advance(reopened, self.run)
            self.assertEqual(2, reopened.execute("SELECT count(*) FROM jobs WHERE run_id=?", (self.run,)).fetchone()[0])
            if path := os.environ.get("GX_RECIPE_FORECAST_RECEIPT"):
                def publication(run, job):
                    return next(out for out in v62_outputs.published_outputs(reopened, run, job)
                                if out["output_name"] == "next.json")
                result = {"transport":"scripted_native_fixture", "live_codex_calls":0, "recipe":card,
                          "source":{"run_id":run_a, "input":ref_a, "job_id":source_job,
                                    "forecast":publication(run_a, source_job),
                                    "value":ArtifactService(reopened).read_json(publication(run_a, source_job)["artifact_ref"])["value"]},
                          "new_goal":{"run_id":self.run, "input":ref, "jobs":[
                              {"job_id":rows[n]["job_id"], "node_id":n, "forecast":publication(self.run, rows[n]["job_id"]),
                               "value":ArtifactService(reopened).read_json(publication(self.run, rows[n]["job_id"])["artifact_ref"])["value"]}
                              for n in nodes], "count_after_cold_replay":2}}
                Path(path).write_text(json.dumps(result,indent=2)+"\n")
