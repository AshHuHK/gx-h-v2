"""C1. Catalog discovery and native graph submission share the scoped inventory grant."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v62a import interleavings_fixture as local
from gx_core.artifacts import ArtifactService
from gx_core.errors import ScopeDenied, StaleRevision
from gx_core.p0 import operator, v62_catalog


class InventoryGrant(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config, data = self.directory / "count.json", self.directory / "temperatures.json"
        local.save(config, {"script": "{'count': count(data['A'])}", "output_schema": {
            "type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"],
            "additionalProperties": False}})
        local.save(data, [6.5, 7.0, 7.5, 8.0])
        self.enterContext(patch.object(local, "GOAL", "Inventory the supplied thermometer readings."))
        self.spec = {"research_effort": "deep", "research_modules": None, "plugins": ["org.gx.protein@0.3.0"],
            "capabilities": ["artifact-analysis@1", "deposited-chain-geometry@2", "fixture-grid@1"],
            "input_files": [{"path": str(config), "role": "spec"}, {"path": str(data), "role": "temperatures"}]}

    def world(self, key, **overrides):
        return self.enterContext(local.Scenario(self.directory/key, spec_updates={**self.spec, **overrides}))

    def test_typed_module_catalog_and_real_job_intersect_the_same_grants(self):
        s = self.world("new")
        analysis, geometry, simulated = self.spec["capabilities"]
        mod = s.call(s.lead, "gx_query_catalog", {"kind": "module", "reference": "data_inventory@3"}, "policy")
        self.assertEqual(set(mod["spec"]["capabilities"]), {analysis, geometry})
        with self.assertRaises(ScopeDenied):
            s.call(s.lead, "gx_open_module", {**s.open_args("out-of-policy", caps=[simulated]),
                                              "module_version": "3"}, "out-of-policy")
        opened = s.call(s.lead, "gx_open_module",
                        {**s.open_args("data", caps=[analysis]), "module_version": "3",
                         "expected_delivery": "Count the supplied thermometer readings."}, "open")
        worker = s.bind(opened["task_id"])
        listing = s.call(worker, "gx_query_catalog", {"kind": "method"}, "catalog")
        self.assertEqual([x["capability_version"] for x in listing["items"]], [analysis])
        card = s.call(worker, "gx_query_catalog", {"kind": "method", "reference": analysis}, "card")
        self.assertEqual(card["spec"]["family"], "bounded_json_analysis")
        for idx, method in enumerate([geometry, simulated, "tl1a-coordinate-quality@1"]):
            with self.subTest(method=method):
                with self.assertRaises(ScopeDenied):
                    s.call(worker, "gx_query_catalog", {"kind": "method", "reference": method}, "denied-card-"+str(idx))
                with self.assertRaises(ScopeDenied):
                    s.submit_node(worker, "denied"+str(idx), capability=method)
        self.assertEqual(s.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        op = {**s.node(worker, "count", capability=analysis), "inputs": s.materials,
              "question": "How many thermometer readings were supplied?"}
        decision = local.draft(op, watches=[{"event": "job_settled", "subject_ref": "local:count"}])
        req = s.call(worker, "gx_decide_revision", {"decision": decision}, "count-readings")
        settled = s.settle(req["aliases"]["local:count"])
        assets = ArtifactService(s.conn)
        result = assets.read_json(settled["result_ref"])
        self.assertEqual(result, {"count": 4})
        self.assertEqual(s.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        if path := os.environ.get("GX623_INVENTORY_RECEIPTS"):
            Path(path).mkdir(parents=True, exist_ok=True)
            local.save(Path(path)/"nonprotein-inventory.json", {
                "schema": "gx.acceptance/6.2.3.inventory", "run": s.run_id,
                "module": mod["spec"]["plugin"], "run_granted": self.spec["capabilities"],
                "module_eligible": mod["spec"]["capabilities"], "delegated": [analysis],
                "inputs": [{"ref": ref, "sha256": assets.describe(ref)["sha256"]} for ref in s.materials],
                "actions": s.actions, "job": settled["job_id"], "node": settled["node_id"],
                "output": {"ref": settled["result_ref"], "sha256": assets.describe(settled["result_ref"])["sha256"],
                           "value": result}, "real_cpu_jobs": 1, "paid_calls": 0})

    def test_later_operator_ban_revokes_catalog_and_dispatch(self):
        s = self.world("ban")
        method = "artifact-analysis@1"
        args = {**s.open_args("before-ban", caps=[method]), "module_version": "3"}
        opening = s.call(s.lead, "gx_open_module", args, "before-ban")
        worker = s.bind(opening["task_id"])
        self.assertEqual(s.call(worker, "gx_query_catalog", {"kind": "method"}, "before")["total"], 1)
        action = {"kind": "change", "items": [{"item_id":"analysis-ban", "item_type":"method_ban",
            "expected_version":0, "value":{"forbidden_capabilities":[method]}}]}
        change = operator.submit(operator.local_context(s.conn), s.run_id, action)["result"]
        self.assertEqual(change["status"], "committed")
        for tool, args in [
            ("gx_query_catalog", {"kind":"method", "reference":method}),
            ("gx_decide_revision", {"decision":local.draft(s.node(worker, "banned", capability=method))})
        ]:
            with self.subTest(tool=tool), self.assertRaises((ScopeDenied, StaleRevision)):
                s.call(worker, tool, args, tool + "-after-ban")
        self.assertNotIn(method, v62_catalog.resolve(s.conn, s.run_id, "data_inventory", "3")["capabilities"])
        self.assertEqual(s.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_historical_module_and_roles(self):
        historical = self.world("old", evidence_policy="gx.evidence/6.2.2", historical_replay=True)
        answer = historical.call(historical.lead, "gx_query_catalog", {"kind":"module"}, "old-list")
        self.assertIn(("data_inventory","2"), [(m["module_id"],m["version"]) for m in answer["items"]])
        with self.assertRaises(ScopeDenied):
            historical.call(historical.lead, "gx_query_catalog", {"kind":"module","reference":"data_inventory@3"}, "no-new")
        profiles = [{"output_nature":"analysis"}]
        # A newly named method can qualify from immutable roles without changing the inventory plugin.
        typed = {"family":"weather_window_statistics", "inputs":["thermometer"],
                 "outputs":[{"name":"bins.json","kind":"file:weather_bins","nature":"analysis",
                             "source_roles":["thermometer"]}]}
        self.assertTrue(v62_catalog._fits_profile(typed, profiles))
        for override in ({"nature":"model_prediction"}, {"source_roles":["uncoupled"]}, {"source_roles":[]}):
            self.assertFalse(v62_catalog._fits_profile({**typed, "outputs":[{**typed["outputs"][0],**override}]}, profiles))


if __name__ == "__main__":
    unittest.main()
