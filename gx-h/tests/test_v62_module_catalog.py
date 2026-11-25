"""Normal Goal startup, scoped L1/L2 discovery and authoring multiple local module identities."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gx_core import config
from gx_core.errors import SchemaInvalid, ScopeDenied, VersionImmutable
from gx_core.plugins.catalog import package_digest
from gx_core.p0 import v62_catalog as catalog


class ModuleCatalog(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.work = Path(tmp)
        self.cli = config.HARNESS_ROOT.parent / "bin/gx-p0"

    def start(self, effort, settings=None):
        state = self.work / effort
        grant = self.work / (effort + ".json")
        grant.write_text(json.dumps(settings or {}))
        cmd = [sys.executable, "-B", self.cli, "start", "--state", state, "--run-key", "one",
               "--goal", "Describe the coverage of these local sources.", "--goal-config", grant]
        if effort != "old":
            cmd += ["--research-effort", effort]
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        rid = json.loads(result.stdout)["run_id"]
        conn = self.enterContext(closing(sqlite3.connect(state / "gx.db")))
        conn.row_factory = sqlite3.Row
        brief = json.loads(conn.execute("SELECT snapshot_json FROM research_contexts WHERE run_id=?", (rid,)).fetchone()[0])
        return conn, rid, brief

    def test_quick_and_old_have_no_module_or_discovery_overhead(self):
        for effort in ("quick", "old"):
            with self.subTest(effort=effort):
                conn, run, brief = self.start(effort)
                self.assertEqual(catalog.policy(conn, run)["effort"], "quick")
                self.assertNotIn("module_catalog", brief)
                self.assertNotIn("research_effort", brief)
                self.assertIs(catalog.project(conn, run, brief), brief)
                with self.assertRaises(ScopeDenied):
                    catalog.resolve(conn, run, "data_inventory", "1")

    def test_normal_goal_preserves_exact_module_grant_and_summarizes_all_methods(self):
        caps = ["deposited-chain-geometry@1", "tl1a-coordinate-quality@1"]
        conn, run, frozen = self.start("medium", {"research_modules": ["data_inventory@1"], "capabilities": caps,
                                      "plugins": ["org.gx.protein@0.2.0", "org.gx.data-inventory@1.0.0"],
                                      "module_policy": {"catalog_limit": 1}})
        self.assertEqual(frozen["research_effort"], "medium")
        self.assertEqual(frozen["catalog_view"]["mode"], "categories")
        self.assertEqual(frozen["capabilities"], [])
        self.assertEqual(frozen["module_catalog"][0]["module_id"], "data_inventory")
        self.assertNotIn("instructions", frozen["module_catalog"][0])
        lookup = lambda **q: catalog.query(conn, run, frozen, q)
        first = lookup(kind="method", limit=1)
        second = lookup(kind="method", offset=first["next_offset"], limit=1)
        self.assertEqual({r["capability_version"] for r in first["items"] + second["items"]}, set(caps))
        self.assertIsNone(second["next_offset"])
        self.assertEqual(lookup(kind="method", reference=caps[0])["spec"]["capability_version"], caps[0])
        module = lookup(kind="module", reference="data_inventory@1")["spec"]
        self.assertEqual(set(module["capabilities"]), set(caps))
        self.assertIn("2RE9", module["instructions"])
        self.assertEqual(len(module["plugin"]["sha256"]), 64)
        self.assertIn("gx_submit_module_report", module["tools"])
        with self.assertRaises(ScopeDenied):
            lookup(kind="method", reference="artifact-analysis@1")
        limited = {**frozen, "scope": "module:fixture", "module_id": "data_inventory", "module_version": "1",
                   "allowed_method_ids": [caps[0]], "allowed_tools": ["gx_query_catalog"]}
        with self.assertRaises(ScopeDenied):
            catalog.query(conn, run, limited, {"kind": "method", "reference": caps[1]})
        restricted = catalog.query(conn, run, limited, {"kind": "module", "reference": "data_inventory@1"})["spec"]
        self.assertEqual(restricted["capabilities"], [caps[0]])
        self.assertEqual(restricted["tools"], ["gx_query_catalog"])
        self.assertEqual(catalog.query(conn, run, limited, {"kind": "method"})["total"], 1)
        with self.assertRaises(ScopeDenied):
            catalog.query(conn, run, {**frozen, "run_id": "another"}, {"kind": "module"})
        with self.assertRaises(ScopeDenied):
            catalog.query(conn, run, {**frozen, "scope": "branch:another"}, {"kind": "module"})
        self.assertEqual(catalog.resolve(conn, run, "data_inventory", "1")["plugin"], module["plugin"])

    def test_byte_limit_and_paginated_search_do_not_hide_last_method(self):
        # Freeze a real medium Run and exercise its persisted catalogue with many test methods.
        conn, run, brief = self.start("medium", {"module_policy": {"catalog_limit": 100, "catalog_bytes": 256}})
        inputs = json.loads(conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run,)).fetchone()[0])
        rows = [{"capability_version": f"local-{index}@1", "portable_sha256": f"{index:064x}",
                 "method": {"family": "metrology", "objective": "Inspect local observations."},
                 "outputs": [{"name": "measurement.json", "kind": "file:measurement"}]}
                for index in range(19)]
        inputs["inputs"]["capability_catalog"] = rows
        conn.execute("UPDATE runs SET inputs_json=? WHERE run_id=?", (json.dumps(inputs), run))
        conn.execute("UPDATE v6_goals SET capabilities_json=? WHERE run_id=?", (json.dumps(
            {r["capability_version"]: r["portable_sha256"] for r in rows}), run))
        projection = catalog.project(conn, run, brief)
        self.assertEqual(projection["capabilities"], [])
        self.assertEqual(projection["catalog_view"]["categories"], {"metrology": 19})
        last = catalog.query(conn, run, projection, {"kind": "method", "category": "metrology", "offset": 15})
        self.assertEqual([m["capability_version"] for m in last["items"]], [f"local-{i}@1" for i in range(15, 19)])
        lookup = catalog.query(conn, run, projection, {"kind": "method", "query": "local-18"})
        self.assertEqual(lookup["total"], 1)
        self.assertEqual(lookup["items"][0]["capability_version"], "local-18@1")

    def test_one_package_registers_distinct_inventory_and_deep_roles_with_its_own_versions(self):
        base = self.work / "harness"
        settings = base / "config"
        settings.mkdir(parents=True)
        root = base / "plugins/installed/lab.fixture/1.0.0"
        root.mkdir(parents=True)
        (root / "gx_plugin.json").write_text(json.dumps({
            "id": "lab.fixture", "version": "1.0.0", "api_version": "1.0", "kind": "bundle",
            "entrypoint": "plugin:register", "gx_compat": ">=3.0,<4.0", "dependencies": [],
            "extensions": [{"id": "lookup", "version": "1", "contract": "research_module"},
                           {"id": "interpret", "version": "2", "contract": "research_module"}],
        }))
        entry = root / "plugin.py"
        entry.write_text(
            "def register(ctx):\n"
            "    for name,version,focus,efforts in [('lookup','1','model',['medium','deep']),"
            "('interpret','2','hypotheses',['deep'])]:\n"
            "        ctx.research_modules.register(module_id=name,version=version,focuses=[focus],efforts=efforts,"
            "purpose='Read local references',instructions='Inspect only granted records.',output='module_report_bundle',"
            "cost_class='metadata',capabilities=[],tools=['gx_query_catalog'],input_roles=[])\n")
        installed = package_digest(root)
        (settings / "workflow_packages.json").write_text(json.dumps({"plugins": [
            {"path": "lab.fixture/1.0.0", "sha256": installed}]}))
        with patch.object(config, "CONFIG_DIR", settings), patch.object(config, "HARNESS_ROOT", base):
            medium = catalog.for_start({"research_effort": "medium"})
            deep = catalog.for_start({"research_effort": "deep"})
            self.assertEqual([r["module_id"] for r in medium["modules"]], ["lookup"])
            self.assertEqual({(r["module_id"], r["version"]) for r in deep["modules"]},
                             {("lookup", "1"), ("interpret", "2")})
            self.assertTrue(all(r["plugin"]["sha256"] == installed for r in deep["modules"]))
            with self.assertRaises(SchemaInvalid):
                catalog.for_start({"research_effort": "medium", "research_modules": ["interpret@2"]})
            with self.assertRaises(SchemaInvalid):
                catalog.for_start({"research_effort": "deep", "research_modules": ["lookup@2"]})
            conn = self.enterContext(closing(sqlite3.connect(":memory:")))
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE runs(run_id TEXT,inputs_json TEXT)")
            conn.execute("INSERT INTO runs VALUES('frozen',?)",
                         (json.dumps({"inputs": {"research_catalog": deep, "capability_catalog": []}}),))
            entry.write_text(entry.read_text()+"# an unpinned change\n")
            with self.assertRaisesRegex(VersionImmutable, "digest mismatch"):
                catalog.resolve(conn, "frozen", "lookup", "1")

    def test_effort_and_budget_validation(self):
        for spec in ({"research_effort": "ultra"}, {"research_effort": "quick", "research_modules": ["x@2"]},
                     {"research_effort": "deep", "module_policy": {"max_parallel": True}},
                     {"research_effort": "deep", "module_policy": {"brief_bytes": 5}},
                     {"research_effort": "medium", "research_modules": ["data_inventory"]}):
            with self.subTest(spec=spec), self.assertRaises(SchemaInvalid):
                catalog.for_start(spec)
