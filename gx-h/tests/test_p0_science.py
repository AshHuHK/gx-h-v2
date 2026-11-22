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
from gx_core.p0 import intent, science, science_refresh
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

    def test_replacing_objective_preserves_prior_results_and_stops_the_old_plan(self):
        self.change("objective", "objective", 0, "Find qualifying sampled points")
        intent.bind_dependencies(self.conn, self.run, "workflow", self.run, {"objective":"context","evaluation":"evaluation"})
        row = self.grid("selected", 2)
        path = {"id":"selected", "prediction":"some score qualifies", "expected_reaches_threshold":True}
        path["hypothesis_id"] = science.register_hypothesis(self.conn, self.run, path, "test-main")
        proposal = {"objective_item":{"item_id":"objective", "version":1}, "paths":[path], "threshold":-0.25,
                    "evaluation_item":self.item, "evaluator":self.pin, "author_principal":"test-main"}
        proposal_ref = self.document("research_proposal",proposal)
        raw = self.document("research_evidence",{"proposal_ref":proposal_ref,"routes":[row]})
        inputs = {"p0":True,"output_kind":"science_assessment","proposal_ref":proposal_ref,"evidence_refs":[raw],
                  "round":0,"max_rounds":3}
        review = self.submission(inputs,"reviewer",judge(self.assets,inputs))
        ctx = SimpleNamespace(conn=self.conn,run_id=self.run,round=0,outputs={
            "proposal":{"proposal_ref":proposal_ref},"experiment":{"evidence_ref":raw}})
        science.settle_assessment(ctx,review)
        self.assertEqual(len(science.current_evidence(self.conn,self.run)),1)
        planner = records.create_task(self.conn,run_id=self.run,kind="research_round",role="planner",actor="backend",
            task_input={**inputs,"output_kind":"science_decision","assessment_ref":review["artifact_ref"]})
        store.transition(self.conn,machine="task",subject_id=planner,to_state="READY",actor="backend")
        science_refresh.bind_task(self.conn,self.run,planner,proposal,"decision")
        preview = self.change("objective","objective",1,"Investigate a different response variable")
        self.assertEqual(preview["status"],"awaiting_confirmation")
        intent.stage_input(self.conn,self.run,"confirm-goal",{"kind":"confirm","change_id":preview["change_id"],
                                                            "preview_sha256":preview["preview_sha256"]})
        intent.accept_input(self.conn,self.run,"test-main","confirm-goal-turn","confirm-goal")
        task = self.conn.execute("SELECT * FROM tasks WHERE task_id=?",(planner,)).fetchone()
        with store.transaction(self.conn):
            self.assertFalse(science_refresh.prepare_dispatch(self.conn,self.run,task))
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE run_id=?",(self.run,)).fetchone()[0],"PAUSED")
        self.assertFalse(science.current_evidence(self.conn,self.run))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM evidence").fetchone()[0],1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0],1)
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


