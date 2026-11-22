"""Run-scoped composition through the real installed Python package loader."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gx_core import config
from gx_core.errors import SchemaInvalid, VersionImmutable
from gx_core.plugins.catalog import package_digest
from gx_core.plugins.selection import restore, select


class PluginSelectionTest(unittest.TestCase):
    version = "1.0.0"
    graph = ("science_grid", "2")

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "config"
        self.config.mkdir()
        self.catalog = self.config / "workflow_packages.json"
        self.records = []
        self.catalog.write_text("{}")
        self.enterContext(patch.object(config, "HARNESS_ROOT", self.root))
        self.enterContext(patch.object(config, "CONFIG_DIR", self.config))

    def install(self, plugin_id, *, deps=(), workflow=None, hook=None, tool=None, marker=None):
        root = self.root / "plugins" / "installed" / plugin_id / self.version
        root.mkdir(parents=True)
        source = []
        if marker:
            source += ["from pathlib import Path", f"Path({str(marker)!r}).touch()"]
        source += ["def handle(*args): return __name__", "def register(ctx):"]
        extensions = []
        if workflow:
            hooks = f"{{{hook!r}: handle}}" if hook else "{}"
            source += [f"    ctx.workflows.register(workflow_id={workflow[0]!r}, version={workflow[1]!r}, hooks={hooks})"]
            extensions.append({"id": "workflow", "contract": "workflow", "version": "1.0"})
        if tool:
            source += [f"    ctx.tools.register(name={tool!r}, schema={{'type': 'object'}}, handler=handle)"]
            extensions.append({"id": "tool", "contract": "tool", "version": "1.0"})
        source.append("    pass")
        (root / "plugin.py").write_text("\n".join(source) + "\n")
        (root / "gx_plugin.json").write_text(json.dumps({
            "id": plugin_id, "version": self.version, "api_version": "1.0", "kind": "bundle",
            "entrypoint": "plugin:register", "gx_compat": ">=2.1,<4.0",
            "dependencies": [{"id": dep, "version": self.version, "optional": optional} for dep, optional in deps],
            "extensions": extensions,
        }))
        digest = package_digest(root)
        self.records.append({"path": f"{plugin_id}/{self.version}", "sha256": digest})
        self.catalog.write_text(json.dumps({"plugins": self.records}))
        return {"id": plugin_id, "version": self.version, "sha256": digest, "api_version": "1.0"}

    def ref(self, name):
        return f"{name}@{self.version}"

    def test_legacy_selection_needs_no_catalog(self):
        self.catalog.unlink()
        with patch("gx_core.plugins.loader.bootstrap", side_effect=AssertionError("loader called")):
            self.assertEqual(select([], *self.graph).plugins, ())
            self.assertEqual(restore([], *self.graph).pins, [])

    def test_exact_graph_contributions_and_unique_names(self):
        dependency = self.install("org.gx.dep", workflow=("other", "1"), hook="prepare_dispatch")
        main = self.install("org.gx.main", deps=[("org.gx.dep", False)], workflow=self.graph,
                            hook="prepare_dispatch", tool="first")
        extra = self.install("org.gx.extra", workflow=self.graph, hook="build_task_context", tool="second")
        refs = [self.ref("org.gx.main"), self.ref("org.gx.extra")]
        selection = select(refs, *self.graph)
        self.assertEqual(selection.pins, [extra, dependency, main])
        self.assertEqual(restore(selection.pins, *self.graph).plugins, selection.plugins)
        self.assertEqual(select(refs, *self.graph, expected=selection.pins).pins, selection.pins)
        with self.assertRaisesRegex(SchemaInvalid, "selected graph"):
            select([self.ref("org.gx.dep")], *self.graph)
        self.install("org.gx.colliding.hook", workflow=self.graph, hook="prepare_dispatch")
        with self.assertRaisesRegex(SchemaInvalid, "duplicate hook 'prepare_dispatch'"):
            select([*refs, self.ref("org.gx.colliding.hook")], *self.graph)
        self.install("org.gx.colliding.tool", tool="second")
        with self.assertRaisesRegex(SchemaInvalid, "duplicate tool 'second'"):
            select([*refs, self.ref("org.gx.colliding.tool")], *self.graph)

    def test_cold_resume_ignores_new_optional_package_before_import(self):
        base = self.install("org.gx.base", deps=[("org.gx.optional", True)], workflow=self.graph)
        original = select([self.ref("org.gx.base")], *self.graph)
        self.assertEqual(original.pins, [base])
        marker = self.root / "imported"
        self.install("org.gx.optional", marker=marker, tool="new_tool")
        script = """
import json, sys
from pathlib import Path
from gx_core import config
from gx_core.plugins.selection import restore
config.HARNESS_ROOT = Path(sys.argv[1])
config.CONFIG_DIR = config.HARNESS_ROOT / 'config'
print(json.dumps(restore(json.loads(sys.argv[2]), 'science_grid', '2').pins))
"""
        restored = subprocess.run([sys.executable, "-B", "-c", script, str(self.root), json.dumps(original.pins)],
                                  cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(restored.stdout), [base])
        self.assertFalse(marker.exists())
        revised = select([self.ref("org.gx.base")], *self.graph)
        self.assertTrue(marker.exists())
        self.assertEqual([pin["id"] for pin in revised.pins], ["org.gx.optional", "org.gx.base"])
        self.records = [row for row in self.records if not row["path"].startswith("org.gx.optional/")]
        self.catalog.write_text(json.dumps({"plugins": self.records}))
        with self.assertRaisesRegex(VersionImmutable, "org.gx.optional"):
            restore(revised.pins, *self.graph)
        self.assertEqual(restore(original.pins, *self.graph).pins, [base])

    def test_bad_pins_and_outside_required_dependency_do_not_import(self):
        marker = self.root / "imported"
        base = self.install("org.gx.mandatory", deps=[("org.gx.helper", False)], marker=marker)
        helper = self.install("org.gx.helper", marker=marker)
        with self.assertRaisesRegex(VersionImmutable, "missing from the frozen closure"):
            restore([base], *self.graph)
        with self.assertRaisesRegex(VersionImmutable, "expected exact digest"):
            restore([{**helper, "sha256": "2"*64}], *self.graph)
        with self.assertRaisesRegex(VersionImmutable, "Author API"):
            restore([{**helper, "api_version": "7.0"}], *self.graph)
        with self.assertRaisesRegex(VersionImmutable, "exceeds"):
            select([self.ref("org.gx.mandatory")], *self.graph, expected=[helper])
        self.assertFalse(marker.exists())
        saved = restore([helper, base], *self.graph).pins
        self.assertTrue(marker.exists())
        self.assertEqual(saved, [helper, base])
        with self.assertRaisesRegex(VersionImmutable, "closure differs"):
            select([self.ref("org.gx.helper")], *self.graph, expected=saved)

    def test_malformed_operator_or_saved_references(self):
        for invalid in (None, "org.gx.a@1.0.0", ["org.gx.a"], ["org.gx.a@1"], [1],
                        ["org.gx.abc@1.0.0"] * 2):
            with self.subTest(invalid=invalid), self.assertRaises(SchemaInvalid):
                select(invalid, *self.graph)
        pin = self.install("org.gx.defined")
        for malformed in ({}, [pin, pin], [{"id": pin["id"]}], [{**pin, "sha256": 1234}]):
            with self.subTest(malformed=malformed), self.assertRaises(SchemaInvalid):
                restore(malformed, *self.graph)
