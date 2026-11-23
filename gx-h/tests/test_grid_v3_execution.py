"""Science-grid proof across real local Jobs, frozen products, source edits and the ledger migration."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from examples.science_judgment import judge
from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid
from gx_core.jobs.service import JobService
from gx_core.native_release import native_state
from gx_core.orchestration import reactor, service
from gx_core.p0 import cli, driver, intent, roots, schema
from gx_core.persistence import store
from gx_core.teams.service import TeamService, backend_ctx, task_mapping
from workflows.science_grid_v3 import products, settlement


class GridExecution(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.enterContext(native_state(self.directory))

    def start(self, *, finite=False, version="3"):
        self.conn = store.open_store(self.state.db_path)
        self.addCleanup(self.conn.close)
        spec = cli.demo_spec()
        spec.update(workflow="science_grid", workflow_version=version, max_rounds=1)
        if finite:
            for path in spec["proposal"]["paths"]:
                path["claim_scope"] = "finite_grid_threshold"
        def bootstrap(rid):
            intent.bind_main(self.conn, rid, "main")
            intent.stage_input(self.conn, rid, "initial", cli.initial_action(spec))
            intent.accept_input(self.conn, rid, "main", "human-initial", "initial")
        self.run = service.run_start(driver.context(self.conn), {"objective": spec["objective"],
            "project_ref": "grid-science", "budget_profile": "p0", "workflow": {"id": "science_grid", "version": version},
            "inputs": {"p0": True, "proposal": spec["proposal"], "max_rounds": 1, "p0_spec": spec},
            "idempotency_key": "start"}, prepare_p0=bootstrap)["run_id"]
        for row in self.conn.execute("SELECT job_id FROM jobs WHERE run_id=?", (self.run,)).fetchall():
            JobService(self.conn).wait(row[0], timeout_seconds=15)
        self.advance()
        self.root = roots.Roots(self.conn, run_id=self.run, cwd=self.directory)
        self.assets = ArtifactService(self.conn)

    def advance(self):
        ctx = backend_ctx(self.conn)
        reactor.lazy(ctx, self.run)
        reactor.drive_science(ctx, self.run)

    def task(self, role):
        return self.conn.execute("SELECT * FROM tasks WHERE run_id=? AND role=? AND status='READY' ORDER BY rowid LIMIT 1",
                                 (self.run, role)).fetchone()

    def count(self, table):
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def claim(self, role):
        task = self.task(role)
        self.assertIsNotNone(task)
        row = self.root._claim(task["task_id"])
        if row is None:
            return None
        principal = f"{role}-{row['root_key']}"
        self.conn.execute("UPDATE p0_roots SET thread_id=? WHERE root_key=?", (principal, row["root_key"]))
        self.conn.execute("UPDATE leases SET principal=? WHERE lease_id=?", (principal, row["lease_id"]))
        task = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        return task_mapping(self.conn, task), principal

    def submit(self, owner, product):
        task, principal = owner
        result = TeamService(self.conn).submit_workflow_product(run_id=self.run, task_id=task["task_id"],
            lease_ref=task["lease_id"], brief_sha256=task["brief_sha256"] or "native", product=product,
            callback={"thread_id": principal, "turn_id": "official-turn", "call_id": "submission"})
        self.assertEqual(result["task_status"], "ACCEPTED", result)
        return result

    def review(self, task):
        prior = judge(self.assets, task["input"])
        return {"findings_by_path": {f["path_id"]: {"reason": f["reason"], "risk": "Limited to the sampled grid."}
                                     for f in prior["findings"]}}

    def test_explicit_finite_scope_skips_model_review_and_keeps_job_provenance(self):
        self.start(finite=True)
        self.assertIsNone(self.task("evidence_reviewer"))
        planner = self.claim("planner")
        ref = planner[0]["input"]["assessment_ref"]
        body = self.assets.read_json(ref)
        self.assertEqual(body["assessment_mode"], "deterministic")
        self.assertEqual({f["supported"] for f in body["findings"]}, {True, False})
        self.assertEqual(len(settlement.current_evidence(self.conn, self.run)), 2)
        self.assertEqual(self.count("evidence_reviews"), 0)
        self.submit(planner, judge(self.assets, planner[0]["input"]))
        self.advance()
        snapshot = driver.snapshot(self.conn, self.run)
        self.assertEqual(snapshot["status"], "COMPLETED")
        self.assertEqual(snapshot["rounds"][0]["assessment_ref"], ref)
        self.assertEqual(len(snapshot["current_evidence"]), 2)
        self.assertIsNone(self.conn.execute("SELECT reviewer_submission_id FROM science_decisions").fetchone()[0])
        self.assertEqual(self.count("jobs"), 2)
        self.assertEqual(self.count("science_settlements"), 2)

    def test_uncertain_claim_requires_an_independent_reviewer_and_structure_precedes_io(self):
        self.start()
        reviewer = self.claim("evidence_reviewer")
        task = reviewer[0]
        valid = self.review(task)
        wrong = {"findings_by_path": {}}
        corrupt_assignment = {**task, "input": {**task["input"], "proposal_ref": "missing-frozen-proposal"}}
        with self.assertRaises(SchemaInvalid) as caught:
            products.prepare_native_product(self.conn, corrupt_assignment, wrong)
        self.assertEqual(caught.exception.details["category"], "product_structure")
        self.assertEqual(caught.exception.details["contract_digest"], task["input"]["product_contract"]["digest"])
        before = self.count("artifacts")
        with self.assertRaises(SchemaInvalid):
            self.submit(reviewer, wrong)
        self.assertEqual(self.count("artifacts"), before)
        result = self.submit(reviewer, valid)
        self.advance()
        planner = self.claim("planner")
        self.assertEqual(planner[0]["input"]["assessment_ref"], result["artifact_ref"])
        self.submit(planner, judge(self.assets, planner[0]["input"]))
        self.advance()
        self.assertEqual(self.count("evidence_reviews"), 2)
        self.assertEqual(self.conn.execute("SELECT reviewer_submission_id FROM science_decisions").fetchone()[0],
                         result["submission_id"])

    def test_supplement_after_deterministic_assessment_reopens_real_review(self):
        self.start(finite=True)
        planner = self.task("planner")
        first_ref = store.loads(planner, "input_json")["assessment_ref"]
        intent.stage_input(self.conn, self.run, "new-source",
                           {"kind": "supplement", "text": "The offset sample calibration is disputed.", "source_ref": "log-7"})
        source = intent.accept_input(self.conn, self.run, "main", "human-source", "new-source")
        self.assertEqual(settlement.current_evidence(self.conn, self.run), [])
        self.assertIsNone(self.claim("planner"))
        reviewer = self.claim("evidence_reviewer")
        product = self.review(reviewer[0])
        path = next(iter(product["findings_by_path"]))
        product["source_reviews_by_id"] = {source["source_id"]: {"disposition": "pending",
            "reason": "The calibration concern affects this sampled route.", "compared_paths": [path], "qualifies_paths": [path]}}
        result = self.submit(reviewer, product)
        self.advance()
        planner = self.claim("planner")
        inputs = planner[0]["input"]
        self.assertEqual(inputs["assessment_ref"], result["artifact_ref"])
        self.assertNotEqual(inputs["assessment_ref"], first_ref)
        current = self.assets.read_json(inputs["assessment_ref"])
        self.assertIsNone(next(f for f in current["findings"] if f["path_id"] == path)["supported"])
        decision = judge(self.assets, inputs)
        decision["source_decisions"] = [{"source_id": source["source_id"], "artifact_ref": source["artifact_ref"],
            "disposition": "pending", "reason": "Resolve this calibration before further interpretation.",
            "evidence_refs": inputs["evidence_refs"]}]
        self.submit(planner, decision)
        self.advance()
        self.assertEqual(driver.snapshot(self.conn, self.run)["status"], "COMPLETED")
        self.assertEqual(len(settlement.current_evidence(self.conn, self.run)), 1)
        self.assertEqual(self.count("jobs"), 2)

    def test_populated_v2_ledger_survives_failed_then_replayed_migration(self):
        migrations = schema.migrations()
        selected = next(m for m in migrations if m[0].endswith("zz_v31_grid_evaluator.sql"))
        with patch.object(schema, "migrations", return_value=[m for m in migrations if m != selected]):
            self.start(version="2")
            reviewer = self.claim("evidence_reviewer")
            self.submit(reviewer, judge(self.assets, reviewer[0]["input"]))
            self.advance()
            planner = self.claim("planner")
            self.submit(planner, judge(self.assets, planner[0]["input"]))
            self.advance()
        tables = ["science_settlements", "science_decisions", "evidence_reviews", "evaluations", "jobs", "submissions"]
        before = {t: [dict(r) for r in self.conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}
        self.assertEqual(len(before["science_decisions"]), 1)
        broken = (*selected[:2], selected[2] + ["SELECT unknown_column FROM science_decisions"])
        with patch.object(schema, "migrations", return_value=[broken]), self.assertRaises(sqlite3.OperationalError):
            schema.install(self.conn)
        self.assertNotIn("assessment_ref", [c["name"] for c in self.conn.execute("PRAGMA table_info(science_settlements)")])
        schema.install(self.conn)
        schema.install(self.conn)
        for table in tables:
            after = [dict(r) for r in self.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            if table == "science_settlements":
                for row in after:
                    self.assertIsNone(row.pop("assessment_ref"))
            self.assertEqual(after, before[table], table)
        self.assertEqual(list(self.conn.execute("PRAGMA foreign_key_check")), [])
        self.assertEqual(driver.snapshot(self.conn, self.run)["workflow"]["version"], "2")


if __name__ == "__main__":
    unittest.main()
