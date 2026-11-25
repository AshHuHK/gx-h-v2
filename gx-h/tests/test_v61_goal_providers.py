"""Exact Goal composition, public startup and named provider failures."""
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
from gx_core.capabilities import shipped
from gx_core.errors import SchemaInvalid, VersionImmutable
from gx_core.plugins import goal
from gx_core.plugins.catalog import package_digest


class GoalProviders(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.work = Path(workspace.name)
        self.cli = config.HARNESS_ROOT.parent / "bin/gx-p0"

    def start(self, name, **grant):
        file = self.work / f"{name}.json"
        file.write_text(json.dumps(grant))
        return subprocess.run(
            [sys.executable, "-B", self.cli, "start", "--state", self.work / name, "--run-key", name,
             "--goal", "Compare sensor readings.", "--goal-config", file], capture_output=True, text=True)

    def test_public_empty_exact_and_repeated_grants(self):
        bare = self.start("empty")
        self.assertEqual(bare.returncode, 0, bare.stderr)
        self.assertEqual(json.loads(bare.stdout)["capabilities"], {})
        cap = "tl1a-coordinate-quality@1"
        package = "org.gx.protein@0.2.0"
        opened = self.start("one", capabilities=[cap], plugins=[package])
        self.assertEqual(opened.returncode, 0, opened.stderr)
        reopened = self.start("one", capabilities=[cap], plugins=[package])
        self.assertEqual(reopened.returncode, 0, reopened.stderr)
        rid = json.loads(opened.stdout)["run_id"]
        self.assertEqual(json.loads(reopened.stdout)["run_id"], rid)
        conflict = self.start("one", capabilities=["tl1a-geometry@1"], plugins=[package])
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("different project specification", conflict.stderr + conflict.stdout)
        with closing(sqlite3.connect(self.work / "one/gx.db")) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (rid,)).fetchone()
            data = json.loads(row[0])
            entry, = data["inputs"]["capability_catalog"]
            self.assertEqual(entry["plugin"], data["plugin_binding"]["packages"][0])
            self.assertEqual(entry["plugin"]["sha256"], package_digest(
                config.HARNESS_ROOT / "plugins/installed/org.gx.protein/0.2.0"))
            self.assertEqual(goal.tools(conn, rid), {})
            self.assertTrue(shipped.participates(conn, rid, cap))
            self.assertFalse(shipped.participates(conn, rid, "tl1a-geometry@1"))
            data["plugin_binding"]["packages"] = []
            conn.execute("UPDATE runs SET inputs_json=? WHERE run_id=?", (json.dumps(data), rid))
            self.assertFalse(shipped.participates(conn, rid, cap))
        missing = self.start("bad", capabilities=[cap], plugins=["org.gx.unknown@1.0.0"])
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("unavailable", missing.stderr + missing.stdout)
        with closing(sqlite3.connect(self.work / "bad/gx.db")) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_ambiguous_provider_and_changed_digest(self):
        base = self.work / "release"
        settings = base / "config"
        settings.mkdir(parents=True)
        (settings / "shipped.json").write_text(json.dumps({"capabilities": {}}))
        records = []
        cap = "shared-calibration@1"
        for package in ("lab.one", "lab.two"):
            path = base / "plugins/installed" / package / "1.0.0"
            path.mkdir(parents=True)
            (path / "plugin.py").write_text(
                "def register(ctx):\n    ctx.capabilities.add_manifest('shared-calibration@1')\n")
            (path / "gx_plugin.json").write_text(json.dumps({
                "id": package, "version": "1.0.0", "api_version": "1.0", "kind": "bundle",
                "gx_compat": ">=3.0,<4.0", "entrypoint": "plugin:register", "dependencies": [],
                "extensions": [{"id": "shared", "contract": "capability", "version": "1.0"}],
            }))
            records.append({"path": package + "/1.0.0", "sha256": package_digest(path)})
        (settings / "workflow_packages.json").write_text(json.dumps({"plugins": records}))
        with patch.object(config, "CONFIG_DIR", settings), patch.object(config, "HARNESS_ROOT", base):
            with self.assertRaisesRegex(SchemaInvalid, "Multiple installed providers.*shared-calibration"):
                goal._discover([cap], [])
            selected = goal._discover([cap], ["lab.one@1.0.0"])[cap][0]
            self.assertEqual(selected.pin.digest, records[0]["sha256"])
            (base / "plugins/installed/lab.one/1.0.0/plugin.py").write_text("# changed")
            with self.assertRaisesRegex(SchemaInvalid, "digest mismatch"):
                goal._discover([cap], ["lab.one@1.0.0"])
            with self.assertRaisesRegex(VersionImmutable, "digest mismatch"):
                goal._provider({"plugin": goal._document(selected.pin)})

    def test_missing_provider_cannot_be_replaced_by_bare_shipped_worker(self):
        settings = self.work / "config"
        settings.mkdir()
        (settings / "workflow_packages.json").write_text('{"plugins": []}')
        (settings / "shipped.json").write_bytes((config.CONFIG_DIR / "shipped.json").read_bytes())
        with patch.object(config, "CONFIG_DIR", settings):
            with self.assertRaisesRegex(SchemaInvalid, "No installed Goal provider for tl1a-geometry@1"):
                goal._discover(["tl1a-geometry@1"], [])
            self.assertEqual(goal._discover(["artifact-analysis@1"], []), {})
