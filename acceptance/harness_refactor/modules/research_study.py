"""Module tests use the ordinary research command and drive only its model callbacks offline."""
import json
import os
import subprocess
import sys

from acceptance.v622.test_close import SurveyedStudy
from acceptance.v62a.interleavings_fixture import PRODUCT


class ResearchStudy(SurveyedStudy):
    def _start_goal(self):
        material = self.directory / "material.txt"
        material.write_text("Public archive index, observation A17\n")
        budget = {"job_submissions":20, "worker_tasks":48, "cpu_seconds":1200,
                  "api_calls":48, "tokens":120000, "gpu_jobs":0, "gpu_minutes":0, "wall_hours":2}
        spec = {"input_files":[{"role":"material", "path":str(material)}],
                "run_budget":budget, "workspace_budget":budget, "turn_token_budget":1000,
                **self.spec_updates}
        config = self.directory / "goal.json"
        config.write_text(json.dumps(spec))
        cmd = [sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "research", "--state", str(self.state),
               "--goal", "What can be concluded from the provided public archives?", "--goal-config", str(config),
               "--run-key", "module-autonomy", "--timeout", "0.001"]
        result = subprocess.run(cmd, env={**os.environ, "GX_CODEX_OFFLINE":"1"},
                                text=True, capture_output=True, timeout=30)
        if result.returncode not in {0, 75} or json.loads(result.stdout).get("goal_status") != "OPEN":
            raise AssertionError(f"research failed ({result.returncode}): {result.stderr} {result.stdout}")
