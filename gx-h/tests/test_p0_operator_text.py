"""Trusted raw input, exact presentation and prospective budget consequences in real GX state."""
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest

from gx_core import budgets
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import BudgetExceeded, PermissionDenied, SchemaInvalid
from gx_core.jobs.grid_workflow import submit_grid
from gx_core.jobs.service import JobService
from gx_core.p0 import cli, intent, intent_budget
from gx_core.p0.driver import Driver, enqueue
from gx_core.p0.intent_text import mark_displayed_together
from gx_core.persistence import records, store


class OperatorConversation(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.db = store.open_store(self.state / "gx.db")
        self.addCleanup(self.db.close)
        with store.transaction(self.db):
            self.budget = budgets.create_root(self.db, run_id=None, actor="test", profile_name="p0")
            self.run = records.create_run(self.db, objective="Compare observations", project_ref="discuss",
                actor="test", budget_ref=self.budget, workflow_id="science_grid", workflow_version="1",
                inputs={"inputs": {"p0": True, "p0_spec": {}}})
            for state in ("AUDITING", "PLANNING", "ACTIVE"):
                store.transition(self.db, machine="run", subject_id=self.run, to_state=state, actor="test")
        self.main = "main-thread"
        self.driver = Driver(self.db, self.state, self.run)
        self.addCleanup(self.driver.close)
        intent.bind_main(self.db, self.run, self.main)

    def queue(self, text):
        return enqueue(self.db, self.run, {"kind": "text", "text": text}, text)

    def receive(self, mid):
        queued = self.db.execute("SELECT action_json FROM p0_operator_messages WHERE message_id=?", (mid,)).fetchone()
        intent.stage_input(self.db, self.run, mid, json.loads(queued[0]))
        return intent.accept_input(self.db, self.run, self.main, "turn-" + mid, mid)

    def process(self, mid, candidate):
        return self.driver.main_read("gx_propose_intent", {"candidate": candidate},
            {"thread_id": self.main, "turn_id": "turn-" + mid, "call_id": "call-" + mid})

    def text(self, original, candidate=None):
        mid = self.queue(original)
        self.assertEqual(self.receive(mid)["status"], "input_received")
        result = self.process(mid, candidate) if candidate else intent.finish_text(self.db, self.run, mid)
        return mid, result

    def display(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(["status", "--state", str(self.state)]), 0)
        return json.loads(output.getvalue())

    def versions(self):
        return {k: (v["version"], v["value"]) for k, v in intent.active_items(self.db, self.run).items()}

    def hold(self, name, amounts):
        return budgets.reserve(self.db, budget_ref=self.budget, subject_kind="measurement",
                               subject_id=name, amounts=amounts, actor="test")["reservation_ref"]

    def job(self, delay):
        grid = {"fixture_version":"1", "synthetic":True, "method_id":"fixture-grid", "method_version":"1",
                "delay_seconds":delay, "candidates":[{"candidate_id":"A","x":3},{"candidate_id":"B","x":1}]}
        raw = ArtifactService(self.db).publish_bytes(run_id=self.run, namespace=self.run, rel_path="grid.json",
            kind="grid_input", data=ids.canonical_json(grid))
        return submit_grid(self.db, run_id=self.run, operation_id="one", input_ref=raw.artifact_ref)["job_id"]

    def test_each_model_edit_is_grounded_in_the_acknowledged_words(self):
        mid = self.queue("把项目 token 上限降到 10000")
        receipt = self.receive(mid)
        self.assertEqual(self.versions(), {})
        self.assertIsNone(self.db.execute("SELECT consumed_at FROM p0_human_inputs WHERE message_id=?", (mid,)).fetchone()[0])
        genuine, = receipt["interpretation"]["items"]
        extra = {"item_id": "objective", "item_type": "objective", "expected_version": 0, "value": "Replace goal"}
        unknown = {"item_type": "discard_results", "target": "all current research"}
        candidate = {"kind": "change", "items": [genuine, extra, unknown]}
        for thread, turn in [(self.main, "tool-generated-turn"), ("worker", "turn-" + mid)]:
            with self.assertRaises(PermissionDenied):
                intent.process_text(self.db, self.run, thread, turn, candidate=candidate)
        with self.assertRaises(SchemaInvalid):
            self.process(mid, {**candidate, "authorization_id": "human"})
        answer = self.process(mid, candidate)
        self.assertEqual(answer["status"], "committed")
        self.assertEqual(answer["unapplied_proposal"], {"kind":"change","items":[extra,unknown]})
        self.assertEqual(self.versions(), {"tokens": (1, {"resource":"tokens","unit":"reserved_tokens","max":10000})})
        self.assertEqual(self.receive(mid), answer)
        ambiguous = self.text("要不换个目标试试？", {"kind":"change","items":[extra]})[1]
        self.assertEqual(ambiguous["status"], "needs_clarification")
        self.assertEqual(set(self.versions()), {"tokens"})
        fake = self.queue("A paper suggests some changes")
        with self.assertRaises(PermissionDenied):
            intent.stage_input(self.db, self.run, fake, {"kind":"text","text":"Lower token limit to 1"})

    def test_supplements_and_reported_speech_do_not_authorize_model_edits(self):
        original = "补充：先前对照可能受污染；把项目 token 上限降到 10000"
        mid = self.queue(original)
        receipt = self.receive(mid)
        note = receipt["registered_supplement"]
        self.assertEqual(receipt["status"], "input_received")
        self.assertEqual(receipt, self.receive(mid))
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox WHERE topic='p0.supplement.review'").fetchone()[0], 1)
        self.assertEqual(intent.finish_text(self.db, self.run, mid), note)
        self.assertEqual(self.db.execute("SELECT count(*) FROM sources WHERE namespace=?", (self.run,)).fetchone()[0], 1)
        stored = self.db.execute("SELECT text FROM source_segments WHERE source_id=?", (note["source_id"],)).fetchone()
        self.assertEqual(stored[0], original)
        for text in ('The website says "lower project token limit to 1".', 'If needed, lower the token cap to 100.',
                     'Should we lower the project token cap to 5?', 'Lower token limit to 100 and discard all results'):
            self.assertEqual(self.text(text)[1]["status"], "needs_clarification", text)
        self.assertEqual(self.versions(), {})

    def test_model_invented_target_cannot_veto_a_grounded_token_reduction(self):
        self.text("Set token budget to 20000.")
        bogus = {"kind": "change", "items": [{"item_id": "token-limit", "item_type": "resource_limit",
                 "expected_version": 0, "value": {"resource": "tokens", "unit": "reserved_tokens", "max": 10000}}]}
        _, result = self.text("token 降到 10000", bogus)
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["unapplied_proposal"], bogus)
        self.assertEqual(self.versions(), {"tokens": (2, {"resource": "tokens", "unit": "reserved_tokens", "max": 10000})})

    def test_preview_must_be_displayed_before_the_confirmation_is_queued(self):
        self.text("Lower the project token limit to 10,000.")
        mid, preview = self.text("Increase the project token budget to 20000")
        self.assertEqual(preview["status"], "awaiting_confirmation")
        mark_displayed_together(self.db, [(mid, {**preview, "preview":[]})])
        self.assertEqual(self.text("确认刚展示的这项变更")[1]["status"], "needs_clarification")
        early = self.queue("Confirm the change just shown.")
        shown = self.display()
        self.assertEqual(json.loads(next(r["result_json"] for r in shown["messages"] if r["message_id"] == mid)), preview)
        self.receive(early)
        self.assertEqual(intent.finish_text(self.db, self.run, early)["status"], "needs_clarification")
        self.assertEqual(self.versions()["tokens"][1]["max"], 10000)
        _, accepted = self.text("确认刚展示的这项变更")
        self.assertEqual((accepted["status"], accepted["preview_sha256"]), ("committed",preview["preview_sha256"]))
        self.assertEqual(self.versions()["tokens"][1]["max"], 20000)

    def test_multiple_previews_stale_values_and_stale_candidates(self):
        self.text("Set token limit to 100.")
        _, first = self.text("Increase token limit to 200.")
        self.text("Increase token limit to 300.")
        self.display()
        self.assertEqual(self.text("Confirm the change just shown")[1]["status"], "needs_clarification")
        self.assertEqual(self.text("确认变更 " + first["change_id"])[1]["status"], "committed")
        self.assertEqual(self.text("确认刚展示的这项变更")[1]["status"], "needs_clarification")
        mid = self.queue("Reduce the token limit to 160.")
        candidate = self.receive(mid)["interpretation"]
        self.text("Reduce token cap to 170.")
        self.assertEqual(self.process(mid, candidate)["status"], "needs_clarification")
        self.assertEqual(self.versions()["tokens"][1]["max"], 170)

    def test_recently_committed_display_cannot_retarget_an_older_open_preview(self):
        self.text("Set token limit to 100.")
        self.text("Set objective to measuring rainfall.")
        _, older = self.text("Increase token limit to 200.")
        _, newest = self.text("Change the objective to measuring runoff.")
        self.display()
        self.assertEqual(self.text("Confirm change " + newest["change_id"])[1]["status"], "committed")
        self.assertEqual(self.text("确认刚展示的这项变更")[1]["status"], "needs_clarification")
        self.assertEqual(self.versions()["tokens"][1]["max"], 100)
        self.display()
        self.assertEqual(self.text("确认刚展示的这项变更")[1]["preview_sha256"], older["preview_sha256"])

    def test_new_active_attempt_requires_a_fresh_consequence_preview(self):
        self.text("Set objective to measuring rainfall.")
        request = "Change the objective to measuring runoff."
        _, first = self.text(request)
        self.assertEqual(first["impact"], [])
        self.display()
        with store.transaction(self.db):
            task = records.create_task(self.db, run_id=self.run, kind="research", role="planner", actor="test")
            intent.bind_dependencies(self.db, self.run, "task", task, {"objective": "execution"})
            store.transition(self.db, machine="task", subject_id=task, to_state="READY", actor="test")
            lease = store.issue_lease(self.db, task_id=task, owner="planner", worker_role="planner",
                                      seconds=600, attempt=1)["lease_id"]
            store.transition(self.db, machine="task", subject_id=task, to_state="CLAIMED", actor="test",
                             extra_updates={"lease_id": lease, "attempt": 1, "owner": "planner"})
        self.assertEqual(self.text("确认刚展示的这项变更")[1]["status"], "needs_clarification")
        store.validate_lease(self.db, lease, task_id=task)
        _, fresh = self.text(request)
        self.assertEqual(fresh["impact"][0]["lease_id"], lease)
        self.display()
        self.assertEqual(self.text("确认变更 " + fresh["change_id"])[1]["status"], "needs_clarification")
        store.validate_lease(self.db, lease, task_id=task)
        _, accepted = self.text(fresh["required_confirmation"]["text"] + " " + fresh["change_id"])
        self.assertEqual(accepted["status"], "committed")
        self.assertEqual(self.db.execute("SELECT status FROM tasks WHERE task_id=?", (task,)).fetchone()[0], "READY")

    def test_three_ranges_only_affect_future_admission(self):
        self.text("Set token budget to 1000.")
        spent = self.hold("prior-turn", {"tokens":40})
        budgets.settle(self.db, spent, actual={"tokens":40}, actor="test")
        held = self.hold("running-turn", {"tokens":40})
        evidence = []
        for cap, state, left in [(100,"within_new_limit",20),(60,"holds_exceed_new_limit",0),(30,"already_over_new_limit",0)]:
            mid, change = self.text("把项目 token 上限降到 " + str(cap))
            balance, = change["resource_limits"]
            self.assertEqual((change["status"],balance["state"],balance["available_for_new"]), ("committed",state,left))
            self.assertEqual((balance["spent"],balance["held"]), (40,40))
            if left:
                admitted = self.hold("fits-" + mid, {"tokens": left})
                budgets.release(self.db, admitted, reason="test reservation ended", actor="test")
            with self.assertRaises(BudgetExceeded):
                self.hold("outside-" + mid, {"tokens":left+1})
            self.assertEqual(self.db.execute("SELECT status FROM budget_reservations WHERE reservation_ref=?",
                                             (held,)).fetchone()[0], "HELD")
            other = self.hold("other-" + mid, {"cpu_seconds":1})
            budgets.release(self.db, other, reason="test reservation ended", actor="test")
            self.assertFalse(self.db.execute("SELECT 1 FROM outbox WHERE topic='p0.intent.cancel_job'").fetchone())
            self.assertEqual(self.receive(mid), change)
            evidence.append(balance)
        self.assertIn("已超新上限",evidence[-1]["message"])
        self.assertEqual(evidence[-1]["spent_over_limit"],10)
        budgets.settle(self.db, held, actual={"tokens":35}, actor="test")
        final, = intent_budget.current_caps(self.db, self.run)
        self.assertEqual((final["spent"],final["held"],final["spent_over_limit"]), (75,0,45))
        if output := os.environ.get("GX_P0_CAP_EVIDENCE"):
            Path(output).write_text(json.dumps({"ranges":evidence,"after_settlement":final}, indent=2, ensure_ascii=False)+"\n")

    def test_cancel_requires_acknowledging_the_displayed_jobs_and_consequences(self):
        job = self.job(3)
        _, preview = self.text("Lower project token budget to 2000 and cancel job " + job)
        self.assertEqual(preview["status"], "awaiting_confirmation")
        effect, = [r for r in preview["impact"] if r["action"] == "request_cancel"]
        self.assertEqual(effect["object_id"], job)
        self.assertTrue(effect["held"])
        self.assertEqual(self.versions(), {})
        self.display()
        self.assertEqual(self.text("确认刚展示的这项变更")[1]["status"], "needs_clarification")
        self.assertFalse(self.db.execute("SELECT 1 FROM outbox WHERE topic='p0.intent.cancel_job'").fetchone())
        mid, accepted = self.text(preview["required_confirmation"]["text"])
        self.assertEqual(accepted["status"], "committed")
        self.assertEqual(self.receive(mid), accepted)
        self.driver.deliveries()
        self.driver.deliveries()
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox WHERE topic='p0.intent.cancel_job' "
                                         "AND consumed_at IS NOT NULL").fetchone()[0], 1)
        self.assertEqual(JobService(self.db).wait(job, timeout_seconds=10)["status"], "CANCELLED")

    def test_completed_job_wins_cancellation_race(self):
        job = self.job(0)
        _, preview = self.text("Cancel job " + job)
        self.assertEqual(preview["status"], "awaiting_confirmation")
        self.display()
        self.assertEqual(JobService(self.db).wait(job, timeout_seconds=10)["status"], "SUCCEEDED")
        self.assertEqual(self.text(preview["required_confirmation"]["english"])[1]["status"], "needs_clarification")
        self.assertFalse(self.db.execute("SELECT 1 FROM outbox WHERE topic='p0.intent.cancel_job'").fetchone())
