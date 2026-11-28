"""New public Goals freeze the native research contract; historical inputs remain resumable."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing

from gx_core.p0.research import _same_goal

ROOT = Path(__file__).resolve().parents[3]
POLICY = "gx.harness/2"


class ResearchPolicy(unittest.TestCase):
    def test_ordinary_cli_pins_policy_and_preserves_explicit_empty_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            config = state / "scope.json"
            config.write_text(json.dumps({"capabilities": [], "capability_options": [], "research_modules": []}))
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "bin/gx-p0"), "research", "--state", folder,
                 "--run-key", "frozen-input", "--goal", "Compare the limits of the two published rain records.",
                 "--goal-config", str(config), "--timeout", "0.001"],
                env={**os.environ, "GX_CODEX_OFFLINE": "1"}, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 75, result.stderr)
            rid = json.loads(result.stdout)["run_id"]
            with closing(sqlite3.connect(state / "gx.db")) as db:
                inputs = json.loads(db.execute("SELECT inputs_json FROM runs WHERE run_id=?", (rid,)).fetchone()[0])["inputs"]
                self.assertEqual(inputs.get("harness_policy"), POLICY)
                self.assertEqual(inputs["p0_spec"]["harness_policy"], POLICY)
                self.assertEqual(inputs["capability_catalog"], [])
                self.assertEqual(inputs["p0_spec"]["research_modules"], [])

    def test_reopening_pre_refactor_cli_preserves_original_request(self):
        old = {"objective": "Report a supported comparison.", "capabilities": [], "discovery_policy": "gx.catalog/1"}
        current = {**old, "harness_policy": POLICY}
        self.assertTrue(_same_goal(old, current))
        self.assertFalse(_same_goal(old, {**current, "objective": "A different question."}))
