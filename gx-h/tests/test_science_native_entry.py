"""Replay a model-authored proposal through the native project entry on a fresh store."""
import json
from pathlib import Path

from gx_core.artifacts import ArtifactService
from gx_core.jobs.service import JobService
from gx_core.project_followup import _markdown_terms, launch_brief
from tests import test_native_router as router


class ScientificProjectEntry(router.RouterFixture):
    def test_fresh_project_freezes_root_proposal_and_executes_its_grid(self):
        prompt = (Path(__file__).resolve().parents[1] / "examples/science_native/question.md").read_text()
        budget, _ = _markdown_terms(prompt)
        self.assertGreaterEqual(budget["job_submissions"], 1)
        template = json.loads(prompt.split("```json\n", 1)[1].split("\n```", 1)[0])
        # This frame is the model boundary. New scientific content comes from
        # the callback, independently of the installed example and driver.
        template["objective"] = "Does the right edge of the sampled interval meet the threshold?"
        path = template["inputs"]["proposal"]["paths"][0]
        path["id"] = "edge"
        path["prediction"] = "One of x=2.2 and x=2.7 meets the threshold. Two misses refute this grid."
        path["grid"]["candidates"] = [{"candidate_id": "near", "x": 2.2}, {"candidate_id": "far", "x": 2.7}]
        snapshot = self.state.directory / "project-prompt.md"
        snapshot.write_text(prompt)
        brief = launch_brief(snapshot, {"limits": budget}).read_text()
        self.assertTrue(brief.endswith(prompt))
        self.assertNotIn(str(snapshot), brief)
        written, run_id, resumed = self.run_session(arguments=template, prompt=brief, profile="native_v2",
                                                     project_continuation=True)
        self.assertFalse(resumed)
        turn = next(row for row in written if row.get("method") == "turn/start")
        delivered = turn["params"]["input"][0]["text"]
        self.assertIn(prompt, delivered)
        self.assertIn("score(x) = -(x - 2)^2", delivered)
        run = self.conn.execute("SELECT project_ref,workflow_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual(tuple(run), ("grid-science", "science_grid"))
        arts = ArtifactService(self.conn)
        proposal = self.conn.execute("SELECT artifact_ref FROM artifacts WHERE run_id=? AND kind='research_proposal'",
                                     (run_id,)).fetchone()
        self.assertIsNotNone(proposal)
        frozen = arts.read_json(proposal["artifact_ref"])
        self.assertEqual(frozen["paths"][0]["prediction"], path["prediction"])
        self.assertEqual(arts.read_json(frozen["paths"][0]["input_ref"]), path["grid"])
        jobs = self.conn.execute("SELECT job_id FROM jobs WHERE run_id=?", (run_id,)).fetchall()
        self.assertEqual(len(jobs), 1)
        observed = JobService(self.conn).wait(jobs[0]["job_id"], timeout_seconds=15)
        self.assertEqual(observed["status"], "SUCCEEDED", observed.get("failure"))
        raw = arts.read_json(observed["outputs"][0]["artifact_ref"])
        self.assertEqual([row["candidate_id"] for row in raw["observations"]], ["near", "far"])
        self.assertGreaterEqual(raw["observations"][0]["score"], frozen["threshold"])
        self.assertLess(raw["observations"][1]["score"], frozen["threshold"])
