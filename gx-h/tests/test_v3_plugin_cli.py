"""Public plugin operator commands in an independent installation and local workspace."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from gx_core import budgets
from gx_core.persistence import records, store


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = Path("examples/plugins/hello_research")
PLUGIN = "org.example.gx.hello-research"
VERSION = "0.2.0"


class PluginOperatorCLI(unittest.TestCase):
    def test_staged_install_verify_legacy_status_and_caller_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            stage = work / "release"
            for directory in ("bin", "gx-h", "examples"):
                shutil.copytree(ROOT / directory, stage / directory, symlinks=True, ignore=shutil.ignore_patterns(
                    "node_modules", "__pycache__", "*.pyc", ".venv", "var", "installed", ".plugin-install.lock"))
            cli = stage / "bin/gx-p0"
            catalog = stage / "gx-h/config/workflow_packages.json"
            original = json.loads(catalog.read_bytes())
            original.pop("plugins", None)
            catalog.write_text(json.dumps(original))
            env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                   "GX_SCOPE": "operator", "GX_PROJECTS": "*", "GX_SUBJECT": "fixture-operator"}
            state = work / "existing-project"
            state.mkdir()
            with closing(store.open_store(state / "gx.db")) as conn:
                budget = budgets.create_root(conn, run_id=None, actor="fixture", profile_name="p0")
                rid = records.create_run(conn, objective="Read the historical observations", project_ref="observations",
                                         actor="fixture", budget_ref=budget, workflow_id="science_grid",
                                         workflow_version="2", inputs={"inputs": {"p0": True, "p0_spec": {}}})
            transcript = []

            def run(*args, scope="operator", expected=0):
                proc = subprocess.run([str(cli), *map(str, args)], cwd=stage, env={**env, "GX_SCOPE": scope},
                                      text=True, capture_output=True, timeout=30)
                self.assertEqual(proc.returncode, expected, f"{args}: {proc.stderr} {proc.stdout}")
                value = json.loads(proc.stdout) if proc.stdout else {"diagnostic": proc.stderr.strip()}
                transcript.append({"command": ["bin/gx-p0", *[str(arg).replace(str(work), "<stage>") for arg in args]],
                                   "scope": scope, "exit_code": proc.returncode, "result": value})
                return value

            def plugin(*args, **options):
                return run("plugin", *args, **options)

            before = run("status", "--state", state, "--run-id", rid)
            self.assertEqual(before["run_id"], rid)
            self.assertEqual(before["workflow"], {"id": "science_grid", "version": "2"})
            self.assertEqual(plugin("list")["plugins"], [])
            digest = plugin("digest", EXAMPLE)["sha256"]
            self.assertRegex(digest, "^[0-9a-f]{64}$")
            target = f"{PLUGIN}@{VERSION}"
            directory = stage / "gx-h/plugins/installed" / PLUGIN / VERSION
            error = plugin("install", EXAMPLE, "--sha256", digest, scope="worker", expected=1)
            self.assertIn("operator", error["diagnostic"].lower())
            error = plugin("install", EXAMPLE, "--sha256", "0" * 64, expected=1)
            self.assertIn("digest", error["diagnostic"].lower())
            self.assertFalse(directory.exists())
            self.assertIn(target, plugin("verify", target, expected=1)["failures"])
            record = {"path": f"{PLUGIN}/{VERSION}", "sha256": digest}
            receipt = plugin("install", EXAMPLE, "--sha256", digest)
            self.assertEqual(receipt, {"id": PLUGIN, "version": VERSION, **record})
            self.assertEqual(plugin("install", EXAMPLE, "--sha256", digest), receipt)
            self.assertEqual(plugin("list"), {"plugins": [record]})
            saved = json.loads(catalog.read_bytes())
            self.assertEqual(saved.pop("plugins"), [record])
            self.assertEqual(saved, original)
            self.assertEqual(plugin("digest", directory)["sha256"], digest)
            verified = plugin("verify", target)
            self.assertEqual(verified["failures"], {})
            loaded, = verified["plugins"]
            self.assertEqual([loaded["id"], loaded["version"], loaded["sha256"]], [PLUGIN, VERSION, digest])
            self.assertTrue(loaded["namespace"].startswith("gx_ext_"))
            self.assertEqual(loaded["contributions"], {"workflows": [], "tools": ["hello_research"], "capabilities": []})

            changed = stage / EXAMPLE / "package/text/messages.py"
            changed.write_text("def greeting(name): return 'Unapproved revision'\n")
            new_digest = plugin("digest", EXAMPLE)["sha256"]
            self.assertNotEqual(new_digest, digest)
            plugin("install", EXAMPLE, "--sha256", new_digest, expected=1)
            self.assertEqual(plugin("list")["plugins"], [record])
            self.assertEqual(plugin("verify", target), verified)
            (directory / "package/text/messages.py").write_bytes(changed.read_bytes())
            self.assertIn("digest", plugin("verify", target, expected=1)["failures"][target].lower())
            self.assertEqual(run("status", "--state", state, "--run-id", rid), before)

            evidence = os.environ.get("GX_V3_PLUGIN_CLI_EVIDENCE")
            if evidence:
                path = Path(evidence)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(transcript, indent=2) + "\n")
