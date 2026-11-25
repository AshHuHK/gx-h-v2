"""A medium Goal with a large Unicode objective can reopen and read its bounded Lead brief."""
from contextlib import closing
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.native_release import native_state
from gx_core.p0 import cli, v6_native
from gx_core.p0.roots import Roots
from gx_core.persistence import store


class LeadDelivery(unittest.TestCase):
    def test_large_frozen_packet_is_bounded_and_original_is_readable(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            goal = "Inspect the observations and report coverage. " + "數據範圍與方法。"*500
            spec = {"project": "large-packet", "mode": "goal", "objective": goal, "capabilities": [],
                    "research_effort": "medium", "module_policy": {"brief_bytes": 4096}}
            with native_state(folder):
                with closing(store.open_store(folder/"gx.db")) as db:
                    rid = cli.start(db, folder, spec, run_key="large-brief")["run_id"]
                with closing(store.open_store(folder/"gx.db")) as db:
                    task = db.execute("SELECT * FROM tasks WHERE run_id=? AND kind='decide_revision'", (rid,)).fetchone()
                    frozen = json.loads(task["input_json"])["brief"]
                    self.assertLessEqual(len((v6_native.PREFIX+ids.canonical_json(frozen)).encode()), 4096)
                    self.assertTrue(frozen["module_catalog_available"])
                    original = ArtifactService(db).read_json(frozen["full_brief_ref"])
                    self.assertEqual(original["goal"], goal)
                    self.assertEqual(original["run_id"], rid)
                    roots = Roots(db, run_id=rid, cwd=folder)
                    with patch("gx_core.p0.roots.tempfile.gettempdir", return_value=tmp):
                        row = roots._claim(task["task_id"])
                    self.assertEqual(v6_native.brief(row), frozen)
                    self.assertIn("gx_open_module", json.loads(row["tool_names_json"]))
                    roots._artifact_access(row, frozen["full_brief_ref"])
