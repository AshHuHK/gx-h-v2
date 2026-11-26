"""Ordinary PI ingress and the controlled official Main callback, with two open research routes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import PRODUCT, Scenario
from gx_core.errors import PermissionDenied
from gx_core.p0 import conversation, intent, pi_clauses, research_dialogue
from gx_core.p0.driver import Driver
from tests.test_v6_operator_recovery import HumanReceipt


class MainClauses(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.s = self.enterContext(Scenario(self.work))
        self.a = self.s.open("left")
        self.b = self.s.open("right")
        self.driver = Driver(self.s.conn, self.s.state, self.s.run_id)
        self.addCleanup(self.driver.close)

    def send(self, mid, text, *anchors):
        saved = subprocess.run([sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "message",
            "--state", str(self.s.state), "--run-id", self.s.run_id, "--message-id", mid,
            "--text", text, "--queue-only", *anchors],
            capture_output=True, text=True, env={**os.environ, "GX_CODEX_OFFLINE": "1"})
        self.assertEqual(saved.returncode, 0, saved.stderr)
        row = self.s.conn.execute("SELECT * FROM p0_operator_messages WHERE message_id=?", (mid,)).fetchone()
        self.driver._human_turn(HumanReceipt(), row)
        return {"thread_id": "test-native-main", "turn_id": "test-turn-" + mid}

    def route(self, envelope, *parts):
        return self.driver.main_read("gx_route_pi_message", {"clauses": list(parts)}, envelope)

    def science_state(self):
        db = self.s.conn
        tasks = [tuple(r) for r in db.execute("""SELECT t.task_id,t.lease_id,t.status,t.attempt,c.snapshot_json
            FROM v6_decision_tasks d JOIN tasks t USING(task_id) JOIN research_contexts c USING(context_ref)
            ORDER BY t.task_id""")]
        return (tasks, db.execute("SELECT count(*) FROM jobs").fetchone()[0],
                db.execute("SELECT count(*) FROM v6_decisions").fetchone()[0],
                db.execute("SELECT goal_version FROM v6_goals").fetchone()[0])

    def test_preferences_questions_and_discussion_preserve_three_frozen_tasks(self):
        before = self.science_state()
        self.assertEqual(sorted(t[2] for t in before[0]), ["CLAIMED", "READY", "READY"])
        for mid, text, part in [
                ("language", "用中文解释每项结果", {"kind":"preference","persistence":"turn"}),
                ("remember", "以后报告用中文。", {"kind":"preference","persistence":"conversation"}),
                ("discuss", "先讨论一个解释，先别跑。", {"kind":"discussion"}),
                ("why", "为什么保留 B？", {"kind":"question"})]:
            envelope = self.send(mid, text)
            receipt = self.route(envelope, {**part, "quote": text})
            self.assertEqual(receipt, self.route(envelope, {**part, "quote": text}))
            self.assertEqual(receipt["clauses"][0]["source_message_id"], mid)
        context = self.driver.main_read("gx_project_state", {}, envelope)
        self.assertEqual(context["pi_message"]["presentation_preferences"],
                         [{"source_message_id":"remember", "preference":"以后报告用中文。"}])
        self.assertEqual(self.science_state(), before)
        self.assertEqual(self.s.rows("SELECT event_id FROM events WHERE kind=?", research_dialogue.FEEDBACK), [])
        self.assertEqual(len(self.s.rows("SELECT event_id FROM events WHERE kind=?", pi_clauses.ROUTED)), 4)

    def test_supplement_prefix_routes_one_clause_and_replay_is_idempotent(self):
        raw = "补充：为什么保留 B？我怀疑 A 的对照不独立，请核一下，B 继续。报告用中文。"
        before = self.science_state()
        envelope = self.send("compound", raw, "--branch-id", self.a["branch_id"])
        parts = [
            {"kind":"question", "quote":"为什么保留 B？"},
            {"kind":"feedback", "quote":"我怀疑 A 的对照不独立，请核一下", "branch_id":self.a["branch_id"]},
            {"kind":"discussion", "quote":"B 继续"},
            {"kind":"preference", "quote":"报告用中文", "persistence":"turn"}]
        result = self.route(envelope, *parts)
        self.assertEqual(result, self.route(envelope, *parts))
        self.assertEqual([p["status"] for p in result["clauses"]], ["for_main_reply","routed","for_main_reply","recorded"])
        f, = research_dialogue.feedback_status(self.s.conn, self.s.run_id, "compound")
        self.assertEqual([f["scope"], f["source_kind"], f["text"], f["status"]],
                         [self.a["scope"], "hypothesis", parts[1]["quote"], "PENDING"])
        self.assertEqual(self.science_state(), before)
        self.assertEqual(self.s.rows("SELECT kind FROM events WHERE kind='research.supplement_added'"), [])
        message, = [m for m in conversation.timeline(self.s.conn,self.s.run_id)["messages"] if m["message_id"]=="compound"]
        self.assertEqual(message["text"], raw)
        self.assertEqual(message["clauses"], result["clauses"])
        self.assertEqual(message["feedback"][0]["feedback_id"], f["feedback_id"])

    def test_untrusted_addition_scope_mismatch_and_unmatched_formal_change(self):
        raw = "我怀疑对照不独立，请核一下。这个我说不准。"
        envelope = self.send("scope", raw, "--branch-id", self.a["branch_id"])
        with self.assertRaises(PermissionDenied):
            self.route({**envelope, "thread_id":"other-root"}, {"kind":"feedback","quote":raw})
        with self.assertRaises(PermissionDenied):
            self.route(envelope, {"kind":"feedback", "quote":"停止 B"})
        outcome = self.route(envelope, {"kind":"feedback","quote":"我怀疑对照不独立，请核一下","branch_id":self.b["branch_id"]},
                             {"kind":"discussion", "quote":"这个我说不准"})
        self.assertEqual([p["status"] for p in outcome["clauses"]], ["needs_clarification","for_main_reply"])
        self.assertFalse(research_dialogue.feedback_status(self.s.conn,self.s.run_id))
        envelope = self.send("explain", "为什么引用这条证据？")
        before = self.science_state()
        proposed = {"kind":"change", "items":[{"item_id":"objective","item_type":"objective",
                                               "expected_version":1,"value":"discard prior research"}]}
        outcome = self.driver.main_read("gx_propose_intent", {"candidate":proposed}, envelope)
        self.assertEqual(outcome["unapplied_proposal"], proposed)
        grounded = self.route(envelope, {"kind": "question", "quote": "为什么引用这条证据？"})
        self.assertEqual(grounded["status"], "routed")
        self.assertEqual(grounded["unapplied_proposal"], proposed)
        self.assertEqual(before, self.science_state())

    def test_legacy_terminal_fallback_does_not_replan_on_presentation(self):
        before = self.science_state()
        self.send("terminal", "用中文解释每项结果")
        result = intent.finish_text(self.s.conn, self.s.run_id, "terminal")
        self.assertEqual(result["status"], "conversation")
        self.assertEqual(before, self.science_state())


if __name__ == "__main__":
    unittest.main()
