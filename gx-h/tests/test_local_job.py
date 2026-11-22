"""A registered local adapter produces durable Job outputs across a coordinator restart."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from gx_core import budgets
from gx_core.capabilities import environments, ladder, registry, storage
from gx_core.context import Ctx, Principal
from gx_core.errors import IdempotencyConflict
from gx_core.governance import assets, checks, reviews
from gx_core.jobs.service import JobService, PURPOSE_WORKFLOW
from gx_core.persistence import records, store


SELFTEST = Path(__file__).resolve().parents[1] / "adapters/local/selftest/manifests/selftest.json"


class LocalJobLifecycle(unittest.TestCase):
    def accept_local_utility(self, ctx, candidate):
        """Exercise activation with a test reviewer standing in for the local operator."""
        capability = candidate["capability_version"]
        subject = ladder.subject_of(registry.candidate(ctx.conn, capability))
        reviewer = Ctx(conn=ctx.conn, tool="tests", principal=Principal(
            subject="fixture-reviewer", scope="operator", source="interactive_terminal"))
        for scan in checks.run_checks(ctx, subject, ["scope_scan"]):
            for item in scan["diagnostics"]:
                checks.adjudicate(reviewer, check_run_id=scan["check_run_id"],
                                  fingerprint=item["fingerprint"], decision="ACCEPTED", person=True,
                                  note="Test fixture for review of the bundled local echo utility.")
        reviews.record_human_review(reviewer, subject=subject, verdict="APPROVE",
                                    note="Test fixture for review of the bundled local echo utility.")
        ladder.advance(ctx, capability)
        def promote(state):
            return assets.promote(ctx, "selftest", "1", state, candidate["manifest_sha256"])
        promote("REVIEWED")
        deadline = time.monotonic() + 30
        while True:
            result = ladder.advance(ctx, capability)
            if "still running" not in (result["stopped"] or ""):
                break
            self.assertLess(time.monotonic(), deadline, result["stopped"])
            time.sleep(0.1)
        self.assertIsNone(result["stopped"])
        for state in ("VALIDATED", "APPROVED", "ACTIVE"):
            promote(state)

    def test_restart_reconciles_the_original_job_and_publishes_its_outputs_once(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "gx.db"
            conn = store.open_store(db)
            try:
                actor = "local-job-test"
                ctx = Ctx(conn=conn, principal=Principal(subject=actor, scope="operator"), tool="tests")
                environments.register(conn, {"env_id": "control-plane-python", "kind": "SYSTEM_BINARY",
                                             "python_executable": sys.executable}, actor=actor)
                bundle = json.loads(SELFTEST.read_text(encoding="utf-8"))
                for name in ("parser", "entry_script"):
                    bundle[name] = registry.publish_script(ctx, bundle[name])
                candidate = registry.register_candidate(ctx, bundle)
                self.accept_local_utility(ctx, candidate)
                budget = budgets.create_root(conn, run_id=None, actor=actor)
                with store.transaction(conn) as tx:
                    run = records.create_run(tx, objective="Exercise a local job", project_ref="example",
                                             actor=actor, budget_ref=budget)
                    for state in ("AUDITING", "PLANNING", "ACTIVE"):
                        store.transition(tx, machine="run", subject_id=run, to_state=state, actor=actor)
                artifacts = storage.artifact_service(conn)
                text = artifacts.publish_bytes(namespace="example/input", rel_path="input.txt",
                                               kind="job_input", run_id=run, data=b"hello")
                inputs = artifacts.make_input_manifest(namespace="example/input", rel_path="inputs.json",
                                                       run_id=run, entries=[{"role": "text",
                                                                           "artifact_ref": text.artifact_ref}])
                request = {"run_id": run, "purpose": PURPOSE_WORKFLOW, "input_manifest_ref": inputs.artifact_ref,
                           "capability_version": candidate["capability_version"],
                           "parameters": {"mode": "echo", "seconds": 0}, "idempotency_key": "echo-once"}
                first = JobService(conn, actor=actor).submit(**request)
            finally:
                conn.close()

            conn = store.open_store(db)
            try:
                jobs = JobService(conn, actor=actor)
                job_id = first["job_id"]
                result = jobs.wait(job_id, timeout_seconds=15)
                self.assertEqual(result["status"], "SUCCEEDED", result["failure"])
                artifacts = storage.artifact_service(conn)
                outputs = {item["output_name"]: item for item in result["outputs"]}
                self.assertEqual(artifacts.read_bytes(outputs["report.txt"]["artifact_ref"]), b"echo\nHELLO")
                report = artifacts.read_json(outputs["report"]["artifact_ref"])
                self.assertEqual((report["characters"], report["job_id"]), (5, job_id))
                self.assertEqual(report["manifest_sha256"], candidate["manifest_sha256"])
                self.assertFalse(outputs["report"]["validated"])
                self.assertIn(b"selftest: done echo", artifacts.read_bytes(outputs["stdout"]["artifact_ref"]))
                replay = jobs.submit(**request)
                self.assertTrue(replay["replayed"])
                self.assertEqual(replay["job_id"], job_id)
                self.assertEqual(jobs.status(job_id)["artifact_refs"], result["artifact_refs"])
                with self.assertRaises(IdempotencyConflict):
                    jobs.submit(**{**request, "parameters": {"mode": "silent", "seconds": 0}})
                job_rows = conn.execute("SELECT reservation_ref FROM jobs WHERE run_id=?", (run,)).fetchall()
                self.assertEqual(len(job_rows), 1)
                hold = conn.execute("SELECT status FROM budget_reservations WHERE reservation_ref=?",
                                    (job_rows[0]["reservation_ref"],)).fetchone()
                self.assertEqual(hold["status"], "SETTLED")
                terminals = conn.execute("SELECT count(*) FROM outbox WHERE idempotency_key=?",
                                         (f"job.terminal:{job_id}",)).fetchone()[0]
                self.assertEqual(terminals, 1)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
