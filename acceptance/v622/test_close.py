"""R6. A close prepared from an acknowledged brief races an actual Job/report settlement."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from acceptance.v62a.interleavings_fixture import Actor, Scenario, draft
from acceptance.v621.test_evidence_gate import account, event, job_reservations, node
from tests.test_v6_decisions import branch
from gx_core import budgets
from gx_core.errors import GxError, ScopeDenied, StaleRevision
from gx_core.p0 import v6_decisions, v6_goal, v6_native
from gx_core.p0.roots import Roots
from gx_core.persistence import store


class CloseStudy(Scenario):
    """Use the real delivery/ack/hold path on both baseline and candidate with a local responder."""
    def delivery_text(self, row):
        return row["brief"]

    def bind(self, task_id):
        row = self.roots._claim(task_id)
        thread = row["thread_id"] or self.roots._bind(
            row, {"id":"script-" + row["lease_id"], "cwd":row["cwd"], "source":"appServer"})
        turn = "turn-" + row["lease_id"]
        text = self.delivery_text(row)

        def respond(method, params):
            assert params["threadId"] == thread
            if method == "thread/read":
                return {"thread":{"id":thread, "status":{"type":"idle"}}}
            assert method == "turn/start" and params["input"][0]["text"] == text
            return {"turn":{"id":turn}}

        with patch.object(self.roots, "_request", respond):
            delivered = self.roots.delivery.deliver(thread, text, event_id="start:" + row["root_key"])
        assert delivered["turn_id"] == turn
        self.roots.active_turns[thread] = turn
        scope, = self.conn.execute("SELECT scope FROM v6_decision_tasks WHERE task_id=?", (task_id,)).fetchone()
        actor = Actor(task_id, row["lease_id"], scope, thread, turn)
        self.actors[actor.lease_id] = actor
        if scope == "run":
            self.lead = actor
        return actor


class SurveyedStudy(CloseStudy):
    """Scripted research actor for tests of other features under the current normal Goal policy."""
    def bind(self, task_id):
        actor = super().bind(task_id)
        if actor.scope == "run":
            brief = self.brief(actor)
            survey = brief.get("catalog_discovery")
            pending = brief.get("pi_method_scope", {}).get("status") == "pending"
            if survey and (not survey.get("survey_event") or pending):
                # This fixture's neutral Goal has no PI method clauses.
                args = {"kind": "all", **({"pi_method_scope": []} if pending else {})}
                self.call(actor, "gx_query_catalog", args, "scripted-catalog-survey")
        return actor


def close_decision(references=()):
    return draft({"op": "close_run", "reason": "Record the known coverage at closure.",
                  "claim_refs": [], "unresolved": ["Sampling independence remains unknown."]},
                 evidence=references)


def trace(name, result):
    if folder := os.getenv("GX622_CLOSE_TRACE_DIR"):
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        (path / (name + ".json")).write_text(json.dumps(result, indent=2) + "\n")


def actual_packet(s, actor):
    brief, = s.conn.execute("SELECT brief FROM p0_roots WHERE thread_id=?", (actor.thread,)).fetchone()
    receipt, digest = s.conn.execute("SELECT state,text_sha256 FROM p0_root_deliveries WHERE thread_id=? AND turn_id=?",
                              (actor.thread, actor.turn)).fetchone()
    assert (receipt, digest) == ("acknowledged", hashlib.sha256(brief.encode()).hexdigest())
    return {"task":actor.task_id, "turn":actor.turn, "text_sha256":hashlib.sha256(brief.encode()).hexdigest(),
            "packet":json.loads(brief.removeprefix(v6_native.PREFIX))}


def paired_close(s, actor, decision):
    """Two native callbacks with identical identity/content, both prepared before either can commit."""
    gate = Barrier(2, timeout=10)
    prepare = v6_decisions.prepare
    def paused(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        gate.wait()
        return prepared

    def attempt(_):
        with closing(store.open_store(s.state / "gx.db")) as db:
            roots = Roots(db, run_id=s.run_id, cwd=s.state, token_budget=10)
            roots.client = s.roots.client
            roots.active_turns[actor.thread] = actor.turn
            params = dict(threadId=actor.thread, turnId=actor.turn, tool="gx_decide_revision",
                          arguments={"decision":decision}, callId="close-after-review")
            if roots.terminal_products:
                params["namespace"] = "gx"
            return roots._call(params)

    with patch.object(v6_decisions, "prepare", paused), ThreadPoolExecutor(2) as pool:
        return list(pool.map(attempt, range(2)))


class CloseFreshness(unittest.TestCase):
    def test_real_job_settles_between_prepare_and_write_then_cold_concurrent_close(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "direct"
        with CloseStudy(work) as s:
            study = branch("study", jobs=1)
            study["budget_limits"]["cpu_seconds"] = 120
            measurement = dict(node("inventory", s.materials[0]), capability="tl1a-coordinate-quality@1", parameters={})
            first_decision = draft(study, measurement)
            adopted = s.call(s.lead, "gx_decide_revision", {"decision": first_decision}, "open")
            measured_node = adopted["aliases"]["local:inventory"]
            v6_goal.record_observation(s.conn, s.run_id, s.materials[-1], source="operator-intervention")
            before = s.next_actor("run")
            first = actual_packet(s, before)
            self.assertEqual(first["packet"]["goal_version"], 1)
            prepare = v6_decisions.prepare
            def settle_after_prepare(*args, **kwargs):
                prepared = prepare(*args, **kwargs)
                s.settle(measured_node)
                return prepared

            with patch.object(v6_decisions, "prepare", settle_after_prepare):
                try:
                    old = s.call(before, "gx_decide_revision", {"decision":close_decision()}, "old-close")
                    err = None
                except StaleRevision as exc:
                    old, err = None, exc.details
            result, = event(s, measured_node)
            self.assertGreater(result["event_id"], first["packet"]["trigger"]["through_cursor"])
            report = {"case":"late_direct_job", "run_id":s.run_id, "model":"local scripted native acknowledgement",
                      "provider_calls":0,"initial_task":first["task"],"initial_turn":first["turn"],
                      "initial_brief_sha256":first["text_sha256"],"initial_cursor":first["packet"]["trigger"]["through_cursor"],
                      "job_event":result,"attempted_close":old,"refusal":err,
                      "node_head":s.head(), "jobs":account(s), "charges":job_reservations(s)}
            trace("late-direct", report)
            self.assertIsNone(old, "The original close committed after the new direct Job result")
            self.assertEqual(err["event_id"], result["event_id"])
            with self.assertRaises(GxError):
                s.call(before, "gx_decide_revision", {"decision":close_decision()}, "old-close")
            self.assertEqual(len(s.rows("SELECT 1 FROM events WHERE kind='v6.decision.superseded' "
                                        "AND subject_id=?", before.task_id)), 1)
            self.assertEqual(len(report["jobs"]), 1)
            self.assertEqual(s.rows("SELECT status FROM v6_goals"), [{"status":"OPEN"}])
            self.assertFalse(s.rows("SELECT 1 FROM events WHERE kind='v6.run.close_requested'"))
        with CloseStudy(work, reopen=True) as s:
            successor = s.next_actor("run")
            second = actual_packet(s, successor)
            self.assertIn(result["result_ref"], second["packet"]["evidence_refs"])
            self.assertTrue(any(row.get("artifact_ref") == result["result_ref"] for row in second["packet"]["observations"]))
            requested = close_decision([result["result_ref"]])
            turns = s.rows("SELECT thread_id,turn_id,reservation_ref FROM p0_root_deliveries ORDER BY rowid")
            accepted, duplicate = paired_close(s, successor, requested)
            self.assertEqual(accepted["close_context"]["brief_sha256"], second["text_sha256"])
            self.assertEqual(s.rows("SELECT thread_id,turn_id,reservation_ref FROM p0_root_deliveries ORDER BY rowid"),
                             turns)
            self.assertEqual(duplicate, accepted)
            s.advance()
            self.assertEqual(s.rows("SELECT status FROM v6_goals"), [{"status":"CLOSED"}])
            self.assertEqual(len(s.rows("SELECT 1 FROM events WHERE kind='v6.run.close_requested'")),1)
            self.assertEqual(account(s), report["jobs"])
            self.assertEqual(job_reservations(s), report["charges"])
            report.update(refreshed_brief_sha256=second["text_sha256"], refreshed_cursor=second["packet"]["trigger"]["through_cursor"],
                          reviewed_result_ref=result["result_ref"], close_receipt=accepted, replay_identical=True, closed=True,
                          additional_jobs=0, additional_job_charges=0)
            trace("late-direct",report)

    def test_report_after_preparation_is_delivered_on_cold_restart(self):
        from acceptance.v621.test_goal_versions import GoalVersions

        work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "delegated"
        with CloseStudy(work) as s:
            worker = GoalVersions().jobless_module(s, "late-report")
            previous = actual_packet(s, s.lead)
            prepare = v6_decisions.prepare
            submitted = {}

            def finish_report(*args, **kwargs):
                prepared = prepare(*args, **kwargs)
                submitted.update(s.report(worker, s.materials[-1]))
                return prepared

            with patch.object(v6_decisions, "prepare", finish_report), self.assertRaises(StaleRevision) as stale:
                s.call(s.lead, "gx_decide_revision", {"decision": close_decision()}, "close-before-report")
            self.assertEqual(stale.exception.details["reason"], "unseen_module_report")
            self.assertEqual(s.report(worker, s.materials[-1]), submitted)
            self.assertNotIn(submitted["report_ref"], previous["packet"]["evidence_refs"])
            self.assertEqual(s.rows("SELECT status FROM v6_goals"), [{"status":"OPEN"}])
            self.assertFalse(account(s))

        with CloseStudy(work, reopen=True) as s:
            current = actual_packet(s, s.next_actor("run"))
            self.assertIn(submitted["report_ref"], current["packet"]["evidence_refs"])
            self.assertIn(submitted["report_ref"], [r["report_ref"] for r in current["packet"]["module_reports"]])
            decision = close_decision([submitted["report_ref"]])
            receipt = s.call(s.lead, "gx_decide_revision", {"decision":decision}, "close-after-report")
            self.assertEqual(receipt, s.call(s.lead, "gx_decide_revision", {"decision":decision}, "close-after-report"))
            s.advance()
            self.assertEqual(s.rows("SELECT status FROM v6_goals"), [{"status":"CLOSED"}])
            self.assertEqual(len(s.rows("SELECT 1 FROM v62_module_reports")), 1)
            self.assertEqual(len(s.rows("SELECT 1 FROM v62_module_wakes")), 1)
            self.assertEqual(account(s), [])
            self.assertEqual(job_reservations(s), [])
            trace("late-module", dict(run_id=s.run_id, original_brief=previous, refreshed_brief=current,
                                       result=submitted, rejected=stale.exception.details, close=receipt,
                                       wakes=1, replay_identical=True, jobs=0, job_charges=0))

    def test_old_goal_report_and_budget_activity_after_new_brief_do_not_block_close(self):
        from acceptance.v621.test_goal_versions import GoalVersions
        from gx_core.p0 import v62_modules

        work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "replacement"
        pi = GoalVersions()
        with CloseStudy(work) as s:
            worker = pi.jobless_module(s, "old-report")
            withdrawn = pi.jobless_module(s, "old-incomplete")
            report = s.report(worker, s.materials[-1])
            self.assertIsNone(report["wake_event_id"])
            pi.change_goal(s)

        with CloseStudy(work, reopen=True) as s:
            current = actual_packet(s, s.next_actor("run"))
            self.assertEqual(current["packet"]["goal_version"], 2)
            self.assertNotIn(report["report_ref"], current["packet"]["evidence_refs"])
            cursor = current["packet"]["trigger"]["through_cursor"]
            v62_modules.advance(s.conn, s.run_id)
            v62_modules.advance(s.conn, s.run_id)
            released, = s.rows("SELECT e.event_id,e.payload_json FROM v62_module_wakes w JOIN events e USING(event_id)")
            payload = json.loads(released["payload_json"])
            self.assertEqual((payload["goal_version"], payload["report_refs"]), (1, [report["report_ref"]]))
            self.assertGreater(released["event_id"], cursor)
            with self.assertRaises(GxError):
                s.report(withdrawn, s.materials[-1], "revoked")

            # Make a new, Run-visible funding event via its real owner after the successor received its brief.
            from gx_core.budgets.allotment import set_run_limit
            budget = budgets.budget_for(s.conn, run_id=s.run_id)
            set_run_limit(s.conn, s.run_id, budget, key="tokens", limit=110000, actor="test-operator")
            last_budget, = s.rows("SELECT kind,event_id FROM events WHERE kind='budget.allotment_changed' "
                                  "AND subject_id=? AND event_id>?", budget, cursor)
            decision = close_decision()
            closed = s.call(s.lead, "gx_decide_revision", {"decision":decision}, "goal-two-close")
            self.assertEqual(closed["goal_version"], 2)
            s.advance()
            self.assertEqual(s.rows("SELECT status FROM v6_goals"), [{"status":"CLOSED"}])
            self.assertEqual(account(s), [])
            self.assertEqual(job_reservations(s), [])
            trace("old-goal-and-accounting", dict(run_id=s.run_id, new_goal_brief=current,
                historical_report=report, historical_report_released=released["event_id"], budget_event=last_budget,
                closed=closed, jobs=0, job_charges=0))

    def test_later_turn_on_same_thread_can_use_its_acknowledged_brief(self):
        from dataclasses import replace

        work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "resumed"
        with CloseStudy(work) as s:
            actor = s.lead
            first = actual_packet(s, actor)
            resumed = "resumed-" + actor.turn
            delivery = s.roots.delivery
            delivery.observe("turn/started", {"threadId": actor.thread, "turn": {"id": actor.turn}})
            delivery.observe("rawResponse/completed", {"threadId": actor.thread, "turnId": actor.turn,
                                                       "responseId": "local-response", "usage": {"totalTokens": 8}})
            delivery.observe("thread/tokenUsage/updated", {"threadId": actor.thread, "turnId": actor.turn,
                                                           "tokenUsage": {"total": {"totalTokens": 8}}})
            delivery.observe("turn/completed", {"threadId": actor.thread,
                                                "turn": {"id": actor.turn, "status": "completed"}})

            def respond(method, params):
                self.assertEqual(params["threadId"], actor.thread)
                if method == "thread/read":
                    return {"thread":{"id":actor.thread, "status":{"type":"idle"}}}
                self.assertEqual(method, "turn/start")
                return {"turn":{"id":resumed}}

            with patch.object(s.roots, "_request", respond):
                receipt = s.roots.delivery.deliver(actor.thread, "Continue from the original research brief.",
                                                   event_id="resume:" + actor.turn)
            self.assertEqual(receipt["turn_id"], resumed)
            actor = replace(actor, turn=resumed)
            s.roots.active_turns[actor.thread] = resumed
            closed = s.call(actor, "gx_decide_revision", {"decision":close_decision()}, "resumed-close")
            context = closed["close_context"]
            self.assertEqual((context["brief_turn"], context["closing_turn"], context["brief_sha256"]),
                             (first["turn"], resumed, first["text_sha256"]))
            self.assertEqual(account(s), [])

    def test_other_acknowledged_input_does_not_prove_frozen_brief_delivery(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "unseen"
        with patch.object(CloseStudy, "delivery_text", return_value="An unrelated message."):
            with CloseStudy(work) as s:
                self.assertEqual(s.rows("SELECT state FROM p0_root_deliveries"), [{"state":"acknowledged"}])
                with self.assertRaisesRegex(ScopeDenied, "acknowledged research brief"):
                    s.call(s.lead, "gx_decide_revision", {"decision":close_decision()}, "unseen-close")
                self.assertEqual(s.rows("SELECT status FROM v6_goals"), [{"status":"OPEN"}])
                self.assertFalse(s.rows("SELECT 1 FROM events WHERE kind='v6.run.close_requested'"))
                self.assertEqual(account(s), [])


    def test_unsettled_job_keeps_its_identity_when_quick_lead_closes(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "inflight"
        with CloseStudy(work, spec_updates={"research_effort":"quick", "research_modules":[]}) as s:
            study = branch("study", jobs=1)
            study["budget_limits"]["cpu_seconds"] = 120
            measure = dict(node("coverage", s.materials[0]), capability="tl1a-coordinate-quality@1", parameters={})
            adopted = s.call(s.lead, "gx_decide_revision", {"decision":draft(study, measure)}, "open")
            node_id = adopted["aliases"]["local:coverage"]
            # One owner tick hands the real Job to the runner. Its result has not entered the graph.
            from gx_core.p0 import v6_jobs
            v6_jobs.advance(s.conn, s.run_id)
            issued, = account(s)
            self.assertFalse(event(s, node_id))
            v6_goal.record_observation(s.conn, s.run_id, s.materials[-1], source="operator-intervention")
            actor = s.next_actor("run")
            before = actual_packet(s, actor)
            decision = close_decision()
            accepted = s.call(actor, "gx_decide_revision", {"decision":decision}, "quick-close")
            self.assertEqual(accepted["close_context"]["brief_sha256"], before["text_sha256"])
            self.assertEqual(s.rows("SELECT status FROM v6_goals"), [{"status":"CLOSING"}])
            self.assertEqual(s.rows("SELECT status,job_id FROM v6_nodes WHERE node_id=?", node_id),
                             [{"status":"CANCEL_REQUESTED", "job_id":issued["job_id"]}])
        with CloseStudy(work, reopen=True) as s:
            import time
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                s.advance()
                if s.rows("SELECT status FROM v6_goals") == [{"status":"CLOSED"}]:
                    break
                time.sleep(.035)
            self.assertEqual(s.rows("SELECT status FROM v6_goals"), [{"status":"CLOSED"}])
            last, = account(s)
            self.assertEqual(last["job_id"], issued["job_id"])
            self.assertEqual(last["reservation_ref"], issued["reservation_ref"])
            charges, = job_reservations(s)
            self.assertEqual(charges["reservation_ref"], last["reservation_ref"])
            trace("pending-at-close", dict(run_id=s.run_id, queued_job=issued, job=last, charge=charges,
                                           close=accepted, evidence_delivered_before_close=before))


if __name__ == "__main__":
    unittest.main()
