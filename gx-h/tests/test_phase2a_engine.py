"""Exercise the static Engine's real checkpoints, leases, waits and once records."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from gx_core.domain import clock
from gx_core.errors import ApprovalRequired, ConcurrentRun, DependencyUnmet, EvaluatorInvalid, IllegalTransition, PermissionDenied
from gx_core.orchestration import expansion
from gx_core.orchestration.engine import Engine, InterruptRequest, Node, NodeResult, Workflow
from gx_core.orchestration.registry import WorkflowRegistry
from gx_core.persistence import records, store


def approval_node(ctx):
    def request():
        ref = "approval-" + ctx.node
        ctx.conn.execute("INSERT INTO approvals(approval_id,run_id,action_kind,request_json,status,requested_at)"
                         " VALUES(?,?,'role_escalation','{}','PENDING',?)", (ref, ctx.run_id, clock.now_iso()))
        return ref

    ref = ctx.once("request", request)
    if ctx.approval_state(ref) == "GRANTED":
        return NodeResult(outputs={"submissions": list(ctx.submissions), "resumed": ctx.resumed, "round": ctx.round})
    return NodeResult(interrupt=InterruptRequest(kind="approval", reason="Operator decision", approval_ref=ref))


def receipt(ctx):
    assert all(dep in ctx.outputs for dep in ctx.inputs.get("requires", {}).get(ctx.node, ()))
    return NodeResult(outputs={"submissions": list(ctx.submissions), "resumed": ctx.resumed, "round": ctx.round})


def closure(conn, run):
    return True, []


class StaticBranches(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.enterContext(closing(store.open_store(self.directory / "test.db")))

    def workflow(self, nodes, *, stage_rules=None):
        self.flow = Workflow("static", "1", nodes, stage_rules=stage_rules, completion_evidence=closure)
        self.registry = WorkflowRegistry([self.flow])
        self.engine = Engine(self.db, self.registry)
        self.run = records.create_run(self.db, objective="independent branches", project_ref="static",
                                      actor="test", workflow_id="static", workflow_version="1")

    def start(self):
        return self.engine.start(run_id=self.run, workflow_id="static", workflow_version="1")

    def grant(self, node):
        self.db.execute("UPDATE approvals SET status='GRANTED' WHERE approval_id=?", ("approval-" + node,))

    def test_two_waits_exact_resume_and_cold_handoff(self):
        self.workflow([Node("A", approval_node), Node("B", approval_node), Node("C", receipt),
                       Node("A2", receipt, deps=("A",)), Node("B2", receipt, deps=("B",)),
                       Node("join", receipt, deps=("A2", "B2", "C"))])
        result = self.start()
        self.assertEqual(result["node"], "A")
        state = self.engine.state(self.run)
        self.assertEqual(state["node_states"], {"A": "WAITING_HUMAN", "B": "WAITING_HUMAN", "C": "COMPLETED",
                                                "A2": "BLOCKED", "B2": "BLOCKED", "join": "BLOCKED"})
        a, b = state["node_waits"]["A"], state["node_waits"]["B"]
        self.assertFalse(self.engine.idle(self.run))
        self.assertEqual(b["references"], ["approval-B"])
        self.assertTrue(b["since"])
        with self.assertRaises(ApprovalRequired):
            self.engine.resume(run_id=self.run, interrupt_ref=b["interrupt_ref"])
        self.grant("B")
        with store.transaction(self.db):
            begun = self.engine.begin_resume(self.db, run_id=self.run, interrupt_ref=b["interrupt_ref"],
                                             submission_refs=["B-submission"])
        with self.assertRaises(ConcurrentRun):
            self.engine.resume(run_id=self.run, interrupt_ref=a["interrupt_ref"])
        # Simulate process loss after clearance but before executing any node.
        self.engine._release_advance(self.run, self.flow, begun["state"]["advance"]["id"])
        with closing(store.open_store(self.directory / "test.db")) as conn:
            cold = Engine(conn, self.registry)
            self.assertTrue(cold.idle(self.run, retry_failed=False))
            answer = cold.advance(run_id=self.run, retry_failed=False)
            self.assertEqual(answer["interrupt_ref"], a["interrupt_ref"])
            self.assertEqual(cold.waits(self.run), {"A": a})
            found = cold.checkpointer.latest(conn, self.run).state
            self.assertEqual(found["outputs"]["B"], {"submissions": ["B-submission"], "resumed": True, "round": 0})
            self.assertFalse(found["outputs"]["B2"]["resumed"])
            self.assertEqual(found["outputs"]["B2"]["submissions"], [])
        with self.assertRaises(IllegalTransition):
            self.engine.resume(run_id=self.run, interrupt_ref=b["interrupt_ref"])
        self.grant("A")
        self.assertEqual(self.engine.resume(run_id=self.run, interrupt_ref=a["interrupt_ref"])["status"], "COMPLETED")
        self.assertEqual(self.db.execute("SELECT count(*) FROM approvals").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE kind='workflow.node_completed'").fetchone()[0], 6)

    def test_legacy_single_wait_with_unstarted_sibling(self):
        self.workflow([Node("A", approval_node), Node("B", receipt), Node("A2", receipt, deps=("A",))])
        self.start()
        latest = self.engine.checkpointer.latest(self.db, self.run)
        legacy = dict(latest.state)
        legacy.pop("node_waits")
        legacy["completed"], legacy["outputs"] = [], {}
        with store.transaction(self.db):
            self.engine.checkpointer.save(self.db, run_id=self.run, workflow_id="static", workflow_version="1",
                                          node="A", state=legacy, interrupt_ref=latest.interrupt_ref)
        unknown = Engine(self.db, WorkflowRegistry())
        self.assertEqual(unknown.state(self.run)["node_states"], {})
        self.assertFalse(unknown.idle(self.run, retry_failed=False))
        self.assertTrue(self.engine.idle(self.run, retry_failed=False))
        self.assertEqual(self.engine.advance(run_id=self.run, retry_failed=False)["interrupt_ref"], latest.interrupt_ref)
        self.assertEqual(self.engine.state(self.run)["completed_nodes"], ["B"])
        self.assertFalse(self.engine.idle(self.run, retry_failed=False))

    def test_failed_branch_rolls_back_and_manual_retry_reuses_other_branches(self):
        outcomes = [False, True]

        def attempt(ctx):
            self.assertNotIn("uncommitted", ctx.state)
            if not outcomes.pop(0):
                ctx.state["uncommitted"] = ["must roll back"]
                ctx.once("uncommitted", lambda: store.append_event(
                    ctx.conn, kind="fixture.uncommitted", actor="fixture", run_id=ctx.run_id,
                    subject_kind="workflow", subject_id=ctx.workflow_id))
                raise ValueError("invalid measurement")
            return receipt(ctx)

        self.workflow([Node("bad", attempt), Node("A", approval_node), Node("C", receipt),
                       Node("bad_child", receipt, deps=("bad",))])
        self.start()
        before = self.engine.state(self.run)
        self.assertEqual(before["node_states"], {"bad": "FAILED", "A": "WAITING_HUMAN",
                                                "C": "COMPLETED", "bad_child": "BLOCKED"})
        self.assertEqual(before["failed_nodes"]["bad"]["message"], "invalid measurement")
        self.assertIsNone(self.db.execute("SELECT 1 FROM events WHERE kind='fixture.uncommitted'").fetchone())
        self.assertIsNone(self.db.execute("SELECT 1 FROM idempotency WHERE scope='workflow_node:bad'").fetchone())
        with closing(store.open_store(self.directory / "test.db")) as conn:
            cold = Engine(conn, self.registry)
            self.assertEqual(cold.state(self.run), before)
            self.assertFalse(cold.idle(self.run, retry_failed=False))
            self.assertTrue(cold.idle(self.run))
            cold.advance(run_id=self.run)
        after = self.engine.state(self.run)
        self.assertEqual(after["node_waits"], before["node_waits"])
        self.assertEqual(after["completed_nodes"], ["C", "bad", "bad_child"])

    def test_automatic_advance_reports_the_failed_node_and_explicit_retry_raises(self):
        def invalid(ctx):
            raise EvaluatorInvalid("measurement has no units")

        self.workflow([Node("gate", approval_node), Node("measurement", invalid, deps=("gate",)),
                       Node("independent", receipt, deps=("gate",)), Node("use_measurement", receipt, deps=("measurement",))])
        ref = self.start()["interrupt_ref"]
        self.grant("gate")
        with store.transaction(self.db):
            begun = self.engine.begin_resume(self.db, run_id=self.run, interrupt_ref=ref)
        result = self.engine.advance_resumed(begun, report_branch_failures=True)
        self.assertIn("measurement failed (EVALUATOR_INVALID): measurement has no units", result["warnings"])
        self.assertEqual(result["status"], "ACTIVE")
        with closing(store.open_store(self.directory / "test.db")) as conn:
            cold = Engine(conn, self.registry)
            self.assertFalse(cold.idle(self.run, retry_failed=False))
            state = cold.state(self.run)
            self.assertEqual(state["node_states"], {"gate": "COMPLETED", "measurement": "FAILED",
                                                    "independent": "COMPLETED", "use_measurement": "BLOCKED"})
            self.assertEqual(state["failed_nodes"]["measurement"]["code"], "EVALUATOR_INVALID")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='workflow.node_failed'").fetchone()[0], 1)
            with self.assertRaisesRegex(EvaluatorInvalid, "measurement has no units"):
                cold.advance(run_id=self.run)
            self.assertEqual(cold.state(self.run)["completed_nodes"], state["completed_nodes"])

    def test_global_refusal_does_not_admit_a_sibling(self):
        def refused(ctx):
            raise PermissionDenied("owner rejected operation")

        self.workflow([Node("restricted", refused), Node("C", receipt)])
        with self.assertRaises(PermissionDenied):
            self.start()
        self.assertEqual(self.engine.state(self.run)["completed_nodes"], [])
        self.assertEqual(self.engine.state(self.run)["failed_nodes"], {})

    def test_rewind_retains_prior_wait_round_and_once_key(self):
        def rule(conn, run, round_index):
            row = conn.execute("SELECT status FROM approvals WHERE approval_id='approval-A'").fetchone()
            return {"complete": bool(row and row[0] == "GRANTED")}

        def stage(ctx):
            result = approval_node(ctx)
            if result.interrupt:
                return NodeResult(interrupt=InterruptRequest(kind="stage", reason="Await decision",
                    approval_ref=result.interrupt.approval_ref, payload={"stage": ctx.node, "waiting_state": "WAITING_APPROVAL"}))
            self.assertEqual(expansion.open_stage_round(ctx.conn, ctx.run_id, ctx.node), ctx.round)
            return result

        def rewind(ctx):
            return NodeResult(reenter="B", reason="second investigation") if ctx.round == 0 else receipt(ctx)

        self.workflow([Node("A", stage, work="stage"), Node("B", receipt), Node("R", rewind, deps=("B",))],
                      stage_rules={"A": rule})
        self.start()
        a = self.engine.waits(self.run)["A"]
        self.assertEqual((a["round"], expansion.open_stage_round(self.db, self.run, "A")), (0, 0))
        self.grant("A")
        self.assertEqual(self.engine.resume(run_id=self.run, interrupt_ref=a["interrupt_ref"])["status"], "COMPLETED")
        state = self.engine.checkpointer.latest(self.db, self.run).state
        self.assertEqual((state["round"], state["outputs"]["A"]["round"], state["outputs"]["B"]["round"]), (1, 0, 1))
        self.assertEqual(self.db.execute("SELECT count(*) FROM approvals").fetchone()[0], 1)

    def test_rewind_with_unresolved_sibling_and_dependent_jobs_keeps_both_operations(self):
        def job(ctx):
            def submit():
                ref = f"job-{ctx.node}-{ctx.round}"
                ctx.conn.execute("INSERT INTO jobs(job_id,run_id,capability_id,capability_version,status,"
                                 "idempotency_key,submit_intent_json,input_manifest_ref,created_at) "
                                 "VALUES(?,?,'fixture','1','OUTCOME_UNKNOWN',?,'{}','in',?)",
                                 (ref, ctx.run_id, ref, clock.now_iso()))
                return ref

            ref = ctx.once("job", submit)
            if ctx.conn.execute("SELECT status FROM jobs WHERE job_id=?", (ref,)).fetchone()[0] == "SUCCEEDED":
                return receipt(ctx)
            return NodeResult(interrupt=InterruptRequest("external_job", "reconcile original submit", job_refs=(ref,)))

        def rewind(ctx):
            return NodeResult(reenter="B", reason="retest with a revised input") if ctx.round == 0 else receipt(ctx)

        self.workflow([Node("B", receipt), Node("unrelated", job), Node("other_result", receipt),
                       Node("affected", job, deps=("B",)), Node("R", rewind, deps=("B",))])
        self.start()
        old = self.engine.waits(self.run)
        self.assertEqual(set(old), {"unrelated", "affected"})
        for name, wait in old.items():
            self.assertEqual(wait["references"], [f"job-{name}-0"])
            self.assertEqual(wait["status"], "WAITING_JOB")
        self.assertNotIn("reenter_round", old["unrelated"])
        self.assertEqual(old["affected"]["reenter_round"], 1)
        with self.assertRaises(DependencyUnmet):
            self.engine.resume(run_id=self.run, interrupt_ref=old["affected"]["interrupt_ref"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 2)
        self.db.execute("UPDATE jobs SET status='SUCCEEDED' WHERE job_id='job-unrelated-0'")
        self.engine.resume(run_id=self.run, interrupt_ref=old["unrelated"]["interrupt_ref"])
        self.assertEqual(self.engine.waits(self.run)["affected"], old["affected"])
        self.db.execute("UPDATE jobs SET status='SUCCEEDED' WHERE job_id='job-affected-0'")
        self.engine.resume(run_id=self.run, interrupt_ref=old["affected"]["interrupt_ref"])
        new = self.engine.waits(self.run)["affected"]
        self.assertEqual(new["references"], ["job-affected-1"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE kind='workflow.node_completed' AND "
                                         "json_extract(payload_json,'$.node')='other_result'").fetchone()[0], 1)

    def test_incomplete_stages_do_not_wake_each_other_on_control_events(self):
        def stage(ctx):
            return NodeResult(interrupt=InterruptRequest("stage", "wait for stored feedback",
                payload={"stage": ctx.node, "waiting_state": "WAITING_CLIENT"}))

        def rule(conn, run, round_index):
            return {"complete": False, "quiescent": False}

        self.workflow([Node("left", stage, work="stage"), Node("right", stage, work="stage")],
                      stage_rules={"left": rule, "right": rule})
        self.start()
        waits = self.engine.waits(self.run)
        self.assertTrue(all(not self.engine.standing(self.run, interrupt_ref=w["interrupt_ref"]).resumable
                            for w in waits.values()))
        store.append_event(self.db, kind="team.feedback", actor="fixture", run_id=self.run)
        for w in waits.values():
            self.assertTrue(self.engine.standing(self.run, interrupt_ref=w["interrupt_ref"]).resumable)
            self.engine.resume(run_id=self.run, interrupt_ref=w["interrupt_ref"])
        self.assertTrue(all(not self.engine.standing(self.run, interrupt_ref=w["interrupt_ref"]).resumable
                            for w in self.engine.waits(self.run).values()))

    def test_quiescent_sibling_waits_close_only_after_other_ready_work_executes(self):
        def missing(ctx):
            return NodeResult(interrupt=InterruptRequest("client_work", "missing product", tasks=("missing-" + ctx.node,)))

        self.workflow([Node("left", missing), Node("right", missing), Node("C", receipt)])
        self.assertEqual(self.start()["status"], "STOPPED")
        self.assertEqual(self.engine.state(self.run)["completed_nodes"], ["C"])
        self.assertFalse(self.engine.waits(self.run))
