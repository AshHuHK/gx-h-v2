"""Exercise frozen scoring, changed Intent and scientific settlement using local GX Jobs."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from examples.science_judgment import judge
from gx_core import budgets
from gx_core.artifacts import ArtifactService
from gx_core.domain import clock, ids
from gx_core.errors import SchemaInvalid
from gx_core.jobs.grid_workflow import observe_grid, submit_grid
from gx_core.jobs.service import JobService
from gx_core.p0 import intent, science
from gx_core.persistence import records, store
from workflows.science_grid import nodes


class ScienceLedger(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = store.open_store(Path(directory.name) / "gx.db")
        self.addCleanup(self.conn.close)
        self.assets = ArtifactService(self.conn)
        budget = budgets.create_root(self.conn, run_id=None, actor="test")
        self.run = records.create_run(self.conn, objective="Measure a numerical sample", project_ref="test",
                                     actor="test", workflow_id="science_grid", workflow_version="1",
                                     inputs={"inputs": {"p0": True}}, budget_ref=budget)
        for state in ("AUDITING", "PLANNING", "ACTIVE"):
            store.transition(self.conn, machine="run", subject_id=self.run, to_state=state, actor="test")
        intent.bind_main(self.conn, self.run, "test-main")
        self.change("evaluation", "evaluation_spec", 0, {"threshold": -0.25})
        self.pin, self.item, _ = science.prospective_spec(self.conn, self.run, {}, -0.25)

    def export(self):
        target = os.environ.get("GX_P0_SCIENCE_EVIDENCE")
        if not target:
            return
        snapshots = {
            "settlements": "SELECT * FROM science_settlements",
            "evaluations": "SELECT * FROM evaluations WHERE purpose='CANDIDATE'",
            "jobs": "SELECT job_id, status, scheduler_ref, outcome_json FROM jobs",
            "manifests": "SELECT job_id, admission_sha256, completion_sha256 FROM p0_execution_manifests",
            "dependencies": "SELECT * FROM p0_intent_dependencies WHERE object_type='evaluation'",
            "outbox": "SELECT outbox_id, idempotency_key, payload_json FROM outbox WHERE topic='p0.intent.reevaluate'",
        }
        result = {"run_id": self.run, "test": self._testMethodName,
                  "current_evidence": [r["evidence_id"] for r in science.current_evidence(self.conn, self.run)],
                  **{name: [dict(row) for row in self.conn.execute(query)] for name, query in snapshots.items()}}
        directory = Path(target)
        directory.mkdir(exist_ok=True, parents=True)
        (directory / (self._testMethodName + ".json")).write_text(json.dumps(result, indent=2) + "\n")

    def change(self, key, kind, version, value):
        mid = f"{key}:{version}"
        intent.stage_input(self.conn, self.run, mid, {"kind": "change", "items": [
            {"item_id": key, "item_type": kind, "expected_version": version, "value": value}]})
        return intent.accept_input(self.conn, self.run, "test-main", "turn:" + mid, mid)

    def grid(self, path, x, cancel=False):
        grid = {"synthetic": True, "method_id": "fixture-grid", "method_version": "1",
                "delay_seconds": 2 if cancel else 0, "candidates": [{"candidate_id": path, "x": x}]}
        source = self.document("grid", grid)
        binding = {"workflow_id": "science_grid", "node_id": "experiment", "evaluator": self.pin,
                   "path_id": path}
        job = submit_grid(self.conn, run_id=self.run, operation_id=path, input_ref=source, binding=binding)
        service = JobService(self.conn)
        if cancel:
            service.cancel(job["job_id"], reason="stop fixture backend")
        service.wait(job["job_id"], timeout_seconds=15)
        receipt = observe_grid(self.conn, job["job_id"])
        row = {"job_id": job["job_id"], "path_id": path, "input_ref": source, "status": receipt["status"],
               "observations_ref": receipt.get("observations_ref"), "failure": receipt.get("failure")}
        science.experiment_result(self.conn, self.run, row, {"evaluator": self.pin, "evaluation_item": self.item})
        return row

    def document(self, name, body, task=None):
        return self.assets.publish_bytes(run_id=self.run, namespace=self.run, rel_path=ids.new_id("artifact"),
            kind=name, media_type="application/json", data=json.dumps(body), task_id=task).artifact_ref

    def submission(self, inputs, author, product, rejected=False):
        task = records.create_task(self.conn, run_id=self.run, kind="research_round", role="evidence_reviewer",
                                   actor="backend", task_input=inputs)
        lease = store.issue_lease(self.conn, task_id=task, worker_role="evidence_reviewer", owner=author,
                                  attempt=1, seconds=120)["lease_id"]
        self.conn.execute("UPDATE leases SET principal=? WHERE lease_id=?", (author, lease))
        ref = self.document(inputs["output_kind"], product, task)
        submission = ids.new_id("submission")
        mapping = {"input": inputs, "task_id": task, "run_id": self.run, "lease_id": lease,
                   "submission_artifact_refs": [ref]}
        if rejected:
            with self.assertRaises(SchemaInvalid):
                nodes.validate(self.conn, mapping, None)
            state = "REJECTED"
        else:
            nodes.validate(self.conn, mapping, None)
            state = "ACCEPTED"
        # Minimal accepted submission fixture. The official root and its complete transition path
        # are exercised separately by the public CLI acceptance.
        self.conn.execute("UPDATE tasks SET status=?,attempt=1 WHERE task_id=?", (state, task))
        self.conn.execute("INSERT INTO submissions(submission_id,task_id,attempt,lease_id,result,"
                          "artifact_refs_json,created_at) VALUES(?,?,1,?,?,?,?)",
                          (submission, task, lease, state, json.dumps([ref]), clock.now_iso()))
        return {"task_id": task, "submission_id": submission, "artifact_ref": ref}

    def test_four_classes_and_reviewed_evidence(self):
        rows = [self.grid("valid", -2), self.grid("bad-data", "invalid"), self.grid("lost", 2, cancel=True)]
        paths = []
        for row in rows:
            path = {"id": row["path_id"], "prediction": "some point reaches -0.25",
                    "expected_reaches_threshold": True}
            path["hypothesis_id"] = science.register_hypothesis(self.conn, self.run, path, "author")
            paths.append(path)
        proposal = self.document("research_proposal", {"paths": paths, "author_principal": "author",
            "threshold": -0.25, "evaluation_item": self.item, "evaluator": self.pin})
        raw = self.document("research_evidence", {"routes": rows})
        inputs = {"proposal_ref": proposal, "evidence_refs": [raw], "output_kind": "science_assessment",
                  "p0": True, "round": 0, "max_rounds": 3}
        review = judge(self.assets, inputs)
        self.assertEqual([r["supported"] for r in review["findings"]], [False, None, None])
        wrong = {**review, "findings": [{**r, "supported": True} for r in review["findings"]]}
        self.submission(inputs, "first-worker", wrong, rejected=True)
        result = self.submission(inputs, "reviewer", review)
        ctx = SimpleNamespace(conn=self.conn, run_id=self.run, round=0, outputs={
            "proposal": {"proposal_ref": proposal}, "experiment": {"evidence_ref": raw}})
        science.settle_assessment(ctx, result)
        science.settle_assessment(ctx, result)
        kinds = dict(self.conn.execute("SELECT kind,count(*) FROM science_settlements GROUP BY kind"))
        self.assertEqual(kinds, {k: 1 for k in ("valid_scientific_outcome", "measurement_failure",
                                               "agent_execution_failure", "infrastructure_failure")})
        current, = science.current_evidence(self.conn, self.run)
        self.assertEqual(current["direction"], "refutes")
        self.assertEqual(current["job_id"], rows[0]["job_id"])
        self.change("methods", "method_ban", 0, {"forbidden_capabilities": ["fixture-grid"]})
        self.assertFalse(science.current_evidence(self.conn, self.run))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM evidence").fetchone()[0], 1)
        self.export()

    def test_criterion_changes_before_original_evaluation_commits(self):
        self.change("evaluation", "evaluation_spec", 1, {"threshold": 1})
        self.assertFalse(self.conn.execute("SELECT 1 FROM outbox WHERE topic='p0.intent.reevaluate'").fetchone())
        row = self.grid("already_frozen", 2)
        self.assertEqual(row["outcome_kind"], "valid_scientific_outcome", row)
        pin = intent.snapshot(self.conn, self.run, "evaluation", row["eval_id"])["evaluation"]
        self.assertEqual(pin["version"], 1)
        work, = self.conn.execute("SELECT payload_json FROM outbox WHERE topic='p0.intent.reevaluate'").fetchall()
        self.assertEqual(json.loads(work[0])["item_version"], 2)
        new = science.reevaluate_intent(self.conn, json.loads(work[0]))
        self.assertEqual(new["observations_ref"], row["observations_ref"])
        self.assertEqual(self.assets.read_json(new["evaluation_ref"])["count_at_or_above"], 0)
        self.assertEqual(self.assets.read_json(row["evaluation_ref"])["count_at_or_above"], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        self.export()

    def test_changed_criterion_reuses_original_bytes_and_job(self):
        row = self.grid("sample", 2)
        self.assertEqual(row["outcome_kind"], "valid_scientific_outcome", row)
        self.change("evaluation", "evaluation_spec", 1, {"threshold": 0.5})
        work, = self.conn.execute("SELECT payload_json FROM outbox WHERE topic='p0.intent.reevaluate'").fetchall()
        payload = json.loads(work[0])
        new = science.reevaluate_intent(self.conn, payload)
        self.assertEqual(new, science.reevaluate_intent(self.conn, payload))
        self.assertNotEqual(new["eval_id"], row["eval_id"])
        self.assertEqual(new["observations_ref"], row["observations_ref"])
        self.assertEqual(self.assets.read_json(new["evaluation_ref"])["count_at_or_above"], 0)
        self.assertEqual(self.assets.read_json(row["evaluation_ref"])["count_at_or_above"], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM evaluations WHERE purpose='CANDIDATE'").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM evidence").fetchone()[0], 0)
        self.export()
