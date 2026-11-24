"""Project continuation discovers the actionable branches of one pinned static workflow."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gx_core.domain import clock
from gx_core.native_release import native_state
from gx_core.orchestration import service
from gx_core.orchestration.engine import Engine, InterruptRequest, Node, NodeResult, Workflow
from gx_core.orchestration.registry import WorkflowRegistry
from gx_core.persistence import records, store
from gx_core.project_work import next_project_work


class ProjectBranchWork(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = self.stack.enter_context(tempfile.TemporaryDirectory())
        state = self.stack.enter_context(native_state(Path(root)))
        self.conn = store.open_store(state.db_path)
        self.stack.callback(self.conn.close)

    def project(self, workflow):
        registry = WorkflowRegistry([workflow])
        self.stack.enter_context(patch.object(service, "default_registry", return_value=registry))
        with store.transaction(self.conn):
            run = records.create_run(self.conn, objective="Review sample observations", project_ref="observations",
                                     actor="test", workflow_id=workflow.workflow_id, workflow_version=workflow.version,
                                     pins={"workflow_sha256": workflow.digest()})
        return run, Engine(self.conn, registry)

    def record_job(self, run, name):
        self.conn.execute("INSERT INTO jobs(job_id,run_id,capability_id,capability_version,status,idempotency_key,"
                          "submit_intent_json,input_manifest_ref,created_at) VALUES(?,?,'measure','1','CREATED',"
                          "?,'{}','manifest',?)", (name, run, name, clock.now_iso()))

    def test_later_granted_gate_is_actionable_beside_a_live_job_and_failed_branch(self):
        def observe_sample(ctx):
            if ctx.resumed:
                return NodeResult()
            return NodeResult(interrupt=InterruptRequest("external_job", "Waiting for the sample", job_refs=("sample",)))

        def release_report(ctx):
            if ctx.resumed:
                return NodeResult()
            return NodeResult(interrupt=InterruptRequest("approval", "Release the report", approval_ref=approval))

        def validate_sample(ctx):
            raise ValueError("Missing sample units")

        workflow = Workflow("sample_report", "1", [Node("observe", observe_sample), Node("release", release_report),
                                                   Node("validate", validate_sample)])
        run, engine = self.project(workflow)
        with store.transaction(self.conn):
            approval = store.request_approval(self.conn, run_id=run, action_kind="data_export", request={})
            self.record_job(run, "sample")

        engine.start(run_id=run, workflow_id=workflow.workflow_id, workflow_version=workflow.version)
        initial = engine.state(run)
        job_ref = initial["node_waits"]["observe"]["interrupt_ref"]
        report_ref = initial["node_waits"]["release"]["interrupt_ref"]
        self.assertEqual(initial["interrupt_ref"], job_ref)
        waiting = next_project_work(self.conn, "observations", run)
        self.assertEqual(waiting["status"], "WAITING_JOB", waiting)
        self.assertIn(approval, waiting["pending_approval_ids"])
        self.assertTrue(any("validate failed (ValueError): Missing sample units" in s for s in waiting["blockers"]))

        with store.transaction(self.conn):
            store.transition(self.conn, machine="approval", subject_id=approval, to_state="GRANTED", actor="operator")
        ready = next_project_work(self.conn, "observations", run)
        self.assertEqual(ready["status"], "READY", ready)
        self.assertEqual(ready["waiting_job_ids"], ["sample"])
        self.assertEqual(ready["actions"],
                         [f"{run} gx_run_resume interrupt {report_ref} checkpoint {initial['checkpoint_seq']}"])
        self.assertEqual(ready, next_project_work(self.conn, "observations", run))
        engine.resume(run_id=run, interrupt_ref=report_ref)
        self.assertEqual(next_project_work(self.conn, "observations", run)["status"], "WAITING_JOB")
        with store.transaction(self.conn):
            store.transition(self.conn, machine="job", subject_id="sample", to_state="CANCELLED", actor="operator")
        engine.resume(run_id=run, interrupt_ref=job_ref)
        blocked = next_project_work(self.conn, "observations", run)
        self.assertEqual(blocked["status"], "BLOCKED", blocked)
        self.assertEqual(blocked["actions"], [])
        self.assertNotIn("reactor_runs", blocked)

    def test_child_requested_by_a_secondary_wait_stays_in_project_progress(self):
        request = {"workflow_id": "sample_report", "idempotency_key": "read-sample"}

        def await_release(ctx):
            return NodeResult(interrupt=InterruptRequest("approval", "Release summary", approval_ref=approval))

        def await_trial(ctx):
            return NodeResult(interrupt=InterruptRequest("stage", "Wait for the trial", payload={
                "stage": "trial", "waiting_state": "WAITING_CLIENT", "wait_for": "child_runs",
                "child_requests": [request]}))

        def trial_progress(conn, run_id, round_index):
            return {"complete": False}

        def coverage(conn, run_id):
            return {"pending_child_run_ids": [child]}

        workflow = Workflow("summary", "1", [Node("release", await_release), Node("trial", await_trial, work="stage")],
                            stage_rules={"trial": trial_progress}, objective_coverage=coverage)
        run, engine = self.project(workflow)
        with store.transaction(self.conn):
            approval = store.request_approval(self.conn, run_id=run, action_kind="data_export", request={})
        engine.start(run_id=run, workflow_id=workflow.workflow_id, workflow_version=workflow.version)
        self.assertEqual(engine.state(run)["node"], "release")
        with store.transaction(self.conn):
            child = records.create_run(self.conn, objective="Read a sample", project_ref="observations", actor="test")
            key = store.tool_key("gx_run_start", request["idempotency_key"])
            store.begin_idempotent(self.conn, key, "gx_run_start", request)
            store.complete_idempotent(self.conn, key, {"run_id": child})
            self.record_job(child, "child-sample")
        work = next_project_work(self.conn, "observations", run, include_progress=True)
        self.assertEqual(work["run_ids"], [run, child])
        self.assertEqual(work["waiting_job_ids"], ["child-sample"])
        self.assertEqual(work["progress"]["jobs_by_status"], {"CREATED": 1})
