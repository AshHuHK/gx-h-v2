"""Cold recovery across real caller processes with two bounded synthetic GX CPU Jobs."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FixtureRecovery(unittest.TestCase):
    def test_lost_caller_preserves_original_job_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            def command(*args, expected=0):
                call = subprocess.run([sys.executable, "-B", "-m", "examples.p01_fixture",
                                       "--state-dir", directory, *args], cwd=ROOT, timeout=45,
                                      text=True, capture_output=True)
                self.assertEqual(call.returncode, expected, call.stderr)
                return json.loads(call.stdout) if call.stdout else None

            def database_image():
                db = Path(directory, "p01.sqlite").as_uri() + "?mode=ro"
                with closing(sqlite3.connect(db, uri=True)) as conn:
                    return tuple(conn.iterdump())

            command("start", "--interrupt-after-submit", expected=75)
            before = command("status")
            [first] = before["jobs"]
            self.assertIsNone(before["decision_ref"])
            finished = command("resume")
            self.assertEqual(before["tasks"], finished["tasks"])
            self.assertEqual(finished["jobs"][0]["job_id"], first["job_id"])
            self.assertEqual(finished["jobs"][0]["scheduler_ref"], first["scheduler_ref"])
            self.assertEqual([j["status"] for j in finished["jobs"]], ["SUCCEEDED", "SUCCEEDED"])
            evidence = finished["decision"]["evidence"]
            self.assertEqual([r["best_score"] for r in evidence], [0, -12.25])
            self.assertEqual(len({r["artifact_ref"] for r in evidence}), 2)
            self.assertEqual(finished["decision"]["reconsider"], ["path-b"])
            self.assertEqual(finished["decision"]["continue"], ["path-a"])

            snapshot = database_image()
            self.assertEqual(command("status"), finished)
            self.assertEqual(command("status"), finished)
            self.assertEqual(database_image(), snapshot)
            self.assertEqual(command("resume"), finished)