class CriterionDuringReview(unittest.TestCase):
    def test_changed_assessment_and_decision_use_fresh_products(self):
        from gx_core.p0 import cli, driver, roots
        from gx_core.orchestration import reactor, service
        from gx_core.teams.service import backend_ctx
        from gx_core.errors import ScopeDenied
        from gx_core.native_release import native_state
        with tempfile.TemporaryDirectory() as tmp, native_state(Path(tmp)) as state:
            conn = store.open_store(state.db_path)
            self.addCleanup(conn.close)
            spec = cli.demo_spec()
            inputs = {"p0":True,"proposal":spec["proposal"],"max_rounds":1}
            def change(rid, msg, action):
                intent.stage_input(conn,rid,msg,action)
                return intent.accept_input(conn,rid,"main",msg+"-turn",msg)
            def bootstrap(rid):
                intent.bind_main(conn,rid,"main")
                change(rid,"initial",cli.initial_action(spec))
            assigned = service.run_start(driver.context(conn), {"objective":spec["objective"],
                "project_ref":"criterion-during-work","budget_profile":"p0","workflow":{"id":"science_grid","version":"1"},
                "inputs":inputs,"idempotency_key":"criterion-run"},prepare_p0=bootstrap)
            run = assigned["run_id"]
            gx = backend_ctx(conn)
            for row in conn.execute("SELECT job_id FROM jobs WHERE run_id=?", (run,)).fetchall():
                JobService(conn).wait(row["job_id"],timeout_seconds=15)
            def advance():
                reactor.lazy(gx,run)
                reactor.drive_science(gx,run)
            advance()
            root = roots.Roots(conn,run_id=run,cwd=tmp)
            def task(stage):
                return conn.execute("SELECT * FROM tasks WHERE stage=? AND run_id=? ORDER BY rowid DESC",
                                    (stage,run)).fetchone()
            def claim(task_id,thread_id):
                binding = root._claim(task_id)
                with store.transaction(conn):
                    conn.execute("UPDATE p0_roots SET thread_id=? WHERE root_key=?", (thread_id,binding["root_key"]))
                    conn.execute("UPDATE leases SET principal=? WHERE lease_id=?", (thread_id,binding["lease_id"]))
                return roots.require_binding(conn,run,thread_id)
            def submit(binding):
                packet = json.loads(binding["brief"].split("\n",1)[1])
                product = judge(ArtifactService(conn),packet["input"])
                with store.transaction(conn):
                    root._submit(binding,product,{"thread_id":binding["thread_id"],"turn_id":"fixture",
                                                 "call_id":ids.new_id("request")})
                return packet, product
            def update(version, threshold):
                change(run,"update-"+str(version),{"kind":"change","items":[{"item_id":"evaluation",
                    "item_type":"evaluation_spec","expected_version":version,"value":{"threshold":threshold}}]})
            reviewer = task("assessment")
            deps = intent.snapshot(conn,run,"task",reviewer["task_id"])
            self.assertEqual({key: value["role"] for key,value in deps.items()},
                             {"objective":"context","evaluation":"evaluation"})
            first = claim(reviewer["task_id"],"review-v1")
            update(1,0.5)
            with self.assertRaises(ScopeDenied):
                roots.require_binding(conn,run,"review-v1")
            second = claim(reviewer["task_id"],"review-v2")
            packet, findings = submit(second)
            self.assertEqual(packet["intent"]["evaluation"]["version"],2)
            self.assertNotEqual(json.loads(first["brief"].split("\n",1)[1])["input"]["proposal_ref"],
                                packet["input"]["proposal_ref"])
            self.assertEqual([f["supported"] for f in findings["findings"]],[False,False])
            advance()
            planner = task("decision")
            deps = intent.snapshot(conn,run,"task",planner["task_id"])
            self.assertEqual(deps["objective"]["role"],"execution")
            claim(planner["task_id"],"plan-v2")
            update(2,-0.25)
            self.assertIsNone(root._claim(planner["task_id"]), "decision must wait for independent updated review")
            followup = conn.execute("SELECT * FROM tasks WHERE run_id=? AND stage='decision' AND role='evidence_reviewer'",
                                    (run,)).fetchone()
            self.assertNotEqual(followup["task_id"],reviewer["task_id"])
            fresh = claim(followup["task_id"],"review-v3")
            packet, findings = submit(fresh)
            self.assertEqual([f["supported"] for f in findings["findings"]],[True,False])
            final = claim(planner["task_id"],"plan-v3")
            packet, decision = submit(final)
            self.assertEqual(ArtifactService(conn).read_json(decision["assessment_ref"]),findings)
            advance()
            self.assertEqual(conn.execute("SELECT status FROM runs WHERE run_id=?", (run,)).fetchone()[0],"COMPLETED")
            current = science.current_evidence(conn,run)
            self.assertEqual({r["direction"] for r in current},{"supports","refutes"})
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs WHERE run_id=?",(run,)).fetchone()[0],2)
            if location := os.environ.get("GX_P0_SCIENCE_EVIDENCE"):
                rows = lambda sql: [dict(r) for r in conn.execute(sql)]
                snapshot = {"run_id":run,"status":"COMPLETED","jobs":rows("SELECT job_id,status FROM jobs"),
                    "task_dependencies":rows("SELECT * FROM p0_intent_dependencies WHERE object_type='task'"),
                    "attempts": [{"thread_id":r["thread_id"],"attempt":r["attempt"],"lease_id":r["lease_id"],
                                  "task_id":r["task_id"],"input":json.loads(r["brief"].split("\n",1)[1])["input"]}
                                 for r in conn.execute("SELECT * FROM p0_roots")],
                    "scientific_settlements":rows("SELECT * FROM science_settlements"),
                    "current_evidence":[r["evidence_id"] for r in current]}
                path = Path(location) / (self._testMethodName + ".json")
                path.parent.mkdir(exist_ok=True,parents=True)
                path.write_text(json.dumps(snapshot,indent=2) + "\n")
