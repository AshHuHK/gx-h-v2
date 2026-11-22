"""Run the public job executor against short local Python commands on Linux."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from gx_core.jobs import executor, resource_guard, runner


class DetachedCpuJob(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gx-cpu-job-")
        self.addCleanup(self.temp.cleanup)
        self.job = executor.paths(Path(self.temp.name), "count-rows")
        self.job.create()

    def start(self, code, timeout):
        out = self.job.work_dir / "out"
        out.mkdir()
        limits = self.job.job_dir / "limits.json"
        limits.write_text(json.dumps({"holds": [], "switches": {}}))
        policy = resource_guard.policy_for(
            "count-rows@1", {"cpu_threads": 1, "wall_seconds": 3}, expected_gpu=None
        )
        executor.write_spec(self.job, {
            "schema": runner.SCHEMA,
            "job_id": self.job.job_dir.name,
            "argv": [sys.executable, "-I", "-S", "-B", "-c", code],
            "cwd": str(self.job.work_dir),
            "env": {
                "HOME": str(self.job.home_dir), "TMPDIR": str(self.job.tmp_dir),
                **policy["required_env"],
            },
            "timeout_seconds": timeout,
            "stdout_path": str(self.job.stdout), "stderr_path": str(self.job.stderr),
            "out_dir": str(out), "limits_path": str(limits), "resource_guard": policy,
            "kill_grace_seconds": 0.05, "heartbeat_seconds": 0.05,
        })
        executor.launch(self.job.job_dir)

    def await_result(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            seen = executor.observe(self.job.job_dir)
            if seen["state"] == executor.FINISHED:
                return seen["outcome"]
            time.sleep(0.05)
        self.fail("Detached runner did not write its outcome within ten seconds")

    def test_runner_produces_a_receipt_and_refuses_a_second_launch(self):
        self.start("print(2 + 3)", timeout=3)
        result = self.await_result()
        self.assertEqual((result["result"], result["exit_code"]), (runner.RESULT_EXITED, 0))
        self.assertEqual(self.job.stdout.read_text().strip(), "5")
        with self.assertRaises(executor.RunnerLaunchFailed):
            executor.launch(self.job.job_dir)
        self.assertEqual(executor.observe(self.job.job_dir)["outcome"], result)

    def test_runner_enforces_wall_time_and_records_process_cleanup(self):
        self.start("import time; time.sleep(10)", timeout=0.2)
        result = self.await_result()
        self.assertEqual(result["result"], runner.RESULT_TIMED_OUT)
        self.assertTrue(result["resource_guard"]["cleanup"]["proven_gone"])
