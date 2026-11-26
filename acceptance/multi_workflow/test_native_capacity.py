"""Run Goal CLI root-limit inputs through the real lease admission transaction."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from gx_core.errors import HoldActive
from gx_core.p0.roots import Roots
from gx_core.persistence import store
from gx_core.teams.service import TeamService

ROOT = Path(__file__).resolve().parents[2]


def start(directory, spec):
    path = directory / "grant.json"
    path.write_text(json.dumps(spec))
    return subprocess.run(
        [sys.executable, "-B", str(ROOT / "bin/gx-p0"), "start", "--state", str(directory / "state"),
         "--run-key", "capacity", "--goal", "Compare the available measurements.", "--goal-config", str(path)],
        env={**os.environ, "GX_CODEX_OFFLINE": "1"}, capture_output=True, text=True, timeout=25,
    )


class GoalCapacity(unittest.TestCase):
    def test_rejected_inputs(self):
        for key, values, reason in [
            ("run", [0, -1, False, True, 2.0, "2", None], "run max_parallel_roots must be a positive integer"),
            ("workspace", [0, 11], "workspace.max_parallel_roots must be an integer from 1 through 10"),
        ]:
            for value in values:
                with self.subTest(key=key, value=value), TemporaryDirectory() as tmp:
                    spec = {"max_parallel_roots": value}
                    if key == "workspace":
                        spec = {"workspace": spec}
                    started = start(Path(tmp), spec)
                    self.assertNotEqual(started.returncode, 0)
                    self.assertIn(reason, started.stderr)
                    with closing(store.connect(Path(tmp) / "state/gx.db")) as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
                    print(json.dumps(dict(spec=spec, exit=started.returncode, rolled_back=True, reason=reason)), flush=True)

    def test_claims_obey_both_limits(self):
        for run_cap, workspace_cap, admitted, blocker in [
            (None, None, 10, "workspace"), (1, None, 1, "run"), (10, None, 10, "workspace"),
            (11, None, 10, "workspace"), (11, 2, 2, "workspace"), (1000, 1, 1, "workspace"),
        ]:
            with self.subTest(run=run_cap, workspace=workspace_cap), TemporaryDirectory() as tmp:
                directory = Path(tmp)
                spec = {} if run_cap is None else {"max_parallel_roots": run_cap}
                if workspace_cap is not None:
                    spec["workspace"] = {"max_parallel_roots": workspace_cap}
                started = start(directory, spec)
                self.assertEqual(started.returncode, 0, started.stderr)
                rid = json.loads(started.stdout)["run_id"]
                with closing(store.connect(directory / "state/gx.db")) as db, \
                        patch("gx_core.p0.roots.tempfile.gettempdir", return_value=tmp):
                    roots = Roots(db, run_id=rid, cwd=directory)
                    lead, = db.execute("SELECT task_id FROM v6_decision_tasks WHERE run_id=?", (rid,))
                    tasks = [lead[0]]
                    for _ in range(admitted):
                        task = TeamService(db).create_task(
                            run_id=rid, kind="research_round", role="evidence_reviewer", bundle_kind="research_bundle",
                            input={}, acceptance={},
                        )
                        tasks.append(task["task_id"])
                    for tid in tasks[:-1]:
                        self.assertTrue(roots._claim(tid)["lease_id"])
                    with self.assertRaises(HoldActive) as hold:
                        roots._claim(tasks[-1])
                    details = hold.exception.details
                    self.assertEqual((blocker, admitted, admitted), (details["scope"], details["limit"], details["occupied"]))
                    self.assertEqual(admitted, db.execute("SELECT count(*) FROM leases").fetchone()[0])
                    self.assertEqual(0, db.execute("SELECT count(*) FROM p0_root_deliveries").fetchone()[0])
                    print(json.dumps(dict(spec=spec, admitted=admitted, refusal=details, host_requests=0)), flush=True)


if __name__ == "__main__":
    unittest.main()
