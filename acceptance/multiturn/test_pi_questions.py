"""PI questions travel through the normal decision and message owners; only Codex transport is scripted."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import GOAL, PRODUCT, draft
from acceptance.v621 import test_goal_versions
from acceptance.v622.test_close import CloseStudy, actual_packet


def ask(study, actor, key, *questions):
    decision = dict(draft(), pi_questions=list(questions))
    return study.call(actor, "gx_decide_revision", {"decision": decision}, key)["pi_questions"]


def tasks(study, scope, *, active=False):
    sql = """SELECT d.task_id FROM v6_decision_tasks d JOIN tasks t USING(task_id)
             WHERE d.run_id=? AND d.scope=?"""
    if active:
        sql += " AND t.status IN ('READY','CLAIMED','RUNNING')"
    return [row[0] for row in study.conn.execute(sql + " ORDER BY d.rowid", (study.run_id, scope))]


def cli(study, command, *arguments):
    return subprocess.run([sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), command,
                           "--state", str(study.state), *arguments],
                          env={**os.environ, "GX_CODEX_OFFLINE": "1"}, timeout=30,
                          text=True, capture_output=True)


def trace(name, **fields):
    if directory := os.environ.get("GX_MULTITURN_PI_TRACE"):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        fields["transport"] = "scripted native acknowledgment; no paid model calls"
        (path / (name + ".json")).write_text(json.dumps(fields, ensure_ascii=False, indent=2) + "\n")


class PIQuestions(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "questions"

    def answer(self, study, qid, mid, text):
        result = cli(study, "message", "--run-id", study.run_id, "--question-id", qid,
                     "--message-id", mid, "--text", text)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def route_message(self, study, message_id, text, kind, **anchors):
        from gx_core.p0.driver import Driver
        from tests.test_v6_operator_recovery import HumanReceipt

        sent = cli(study, "message", "--run-id", study.run_id, "--message-id", message_id,
                   "--text", text, "--queue-only")
        self.assertEqual(sent.returncode, 0, sent.stderr)
        row = study.conn.execute("SELECT * FROM p0_operator_messages WHERE run_id=? AND message_id=?",
                                 (study.run_id, message_id)).fetchone()
        driver = Driver(study.conn, study.state, study.run_id)
        try:
            driver._human_turn(HumanReceipt(), row)
            return driver.main_read("gx_route_pi_message", {"clauses": [{"kind": kind, "quote": text, **anchors}]},
                                    {"thread_id": "test-native-main", "turn_id": "test-turn-" + message_id})
        finally:
            driver.close()

    def test_answer_one_of_two_after_cold_restart_leaves_other_waiting(self):
        with CloseStudy(self.work) as s:
            a = s.open("classify-missing-sites", inputs=[s.materials[1]])
            b = s.open("inspect-second-material", inputs=[s.materials[0]])
            ar, br = s.bind(a["task_id"]), s.bind(b["task_id"])
            original_a, original_b = actual_packet(s, ar), actual_packet(s, br)

            first = {"question": "Report missing sites separately or mark them unknown?",
                     "choices": ["Separate table", "Unknown"], "blocking": True}
            second = {"question": "Compare deposited chains or publish a source inventory?",
                      "choices": ["Compare chains", "Inventory"], "blocking": True}
            qa, = ask(s, ar, "ask-a", first)
            self.assertEqual([qa], ask(s, ar, "ask-a", first))
            self.assertEqual((qa["scope"], qa["branch_id"], qa["goal_version"]),
                             (a["scope"], a["branch_id"], 1))
            # B runs a real coordinate inventory and receives its result while A waits.
            measured = s.submit_node(br, "inventory-second", s.materials[0])
            job = s.settle(measured["aliases"]["local:inventory-second"])
            br = s.next_actor(b["scope"])
            self.assertIn(job["result_ref"], json.dumps(actual_packet(s, br)["packet"]))
            qb, = ask(s, br, "ask-b", second)
            self.assertNotEqual(qb["question_id"], qa["question_id"])
            self.assertEqual(qb["scope"], b["scope"])
            from gx_core.p0 import research_dialogue

            self.assertEqual({q["question_id"] for q in research_dialogue.questions(s.conn, s.run_id)
                              if q["status"] == "OPEN"}, {qa["question_id"], qb["question_id"]})
            self.assertEqual(actual_packet(s, ar), original_a)
            self.assertNotIn(first["question"], json.dumps(original_b))
            a_before, b_before = tasks(s, a["scope"]), tasks(s, b["scope"])
            # Both question IDs survive other PI turns and persist after the active GX process disconnects.
            self.route_message(s, "asking-about-sources", "What does a missing coordinate mean?", "question")
            self.route_message(s, "asking-about-units", "Which units will you report?", "question")
            self.assertEqual(tasks(s, a["scope"]), a_before)
            self.assertEqual(tasks(s, b["scope"]), b_before)

        with CloseStudy(self.work, reopen=True) as s:
            s.advance()
            self.assertEqual(tasks(s, a["scope"], active=True), [])
            self.assertEqual(tasks(s, b["scope"], active=True), [])
            mistargeted = cli(s, "message", "--run-id", s.run_id, "--question-id", qa["question_id"],
                              "--branch-id", b["branch_id"], "--message-id", "conflicting-anchors",
                              "--text", "Choose the first option.")
            self.assertNotEqual(mistargeted.returncode, 0, mistargeted.stdout)
            s.advance()
            self.assertEqual(tasks(s, a["scope"], active=True), [])
            self.assertEqual(tasks(s, b["scope"], active=True), [])
            raw = "前者。請附上缺失位置表。"
            first_reply = self.answer(s, qa["question_id"], "pi-answer-a", raw)
            s.advance()
            self.assertEqual(len(tasks(s, a["scope"])), len(a_before) + 1)
            self.assertEqual(tasks(s, b["scope"]), b_before)
            self.assertEqual(tasks(s, b["scope"], active=True), [])
            replay = self.answer(s, qa["question_id"], "pi-answer-a", raw)
            self.assertIn("ANSWERED", json.dumps(first_reply).upper())
            self.assertIn("ANSWERED", json.dumps(replay).upper())
            second_reply = self.answer(s, qa["question_id"], "late-second-answer-a", "Actually choose the latter.")
            self.assertIn("ALREADY_ANSWERED", json.dumps(second_reply).upper())
            s.advance()
            self.assertEqual(len(tasks(s, a["scope"])), len(a_before) + 1)
            self.assertEqual(tasks(s, b["scope"]), b_before)
            refreshed = actual_packet(s, s.next_actor(a["scope"]))["packet"]["pi_dialogue"]
            answer, = [item for item in refreshed if item["kind"] == "answer"
                       and item["question_id"] == qa["question_id"]]
            self.assertEqual((answer["answer"], answer["question"], answer["answer_message_id"]),
                             (raw, first["question"], "pi-answer-a"))
            self.assertEqual(answer["choices"], first["choices"])
            self.assertNotIn(qb["question_id"], json.dumps(refreshed))
            self.assertNotIn("Actually choose the latter.", json.dumps(refreshed))
            self.assertEqual({q["question_id"]: q["status"] for q in research_dialogue.questions(s.conn, s.run_id)},
                             {qa["question_id"]: "ANSWERED", qb["question_id"]: "OPEN"})
            self.assertEqual(len(s.rows("SELECT 1 FROM jobs WHERE run_id=?", s.run_id)), 1)
            trace("two-open", run_id=s.run_id, original_questions=[qa, qb], answer=answer,
                  original_a_brief_sha256=original_a["text_sha256"], a_tasks_before=a_before,
                  a_tasks_after=tasks(s, a["scope"]), b_tasks_before=b_before, b_tasks_after=tasks(s, b["scope"]),
                  b_job=job["job_id"], b_result=job["result_ref"], replay=replay, second_reply=second_reply)

    def test_lead_can_target_an_active_branch_and_late_old_answer_never_wakes_replacement(self):
        with CloseStudy(self.work) as s:
            assigned = s.open("check-coverage")
            actor = s.bind(assigned["task_id"])
            original = actual_packet(s, actor)
            question = {"question": "Which scope should this coverage comparison use?",
                        "choices": ["All chains", "Deposited pairs"], "branch_id": assigned["branch_id"],
                        "blocking": True}
            asked, = ask(s, s.lead, "lead-asks-worker", question)
            self.assertEqual((asked["scope"], asked["branch_id"]), (assigned["scope"], assigned["branch_id"]))
            self.assertEqual(actual_packet(s, actor), original)
            test_goal_versions.GoalVersions().change_goal(s)
            before = tasks(s, assigned["scope"])
            result = self.answer(s, asked["question_id"], "after-change", "Use all chains.")
            self.assertIn("STALE", json.dumps(result).upper())
            s.advance()
            self.assertEqual(tasks(s, assigned["scope"]), before)
            from gx_core.p0 import research_dialogue

            historical, = research_dialogue.questions(s.conn, s.run_id, goal_version=1)
            self.assertEqual(historical["status"], "STALE")
            current = actual_packet(s, s.next_actor("run"))["packet"]
            self.assertEqual(current["goal_version"], 2)
            self.assertNotIn("Use all chains.", json.dumps(current))
            trace("old-version", run_id=s.run_id, original=asked, late=result, original_tasks=before,
                  after=tasks(s, assigned["scope"]), current_goal_version=current["goal_version"])

    def test_nonblocking_question_keeps_working_and_reminder_reuses_original_question(self):
        from gx_core.p0 import research_dialogue

        with CloseStudy(self.work) as s:
            module = s.open("catalog-sources")
            actor = s.bind(module["task_id"])
            optional = {"question": "Would a source inventory appendix be useful?", "blocking": False}
            first, = ask(s, actor, "offer-appendix", optional)
            s.advance()
            next_turn = s.next_actor(module["scope"])
            seen = actual_packet(s, next_turn)["packet"]["pi_dialogue"]
            self.assertEqual([item["question_id"] for item in seen if item["kind"] == "question"],
                             [first["question_id"]])
            repeated, = ask(s, next_turn, "remind-about-appendix", optional)
            self.assertEqual(repeated["question_id"], first["question_id"])
            self.assertEqual(len(research_dialogue.questions(s.conn, s.run_id)), 1)
            self.assertEqual(s.rows("SELECT count(*) AS n FROM jobs WHERE run_id=?", s.run_id), [{"n": 0}])

    def test_feedback_disposition_requires_its_delivered_brief_and_links_module_report(self):
        from gx_core.errors import GxError
        from gx_core.p0 import research_dialogue

        with CloseStudy(self.work) as s:
            a = s.open("check-missingness", inputs=[s.materials[0], s.materials[-1]])
            b = s.open("independent-inventory", inputs=[s.materials[1]])
            ar, br = s.bind(a["task_id"]), s.bind(b["task_id"])
            original_a, original_b = actual_packet(s, ar), actual_packet(s, br)
            b_before = tasks(s, b["scope"])
            s.call(ar, "gx_read_artifact", {"ref": s.materials[-1], "operation_key": "read-marker"}, "read-marker")
            source_id = "pi-check-missing"
            raw = "Please report which coordinates the method could not measure in this source."
            self.route_message(s, source_id, raw, "feedback", branch_id=a["branch_id"])
            pending, = research_dialogue.feedback_status(s.conn, s.run_id, source_id)
            self.assertEqual(pending["status"], "PENDING")
            self.assertEqual(actual_packet(s, ar), original_a)
            self.assertEqual(actual_packet(s, br), original_b)
            choice = {"feedback_id": pending["feedback_id"], "outcome": "adopt",
                      "reason": "The coordinate inventory identifies what could be measured."}
            with self.assertRaises(GxError) as error:
                s.report(ar, s.materials[-1], key="unseen-feedback", pi_dispositions=[choice])
            self.assertRegex(str(error.exception).lower(), "feedback|suggestion|frozen|delivered")
            self.assertEqual(s.rows("SELECT 1 FROM v62_module_reports"), [])
            measured = s.submit_node(ar, "actual-inventory", s.materials[0])
            job = s.settle(measured["aliases"]["local:actual-inventory"])

        with CloseStudy(self.work, reopen=True) as s:
            refreshed = s.next_actor(a["scope"])
            seen = actual_packet(s, refreshed)["packet"]["pi_dialogue"]
            visible, = [item for item in seen if item["kind"] == "feedback" and item["feedback_id"] == choice["feedback_id"]]
            self.assertIn(raw, json.dumps(visible))
            delivered, = research_dialogue.feedback_status(s.conn, s.run_id, source_id)
            self.assertEqual(delivered["status"], "DELIVERED")
            delivery, = delivered["deliveries"]
            self.assertEqual((delivery["task_id"], delivery["thread_id"], delivery["turn_id"]),
                             (refreshed.task_id, refreshed.thread, refreshed.turn))
            self.assertEqual(actual_packet(s, br), original_b)
            receipt = s.report(refreshed, job["result_ref"], key="sourced-disposition", pi_dispositions=[choice])
            self.assertEqual(receipt, s.report(refreshed, job["result_ref"], key="sourced-disposition",
                                               pi_dispositions=[choice]))
            decided, = research_dialogue.feedback_status(s.conn, s.run_id, source_id)
            self.assertEqual(decided["status"], "DISPOSED")
            resolution = decided["disposition"]
            self.assertEqual((resolution["outcome"], resolution["reason"]), ("adopt", choice["reason"]))
            self.assertEqual((resolution["report_ref"], resolution["delivery_turn_id"]),
                             (receipt["report_ref"], refreshed.turn))
            self.assertEqual(resolution["delivery_event_id"], delivery["delivery_event_id"])
            self.assertIn(job["result_ref"], resolution["evidence_refs"])
            self.assertEqual(resolution["event_id"], receipt["pi_dispositions"][0]["event_id"])
            self.assertEqual(tasks(s, b["scope"]), b_before)
            self.assertEqual(len(s.rows("SELECT 1 FROM v62_module_reports")), 1)
            trace("report-disposition", run_id=s.run_id, source_id=source_id, pending=pending,
                  delivered=delivery, report_ref=receipt["report_ref"], disposition=resolution,
                  evidence_job=job["job_id"], evidence_ref=job["result_ref"], unaffected_tasks=b_before)

    def test_finished_module_hands_pending_feedback_to_lead_with_its_released_report(self):
        from gx_core.p0 import research_dialogue

        with CloseStudy(self.work) as s:
            a = s.open("source-inventory", inputs=[s.materials[-1]], wake_on=["gap"])
            b = s.open("unrelated-inventory", inputs=[s.materials[0]])
            ar, br = s.bind(a["task_id"]), s.bind(b["task_id"])
            lead0, a0, b0 = (actual_packet(s, actor) for actor in (s.lead, ar, br))
            waiting = draft(watches=[{"event": "module_report", "subject_ref": "run"}])
            s.call(s.lead, "gx_decide_revision", {"decision": waiting}, "wait-for-source-findings")
            mid = "pi-assess-sampling"
            text = "Please state whether this source inventory also establishes sampling independence."
            self.route_message(s, mid, text, "feedback", branch_id=a["branch_id"])
            pending, = research_dialogue.feedback_status(s.conn, s.run_id, mid)
            self.assertEqual(pending["status"], "PENDING")
            for packet in (lead0, actual_packet(s, ar), actual_packet(s, br)):
                self.assertNotIn(pending["feedback_id"], json.dumps(packet))
            self.assertEqual(actual_packet(s, ar), a0)
            s.call(ar, "gx_read_artifact", {"ref": s.materials[-1], "operation_key": "read-source"}, "read-source")
            report = s.report(ar, s.materials[-1], key="unamended-source-report",
                              gaps=["Sampling independence is not documented by this source."])
            self.assertIsNotNone(report["wake_event_id"])
            self.assertNotIn("pi_dispositions", report)
            self.assertEqual(tasks(s, a["scope"]), [ar.task_id])
            self.assertEqual(actual_packet(s, br), b0)

        with CloseStudy(self.work, reopen=True) as s:
            lead = s.next_actor("run")
            brief = actual_packet(s, lead)["packet"]
            self.assertIn(report["report_ref"], brief["evidence_refs"])
            handed, = [item for item in brief["pi_dialogue"] if item["kind"] == "feedback"]
            self.assertEqual((handed["feedback_id"], handed["scope"], handed["text"]),
                             (pending["feedback_id"], a["scope"], text))
            disposition = {"feedback_id": handed["feedback_id"], "outcome": "maintain",
                           "reason": "The released inventory cannot establish sampling independence."}
            decision = dict(draft(evidence=[report["report_ref"]]), pi_dispositions=[disposition])
            receipt = s.call(lead, "gx_decide_revision", {"decision": decision}, "maintain-inventory-scope")
            result, = research_dialogue.feedback_status(s.conn, s.run_id, mid)
            self.assertEqual((result["scope"], result["status"]), (a["scope"], "DISPOSED"))
            self.assertEqual([r["task_id"] for r in result["deliveries"]], [lead.task_id])
            adopted = result["disposition"]
            self.assertEqual((adopted["decision_ref"], adopted["outcome"], adopted["delivery_turn_id"]),
                             (receipt["decision_ref"], "maintain", lead.turn))
            self.assertIn(report["report_ref"], adopted["evidence_refs"])
            self.assertEqual(result["module_report_ref"], report["report_ref"])
            consequence, = result["lead_decisions"]
            self.assertEqual(consequence["decision_ref"], receipt["decision_ref"])
            self.assertIn(report["report_ref"], consequence["cited_refs"])
            self.assertEqual(actual_packet(s, br), b0)
            self.assertEqual(tasks(s, b["scope"]), [br.task_id])
            self.assertEqual(tasks(s, a["scope"]), [ar.task_id])
            trace("module-handback", run_id=s.run_id, source_message_id=mid, a_report=report,
                  disposition=adopted, lead=consequence, unaffected_b=br.task_id)

    def test_existing_other_run_cannot_answer_even_if_same_state_and_goal_version(self):
        per_run = {"job_submissions": 8, "worker_tasks": 20, "cpu_seconds": 500,
                   "api_calls": 20, "tokens": 50000, "gpu_jobs": 0, "gpu_minutes": 0, "wall_hours": 1}
        with CloseStudy(self.work, spec_updates={"run_budget": per_run}) as s:
            common = {"question": "Report absolute or relative coverage?", "blocking": True}
            a = s.open("public-inventory")
            q, = ask(s, s.bind(a["task_id"]), "coverage-choice", common)
            started = cli(s, "start", "--run-key", "private-other-project", "--goal", GOAL,
                          "--goal-config", str(s.directory / "grant.json"))
            self.assertEqual(started.returncode, 0, started.stderr)
            from gx_core.p0 import coordinator, research_dialogue
            from gx_core.p0.roots import Roots

            coordinator.drain_starts(s.conn, s.state)
            other_id, = s.conn.execute("SELECT run_id FROM v6_goals WHERE run_id<>?", (s.run_id,)).fetchone()
            # Use the same coordinator-owned native state with the second Run's genuine Lead.
            other = CloseStudy(self.work, reopen=True)
            other.conn, other.run_id = s.conn, other_id
            inputs, = s.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (other_id,)).fetchone()
            other.materials = [item["artifact_ref"] for item in json.loads(inputs)["files"]]
            other.roots = Roots(s.conn, run_id=other_id, cwd=s.state, token_budget=10)
            other.roots.client = s.roots.client
            other.next_actor("run")
            b = other.open("public-inventory")
            other_q, = ask(other, other.bind(b["task_id"]), "coverage-choice", common)
            original, other_before = tasks(s, a["scope"]), tasks(other, b["scope"])
            result = cli(s, "message", "--run-id", other_id, "--question-id", q["question_id"],
                         "--message-id", "wrong-run", "--text", "Relative coverage.")
            self.assertNotEqual(result.returncode, 0, result.stdout)
            s.advance()
            other.advance()
            self.assertEqual(tasks(s, a["scope"]), original)
            self.assertEqual(tasks(other, b["scope"]), other_before)
            self.assertEqual(research_dialogue.questions(s.conn, s.run_id)[0]["status"], "OPEN")
            self.assertEqual(research_dialogue.questions(s.conn, other_id)[0]["status"], "OPEN")
            self.assertNotEqual(q["question_id"], other_q["question_id"])
            self.answer(other, other_q["question_id"], "right-run", "Relative coverage.")
            s.advance()
            other.advance()
            self.assertEqual(tasks(s, a["scope"]), original)
            self.assertEqual(len(tasks(other, b["scope"])), len(other_before) + 1)
            trace("same-named-routes", run_ids=[s.run_id, other_id], questions=[q, other_q],
                  wrong_run_cli_exit=result.returncode, wrong_run_message=result.stderr,
                  first_tasks=original, second_before=other_before, second_after=tasks(other, b["scope"]))



if __name__ == "__main__":
    unittest.main()
