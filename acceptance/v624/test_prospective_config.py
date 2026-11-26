"""A prospective endpoint selected at Goal admission remains visible to the Lead across restarts."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.p0 import v6_native, v6_prospective
from gx_core.persistence import store


ROOT = Path(__file__).resolve().parents[2]
SIGNED = "deposited-block-signed@1"
OPENING = {"capabilities": [SIGNED], "material_roles": ["structure"], "metric_ids": ["missed_present"]}


class ProspectiveConfig(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.directory / "state"

    def start(self, opening=None):
        config = {"project": "prospective-brief", "research_effort": "medium", "module_policy": {"brief_bytes": 4096},
                  "capabilities": [SIGNED], "plugins": ["org.gx.block-signed@1.0.0"],
                  "input_files": [{"role": "structure",
                                   "path": str(ROOT / "gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif"),
                                   "sha256": "8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b"}]}
        if opening is not None:
            config["prospective_opening"] = opening
        path = self.directory / "grant.json"
        path.write_text(json.dumps(config))
        # The long objective forces the normal bounded-packet path in the very first Lead task.
        args = [sys.executable, "-B", str(ROOT / "bin/gx-p0"), "start", "--state", str(self.state),
                "--run-key", "same-study", "--goal", "Which local measurements merit reporting? " * 160,
                "--goal-config", str(path)]
        return subprocess.run(args, cwd=ROOT, env={**os.environ, "GX_CODEX_OFFLINE": "1"}, capture_output=True, text=True)

    def brief(self, rid):
        with closing(store.connect(self.state / "gx.db")) as db:
            row, = db.execute("SELECT input_json FROM tasks WHERE kind='decide_revision' AND run_id=?", (rid,))
            packet = json.loads(row[0])["brief"]
            self.assertLessEqual(len((v6_native.PREFIX + ids.canonical_json(packet)).encode()), 4096)
            full = ArtifactService(db).read_json(packet["full_brief_ref"])
            self.assertGreater(len(ids.canonical_json(full).encode()), 4096)
            self.assertEqual(0, db.execute("SELECT count(*) FROM jobs WHERE run_id=?", (rid,)).fetchone()[0])
            self.assertEqual(0, db.execute("SELECT count(*) FROM p0_root_deliveries WHERE run_id=?", (rid,)).fetchone()[0])
            return packet, full, v6_prospective.opening_pending(db, rid)

    def test_original_and_compact_lead_packets_keep_registered_opening_and_same_run(self):
        started = self.start(OPENING)
        self.assertEqual(0, started.returncode, started.stderr)
        rid = json.loads(started.stdout)["run_id"]
        packet, full, pending = self.brief(rid)
        self.assertTrue(pending)
        selection = packet["prospective_opening"]
        self.assertEqual(selection, full["prospective_opening"])
        self.assertEqual(OPENING, {k: selection[k] for k in OPENING})
        self.assertIn("claim_ref", selection["instructions"])
        self.assertIn("adopt", selection["instructions"])
        restarted = self.start(OPENING)
        self.assertEqual(0, restarted.returncode, restarted.stderr)
        self.assertEqual(rid, json.loads(restarted.stdout)["run_id"])
        changed = self.start({k: v for k, v in OPENING.items() if k != "metric_ids"})
        self.assertNotEqual(0, changed.returncode)
        self.assertIn("different project specification", changed.stderr)
        self.assertEqual((packet, full, pending), self.brief(rid))

    def test_ordinary_goal_has_no_opening_in_either_packet(self):
        result = self.start()
        self.assertEqual(0, result.returncode, result.stderr)
        packet, full, pending = self.brief(json.loads(result.stdout)["run_id"])
        self.assertFalse(pending)
        self.assertNotIn("prospective_opening", packet)
        self.assertNotIn("prospective_opening", full)

    def test_metric_typo_cannot_create_a_goal_waiting_on_a_nonexistent_endpoint(self):
        result = self.start({**OPENING, "metric_ids": ["missed_preset"]})
        self.assertNotEqual(0, result.returncode)
        self.assertIn("metric", result.stderr.lower())
        with closing(sqlite3.connect(f"file:{self.state / 'gx.db'}?mode=ro", uri=True)) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM runs").fetchone()[0])
