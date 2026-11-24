"""Offline branch-resume checks with a pinned test workflow and a scratch GX store."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import shutil
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from gx_core import config
from gx_core.artifacts import ArtifactService
from gx_core.context import Ctx, Principal
from gx_core.errors import ApprovalRequired, IllegalTransition, ModelPolicyViolation, SchemaInvalid
from gx_core.governance import approvals
from gx_core.hostobs.observation import write_observation
from gx_core.native_release import native_state
from gx_core.orchestration import reactor, seams, service
from gx_core.orchestration.engine import InterruptRequest, Node, NodeResult, Workflow
from gx_core.orchestration.hooks import WorkflowHooks
from gx_core.orchestration.registry import WorkflowRegistry
from gx_core.persistence import store
from gx_core.registry import Subject
from gx_core.teams.service import TeamService


def planner(ctx):
    task = ctx.once("planner", lambda: ctx.teams.create_task(run_id=ctx.run_id, kind="propose_paths",
        role="planner", bundle_kind="path_bundle", acceptance={}, input={})["task_id"])
    return NodeResult(interrupt=InterruptRequest("client_work", "Waiting for the planner", tasks=(task,)))


def review_complete(conn, run_id, round_index):
    row = conn.execute("SELECT status FROM tasks WHERE run_id=? AND stage='review'", (run_id,)).fetchone()
    return {"complete": row is not None and row["status"] == "ACCEPTED"}


def validate_product(conn, task, bundle):
    ref, = task["submission_artifact_refs"]
    if ArtifactService(conn).read_json(ref) != {"finding": "two rows"}:
        raise SchemaInvalid("unexpected fixture finding")


def review(ctx):
    task = ctx.once("review", lambda: ctx.teams.create_task(run_id=ctx.run_id, stage=ctx.node,
        kind="propose_paths", role="planner", bundle_kind=None, acceptance={"validate_workflow_product": True},
        required_artifacts=["review_finding"], input={"output_kind": "review_finding"})["task_id"])
    if ctx.resumed and review_complete(ctx.conn, ctx.run_id, ctx.round)["complete"]:
        return NodeResult(outputs={"task": task, "submissions": list(ctx.submissions)})
    return NodeResult(interrupt=InterruptRequest("stage", "Waiting for the review", tasks=(task,),
        payload={"stage": ctx.node, "waiting_state": "WAITING_CLIENT"}))


class BranchResumePolicy(unittest.TestCase):
    def setUp(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.enterContext(native_state(tmp / "state", env={}))
        self.conn = self.enterContext(closing(store.open_store(self.state.db_path)))
        registry = WorkflowRegistry()
        flow = registry.register(Workflow("resume_fixture", "1", [
            Node("planner", planner, work="intelligent"), Node("review", review, work="stage")],
            document={"nodes": [{"name": "review", "work": "stage", "artifact_task_kind": "propose_paths"}]},
            stage_rules={"review": review_complete}, task_product_validator=validate_product),
            hooks=WorkflowHooks(auto_advance=True))
        private = tmp / "config"
        shutil.copytree(config.CONFIG_DIR, private)
        pins = private / "shipped.json"
        content = json.loads(pins.read_text())
        content["workflows"]["resume_fixture@1"] = flow.digest()
        pins.write_text(json.dumps(content))
        self.enterContext(patch.object(config, "CONFIG_DIR", private))
        service.set_registry(registry)
        self.addCleanup(service.set_registry, None)

        self.principal = Principal(subject="fixture-coordinator", scope="operator")
        self.ctx = Ctx(self.conn, self.principal)
        started = service.run_start(self.ctx, {"objective": "Collect a synthetic row count",
            "project_ref": "resume-test", "budget_profile": "cpu_only", "workflow": {"id": flow.workflow_id,
            "version": flow.version}, "idempotency_key": "fixture-start"})
        self.run_id = started["run_id"]
        self.lease = started["coordinator_lease_ref"]
        self.original = self.checkpoint()["node_waits"]
        self.first = self.original["planner"]["interrupt_ref"]
        self.second = self.original["review"]["interrupt_ref"]
        self.assertEqual(started["interrupt_ref"], self.first)
        self.assertNotEqual(self.first, self.second)
        self.task, = self.original["review"]["interrupt"]["tasks"]
        worker = "fixture-reviewer"
        # Real task lease and submission, with synthetic native callback identifiers.
        self.worker_lease = self.lease_task(self.task, worker)
        brief = self.conn.execute("SELECT brief_sha256 FROM tasks WHERE task_id=?", (self.task,)).fetchone()[0]
        self.submission = TeamService(self.conn).submit_workflow_product(run_id=self.run_id, task_id=self.task,
            lease_ref=self.worker_lease, brief_sha256=brief, product={"finding": "two rows"},
            callback={"thread_id": worker, "turn_id": "fixture-turn", "call_id": "fixture-call"})
        self.assertEqual(self.submission["task_status"], "ACCEPTED", self.submission)

    def lease_task(self, task, worker):
        with store.transaction(self.conn) as tx:
            lease = store.issue_lease(tx, task_id=task, owner=worker, principal=worker,
                worker_role="planner", seconds=3600, attempt=1)["lease_id"]
            store.transition(tx, machine="task", subject_id=task, to_state="CLAIMED", actor=worker,
                extra_updates={"owner": worker, "lease_id": lease, "attempt": 1})
        return lease

    def checkpoint(self):
        with closing(store.open_store(self.state.db_path)) as reader:
            return store.loads(store.latest_checkpoint(reader, self.run_id), "state_json")

    def assert_only_second_consumed(self, refs):
        state = self.checkpoint()
        self.assertEqual(state["node_waits"], {"planner": self.original["planner"]})
        self.assertEqual(state["outputs"]["review"], {"task": self.task, "submissions": refs})
        with closing(store.open_store(self.state.db_path)) as reader:
            events = reader.execute("SELECT json_extract(payload_json,'$.node') FROM events "
                "WHERE run_id=? AND kind='workflow.node_completed' ORDER BY event_id", (self.run_id,))
            self.assertEqual([event[0] for event in events], ["review"])

    def record_mismatch(self):
        # Feed a late host observation to the policy reader after GX accepted the product.
        with store.transaction(self.conn) as tx:
            return write_observation(tx, task_id=self.task, lease_id=self.worker_lease, host="codex",
                state="observed_started", source="host_protocol", evidence_level="EXECUTION_OBSERVED",
                observed_model="fixture-mismatch", policy_verdict="MISMATCH")

    def grant(self, obs):
        row = self.conn.execute("SELECT submission_id,content_sha256 FROM submissions WHERE submission_id=?",
                                (self.submission["submission_id"],)).fetchone()
        subject = Subject(kind="submission", id=row["submission_id"], sha256=row["content_sha256"])
        requested = approvals.request(self.ctx, run_id=self.run_id, action_kind="model_switch", subject=subject,
            request={"worker_observation_id": obs})
        # Synthetic operator decision bound to this exact accepted product.
        human = Principal(subject="fixture-operator", scope="operator", source=approvals.INTERACTIVE_SOURCE)
        result = approvals.decide(Ctx(self.conn, human), approval_id=requested["approval_id"], decision="GRANTED",
            expected_subject_sha256=subject.sha256, note="Accept the synthetic settings change.")
        self.assertEqual(result["status"], "GRANTED")

    def args(self):
        return {"run_id": self.run_id, "interrupt_ref": self.second, "coordinator_lease_ref": self.lease,
            "submission_refs": [self.submission["artifact_ref"]], "idempotency_key": "resume-second"}

    def test_service_uses_selected_submission_then_replays_across_connections(self):
        obs = self.record_mismatch()
        request = self.args()
        with self.assertRaises(ModelPolicyViolation) as caught:
            service.run_resume(self.ctx, request)
        self.assertEqual(caught.exception.details["submission_id"], self.submission["submission_id"])
        self.assertEqual(self.checkpoint()["node_waits"], self.original)
        self.assertIsNone(store.stored_response(self.conn, store.tool_key("gx_run_resume", request["idempotency_key"])))
        self.grant(obs)
        answer = service.run_resume(self.ctx, request)
        self.assertEqual(answer["interrupt_ref"], self.first)
        with closing(store.open_store(self.state.db_path)) as reader:
            ctx = Ctx(reader, self.principal)
            replay = service.run_resume(ctx, request)
            self.assertEqual(replay["revision"], answer["revision"])
            with self.assertRaises(IllegalTransition):
                service.run_resume(ctx, {**request, "idempotency_key": "stale-interrupt"})
        self.assert_only_second_consumed(request["submission_refs"])

    def test_reactor_checks_selected_submission_before_consuming_ready_stage(self):
        obs = self.record_mismatch()
        self.assertIsNone(reactor.drive_science(self.ctx, self.run_id))
        self.assertEqual(self.checkpoint()["node_waits"], self.original)
        self.grant(obs)
        self.assertEqual(reactor.drive_science(self.ctx, self.run_id)["interrupt_ref"], self.first)
        self.assertIsNone(reactor.drive_science(self.ctx, self.run_id))
        self.assert_only_second_consumed([])

    def test_two_reactors_observing_same_ready_stage_consume_it_once(self):
        rendezvous = Barrier(2, timeout=10)
        require_pins = service.require_pins

        def pins_after_both_observed(run, workflow):
            checked = require_pins(run, workflow)
            # Both connections observed the ready stage before either acquires the writer lock.
            rendezvous.wait()
            return checked

        def drive():
            with closing(store.open_store(self.state.db_path)) as conn:
                return reactor.drive_science(Ctx(conn, self.principal), self.run_id)

        with patch.object(service, "require_pins", side_effect=pins_after_both_observed):
            with ThreadPoolExecutor(max_workers=2) as pool:
                attempts = [pool.submit(drive) for _ in range(2)]
                results = [attempt.result(timeout=15) for attempt in attempts]
        self.assertEqual([result["interrupt_ref"] for result in results if result is not None], [self.first])
        self.assert_only_second_consumed([])

    def test_both_entry_points_pass_selected_interrupt_to_held_submission_check(self):
        task, = self.original["planner"]["interrupt"]["tasks"]
        worker = "fixture-planner"
        lease = self.lease_task(task, worker)
        product = ArtifactService(self.conn).put(run_id=self.run_id, task_id=task, kind="path_bundle",
            media_type="application/json", content='{"paths":[]}', origin="HOST_PUT", put_by=worker)
        team = TeamService(self.conn, ctx=Ctx(self.conn, Principal(subject=worker)))
        # Inject only the model observation for this hand-in. GX persists the held submission
        # and pending approval. Both resume paths run the real held-submission predicate.
        with patch.object(seams, "policy_verdict", return_value={"action": "hold", "policy_verdict": "MISMATCH"}):
            held = team.submit(task_id=task, lease_ref=lease, artifact_refs=[product.artifact_ref],
                               idempotency_key="held-planner-product")
        self.assertEqual(held["task_status"], "BLOCKED")
        approval = held["validation"]["approval_id"]
        selected = self.original["review"]["interrupt"]
        with patch.object(service, "_require_no_held_submission", wraps=service._require_no_held_submission) as check:
            with self.assertRaises(ApprovalRequired) as caught:
                service.run_resume(self.ctx, self.args())
            self.assertEqual(caught.exception.details["pending_holds"], [{"task_id": task, "approval_id": approval}])
            self.assertIsNone(reactor.drive_science(self.ctx, self.run_id))
            self.assertEqual([call.kwargs["pending"] for call in check.call_args_list], [selected, selected])
        self.assertEqual(self.checkpoint()["node_waits"], self.original)
        with closing(store.open_store(self.state.db_path)) as reader:
            row = reader.execute("SELECT s.result,a.status FROM submissions s JOIN approvals a "
                "ON a.subject_id=s.submission_id AND a.subject_sha256=s.content_sha256 "
                "WHERE a.approval_id=?", (approval,)).fetchone()
            self.assertEqual(tuple(row), ("HELD", "PENDING"))
