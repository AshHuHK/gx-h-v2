"""Real GX artifact store and Cordis subprocess; no model requests."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from gx_core import budgets
from gx_core.artifacts import ArtifactService
from gx_core.capabilities import environments, registry
from gx_core.context import Ctx, Principal
from gx_core.jobs.service import JobService, PURPOSE_WORKFLOW
from gx_core.errors import ArtifactNotFound, SchemaInvalid
from gx_core.persistence import records, store
from gx_core.plugins.evaluator import HOST, evaluate_scores, plugin_ref
from tests import test_local_job


class ScoreEvaluatorTest(unittest.TestCase):
    def test_pinned_input_version_relaunch_and_lost_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "gx.sqlite3"
            conn = store.open_store(db)
            self.addCleanup(lambda: conn.close())
            runs = [records.create_run(conn, actor="test", project_ref=p, objective="measure grid")
                    for p in ("A", "B")]
            raw = ArtifactService(conn).publish_bytes(run_id=runs[0], namespace=runs[0], rel_path="raw.json",
                kind="measurements", data=json.dumps({"observations": [{"score": -3}, {"score": 0}]}))
            params = dict(run_id=runs[0], observations_ref=raw.artifact_ref, attempt_id="measure-1",
                          threshold=-0.25, plugin_ref=plugin_ref("1"))
            with self.assertRaises(ArtifactNotFound):
                evaluate_scores(conn, **{**params, "run_id": runs[1]})
            # A missing pinned implementation blocks this attempt and keeps its original lock.
            broken = Path(directory) / "installed-host"
            shutil.copytree(HOST, broken)
            (broken / "score-v1.ts").unlink()
            with patch("gx_core.plugins.evaluator.HOST", broken):
                with self.assertRaises(FileNotFoundError):
                    evaluate_scores(conn, **params)
            conn.close()
            conn = store.open_store(db)
            second = evaluate_scores(conn, **{**params, "attempt_id": "measure-2", "plugin_ref": plugin_ref("2")})
            self.assertEqual(second["min_score"], -3)
            first = evaluate_scores(conn, **params)
            self.assertNotIn("min_score", first)
            self.assertEqual(first["count_at_or_above"], 1)
            self.assertEqual(ArtifactService(conn).read_json(first["plugin_lock_ref"])["plugin_ref"], plugin_ref("1"))
            for changed in ({"threshold": 0}, {"plugin_ref": plugin_ref("2")}):
                with self.assertRaises(SchemaInvalid):
                    evaluate_scores(conn, **{**params, **changed})
            conn.close()
            conn = store.open_store(db)
            with patch("subprocess.run", side_effect=AssertionError("duplicated completed evaluation")):
                self.assertEqual(first, evaluate_scores(conn, **params))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM artifacts WHERE kind='score_evaluation'").fetchone()[0], 2)


    def test_host_disposes_while_gx_job_keeps_its_owner_and_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "gx.sqlite3"
            conn = store.open_store(db)
            self.addCleanup(lambda: conn.close())
            actor = "test"
            ctx = Ctx(conn=conn, tool="tests", principal=Principal(subject=actor, scope="operator"))
            environments.register(conn, {"env_id": "control-plane-python", "kind": "SYSTEM_BINARY",
                "python_executable": sys.executable}, actor=actor)
            bundle = json.loads(test_local_job.SELFTEST.read_text())
            for name in ("parser", "entry_script"):
                bundle[name] = registry.publish_script(ctx, bundle[name])
            candidate = registry.register_candidate(ctx, bundle)
            test_local_job.LocalJobLifecycle().accept_local_utility(ctx, candidate)
            budget = budgets.create_root(conn, run_id=None, actor=actor)
            run = records.create_run(conn, objective="collect while evaluating", project_ref="project-A",
                                     actor=actor, budget_ref=budget)
            for state in ("AUDITING", "PLANNING", "ACTIVE"):
                store.transition(conn, machine="run", subject_id=run, to_state=state, actor=actor)
            artifacts = ArtifactService(conn)
            raw = artifacts.publish_bytes(namespace=run, rel_path="sample.json", run_id=run,
                kind="measurements", data='{"observations":[{"score":2}]}')
            manifest = artifacts.make_input_manifest(namespace=run, run_id=run,
                entries=[{"role": "text", "artifact_ref": raw.artifact_ref}])
            jobs = JobService(conn, actor=actor)
            job = jobs.submit(run_id=run, purpose=PURPOSE_WORKFLOW, capability_version=candidate["capability_version"],
                input_manifest_ref=manifest.artifact_ref, parameters={"mode": "sleep", "seconds": 2},
                idempotency_key="project-owned")
            deadline = time.monotonic() + 5
            while True:
                current = jobs.status(job["job_id"])["status"]
                if current == "RUNNING":
                    break
                self.assertIn(current, ("SUBMITTED", "QUEUED"))
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            params = dict(run_id=run, attempt_id="observe", plugin_ref=plugin_ref("2"),
                          threshold=0, observations_ref=raw.artifact_ref)
            result = evaluate_scores(conn, **params)
            self.assertEqual(jobs.status(job["job_id"])["status"], "RUNNING")
            # Drop caller state after both the plugin and project Job have returned receipts.
            conn.close()
            conn = store.open_store(db)
            self.assertEqual(evaluate_scores(conn, **params), result)
            final = JobService(conn, actor=actor).wait(job["job_id"], timeout_seconds=20)
            self.assertEqual(final["status"], "SUCCEEDED", final)
            count = conn.execute("SELECT COUNT(*) FROM outbox WHERE idempotency_key=?",
                                 ("job.terminal:" + job["job_id"],)).fetchone()[0]
            self.assertEqual(count, 1)
