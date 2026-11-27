"""Run the public research command, interrupt it, then export an actual accepted close twice."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from acceptance.v622.test_close import SurveyedStudy, close_decision
from acceptance.v62a.interleavings_fixture import PRODUCT


class ResearchEntry(unittest.TestCase):
    def test_same_key_resumes_and_closed_reopen_needs_no_native_host_or_source_files(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            state = work/"state"
            question = "Which facts are established?\nWhat remains uncertain?\n"
            (work/"question.md").write_text(question)
            original = PRODUCT/"gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif"
            (work/"input.cif").write_bytes(original.read_bytes())
            source = PRODUCT/"examples/v621-coordinate-analysis/2RE9.json"
            (work/"descriptor.json").write_bytes(source.read_bytes())
            (work/"config.json").write_text(json.dumps({"input_files":[
                {"role":"structure", "path":"input.cif", "acquisition":"descriptor.json"}]}))
            base = [sys.executable, "-B", str(PRODUCT/"bin/gx-p0"), "research", "--state", str(state),
                    "--run-key","one","--timeout","0.001","--goal-config",str(work/"config.json")]
            env = {**os.environ, "GX_CODEX_OFFLINE":"1"}
            def invoke(*question_args, expected=0):
                run = subprocess.run([*base,*question_args], env=env, text=True, capture_output=True, timeout=30)
                self.assertEqual(run.returncode, expected, run.stderr)
                return json.loads(run.stdout) if run.stdout else run.stderr
            by_file = ["--goal-file",str(work/"question.md")]
            invoke(expected=2)
            invoke(*by_file, "--goal","Other", expected=2)
            first = invoke(*by_file, expected=75)
            self.assertEqual(first["goal"], question)
            resumed = invoke("--goal",question,expected=75)
            self.assertEqual(resumed["run_id"], first["run_id"])
            self.assertEqual(resumed["invocation"]["action"], "resume")
            self.assertIn("different Goal specification",invoke("--goal","Changed question",expected=1))
            with SurveyedStudy(work, reopen=True) as s:
                self.assertEqual(s.run_id, first["run_id"])
                obs, = s.conn.execute("SELECT payload_json FROM events WHERE run_id=? AND kind='v6.observation.added'",
                                      (s.run_id,)).fetchall()
                self.assertEqual(json.loads(obs[0])["artifact_ref"], s.materials[0])
                actor = s.next_actor("run")
                proposal = close_decision([s.materials[0]])
                proposal["stop_reason"] = "INSUFFICIENT_DATA"
                proposal["rationale"] = "The single structure supports a limited descriptive conclusion."
                closed = s.call(actor, "gx_decide_revision", {"decision":proposal}, "close")
                s.advance()
                before = s.conn.execute("SELECT count(*) FROM events").fetchone()[0]
            (work/"input.cif").unlink()
            (work/"descriptor.json").unlink()
            exported = invoke(*by_file)
            second = invoke(*by_file)
            self.assertEqual(second, exported)
            self.assertEqual(exported["invocation"]["added"], {"jobs":0, "host_turns":0})
            result = json.loads(Path(exported["result"]).read_text())
            self.assertEqual(result["goal"], question)
            self.assertEqual(result["closure"]["decision_ref"], closed["decision_ref"])
            self.assertEqual(result["final_decision"]["rationale"], proposal["rationale"])
            self.assertIn(proposal["rationale"], Path(exported["report"]).read_text())
            with closing(sqlite3.connect(state/"gx.db")) as db:
                self.assertEqual(before, db.execute("SELECT count(*) FROM events").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
