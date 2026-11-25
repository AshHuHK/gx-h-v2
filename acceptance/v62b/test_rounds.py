"""Public Goal scenarios for settlement, waiting, late arrivals and persistent convergence."""
import json
from pathlib import Path
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import Scenario, draft
from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid, ScopeDenied, StaleRevision
from gx_core.p0 import v6_goal, v62_rounds


class ResearchRounds(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "study"
        self.s = self.enterContext(Scenario(self.directory, spec_updates={
            "module_policy": {"max_rounds": 5, "no_progress_rounds": 2},
        }))
        self.ref = self.s.materials[0]

    def open(self, number, key, question):
        args = self.s.open_args(key, inputs=[self.ref], round=number, caps=[])
        args["expected_delivery"] = question
        receipt = self.s.call(self.s.lead, "gx_open_module", args, key)
        return self.s.bind(receipt["task_id"])

    def decide(self, key="wait", *, decision=None):
        return self.s.call(self.s.lead, "gx_decide_revision", {"decision": decision or draft()}, key)

    def report(self, actor, key="findings", **changes):
        self.s.call(actor, "gx_read_artifact", {"ref": self.ref, "operation_key": "read"}, "read")
        return self.s.report(actor, self.ref, key, **changes)

    def next(self):
        self.s.next_actor("run")
        return self.s.brief(self.s.lead)

    def round(self, number, inquiry, **changes):
        worker = self.open(number, f"question-{number}", inquiry)
        self.decide()
        report = self.report(worker, **changes)
        return report, self.next()["research_progress"]

    def convergence(self, reference):
        decision = draft(evidence=[reference])
        decision["rationale"] = "The existing assessments support selecting the documented candidate set."
        decision["convergence"] = {"spec_version": "coverage-rule/1", "candidates": ["recorded-coverage"]}
        return decision

    def measurement(self, name="reading-A"):
        artifact = ArtifactService(self.s.conn).publish_bytes(
            namespace=self.s.run_id + "/measurements", rel_path=name + ".json", data='{"reading":7}',
            kind="instrument_measurement", run_id=self.s.run_id, actor="operator", origin="INGEST")
        v6_goal.record_observation(self.s.conn, self.s.run_id, artifact.artifact_ref, source=name,
                                   acquisition={"source": "public-instrument-exercise/v1", "acquisition": "reading-A"})
        return artifact.artifact_ref

    def test_repeated_report_does_not_increment_streak_and_convergence_survives_cold_restart(self):
        _, first = self.round(1, "Check documented case coverage")
        self.assertEqual(first["rounds"][-1]["outcome"], "no_progress")
        self.assertEqual(first["consecutive_no_progress"], 1)
        _, repeated = self.round(2, "   CHECK documented case coverage ", text="This repeats the inventory.")
        self.assertEqual(repeated["rounds"][-1]["outcome"], "uncounted")
        self.assertEqual(repeated["consecutive_no_progress"], 1)
        report, final = self.round(3, "Check the separately requested uncertainty statement")
        self.assertEqual((final["phase"], final["consecutive_no_progress"]), ("convergence_due", 2))
        self.assertEqual(final["decision_options"], ["enter_convergence", "close_run"])
        with self.assertRaises(ScopeDenied):
            self.s.open("too-late", round=4)
        with self.assertRaises(ScopeDenied):
            self.decide(key="continue-exploring")
        receipt = self.decide("converge", decision=self.convergence(report["report_ref"]))
        self.assertEqual(receipt["convergence"]["candidates"], ["recorded-coverage"])
        self.assertEqual(receipt["convergence"]["spec_version"], "coverage-rule/1")
        self.assertEqual(receipt["convergence"]["evidence_refs"], [report["report_ref"]])
        self.assertEqual(self.decide("converge", decision=self.convergence(report["report_ref"])), receipt)
        self.s.stack.close()
        with Scenario(self.directory, reopen=True) as resumed:
            v62_rounds.reconcile(resumed.conn, resumed.run_id)
            current = v62_rounds.brief(resumed.conn, resumed.run_id, None)["research_progress"]
            self.assertEqual(current["convergence"], receipt["convergence"])
            self.assertEqual(len(current["rounds"]), 3)
            self.assertEqual(len(resumed.rows("SELECT event_id FROM events WHERE kind='v62.convergence.entered'")), 1)
            with self.assertRaises(ScopeDenied):
                v62_rounds.require_exploration(resumed.conn, resumed.run_id)

    def test_explicit_early_watch_waits_for_peer_and_late_report_wakes_only_once(self):
        a = self.open(1, "first", "Read first")
        b = self.open(1, "second", "Read second")
        self.decide(decision=draft(watches=[{"event": "module_report", "subject_ref": "run"}]))
        early = self.report(a)
        self.assertIsNotNone(early["wake_event_id"])
        self.assertEqual(self.s.rows("SELECT payload_json FROM events WHERE event_id=?", early["wake_event_id"])[0]
                         ["payload_json"].count('"reconsider_when"'), 1)
        frozen = self.next()
        self.assertEqual([r["report_ref"] for r in frozen["module_reports"]], [early["report_ref"]])
        self.assertEqual(frozen["research_progress"]["rounds"], [])
        self.assertEqual(frozen["research_progress"]["consecutive_no_progress"], 0)
        self.assertTrue(frozen["research_progress"]["waiting_for"])
        with self.assertRaises(ScopeDenied) as pending:
            self.decide("premature", decision=self.convergence(early["report_ref"]))
        self.assertIn("waiting_for", pending.exception.details)
        self.decide()
        late = self.report(b)
        self.assertNotEqual(early["wake_event_id"], late["wake_event_id"])
        late_brief = self.next()
        self.assertEqual({r["report_ref"] for r in late_brief["module_reports"]},
                         {early["report_ref"], late["report_ref"]})
        self.assertEqual(len(late_brief["research_progress"]["rounds"]), 1)
        self.assertEqual(self.s.report(b, self.ref, "findings"), late)
        self.decide()
        self.assertIsNone(v6_goal.ensure_task(self.s.conn, self.s.run_id))
        self.assertEqual(len(self.s.rows("SELECT event_id FROM v62_module_wakes")), 2)

    def test_no_event_or_unsupported_engineering_partial_is_an_uncounted_round(self):
        worker = self.open(1, "attempt", "Check an unavailable field")
        self.decide()
        for _ in range(3):
            self.assertIsNone(v6_goal.ensure_task(self.s.conn, self.s.run_id))
        self.assertEqual(self.s.rows("SELECT round_id FROM research_rounds"), [])
        receipt = self.s.report(worker, self.ref, status="PARTIAL", findings=[], artifact_refs=[],
                                gaps=["A local call could not complete."])
        self.assertEqual(receipt["status"], "PARTIAL")
        result = self.next()["research_progress"]
        self.assertEqual(result["rounds"][0]["outcome"], "uncounted")
        self.assertEqual(result["rounds"][0]["outcomes"][0]["status"], "PARTIAL")
        self.assertEqual(result["consecutive_no_progress"], 0)
        self.assertEqual(result["open_gaps"], [])
        row = self.s.conn.execute("SELECT * FROM research_rounds").fetchone()
        self.assertEqual(json.loads(row["expected_json"])[0]["expected_delivery"], "Check an unavailable field")

    def test_same_acquired_measurement_republished_under_an_alias_resets_streak_only_once(self):
        _, first = self.round(1, "Inspect original coverage")
        self.assertEqual(first["consecutive_no_progress"], 1)
        worker = self.open(2, "new-measure", "Inspect the public uncertainty statement")
        self.decide()
        original = self.measurement()
        v62_rounds.reconcile(self.s.conn, self.s.run_id)
        self.assertEqual(len(v62_rounds._history(self.s.conn, self.s.run_id)), 1)
        self.report(worker)
        self.assertEqual(self.next()["research_progress"]["consecutive_no_progress"], 0)
        original_round = v62_rounds._history(self.s.conn, self.s.run_id)[-1]
        self.assertEqual([(f["kind"], f["reference"]) for f in original_round["new_facts"].values()],
                         [("observed", original)])
        alias = self.measurement("alias")
        self.assertNotEqual(alias, original)
        _, repeated = self.round(3, "Inspect the public uncertainty statement")
        self.assertEqual((repeated["rounds"][-1]["outcome"], repeated["consecutive_no_progress"]),
                         ("uncounted", 0))
        self.assertEqual(v62_rounds._history(self.s.conn, self.s.run_id)[-1]["new_facts"], {})

    def test_verified_counterevidence_can_release_early_without_a_watch(self):
        self.ref = self.measurement()
        self.decide()
        self.assertIn(self.ref, self.next()["evidence_refs"])
        first = self.open(1, "read-observation", "Compare the reading with the candidate rule")
        self.open(1, "still-reading", "Check whether a second reading is available")
        self.decide(decision=draft(watches=[{"event": "goal_changed", "subject_ref": "objective"}]))
        adverse = {"kind": "counterevidence", "source_refs": [self.ref],
                   "text": "The observed reading of 7 is incompatible with the candidate threshold of 10."}
        reported = self.report(first, findings=[adverse])
        row = self.s.rows("SELECT payload_json FROM events WHERE event_id=?", reported["wake_event_id"])[0]
        self.assertEqual(json.loads(row["payload_json"])["reason"], "critical_counterevidence")
        early = self.next()
        self.assertIn(reported["report_ref"], early["evidence_refs"])
        self.assertEqual(early["research_progress"]["rounds"], [])
        self.assertEqual(len(early["research_progress"]["waiting_for"]), 1)

    def test_sealing_waits_for_the_opening_lead_to_finish_commissioning_the_round(self):
        first = self.open(1, "withdrawn", "Inspect the first question")
        mid = self.s.brief(first)["module"]["module_instance_id"]
        self.s.call(self.s.lead, "gx_cancel_module",
                    {"module_instance_id": mid, "reason": "The revised question uses a different record",
                     "operation_key": "withdraw"}, "withdraw")
        self.assertIsNone(v6_goal.ensure_task(self.s.conn, self.s.run_id))
        self.assertEqual(self.s.rows("SELECT round_id FROM research_rounds"), [])
        sibling = self.open(1, "second-question", "Inspect a different input")
        self.decide()
        self.report(sibling)
        settled = self.next()["research_progress"]["rounds"][0]
        self.assertEqual([entry["status"] for entry in settled["outcomes"]], ["CANCELLED", "COMPLETE"])
        expected = self.s.rows("SELECT expected_json FROM research_rounds")[0]
        self.assertEqual(len(json.loads(expected["expected_json"])), 2)

    def test_a_pending_operator_decision_defers_stagnation_until_the_wait_is_settled(self):
        from gx_core.context import Ctx, Principal
        from gx_core.governance import approvals
        from gx_core.p0.driver import context
        from gx_core.registry import Subject

        self.round(1, "Check the first inventory question")
        worker = self.open(2, "second-question", "Check a new coverage question")
        lead = self.decide()
        original = self.s.conn.execute("SELECT content_sha256 FROM submissions WHERE submission_id=?",
                                       (lead["submission_id"],)).fetchone()
        target = Subject(kind="submission", id=lead["submission_id"], sha256=original[0])
        request = approvals.request(context(self.s.conn), action_kind="model_switch", subject=target,
                                    run_id=self.s.run_id, request={"reason": "Review the pending follow-up"})
        self.assertEqual(request["status"], "PENDING")
        self.report(worker)
        frozen = v6_goal.ensure_task(self.s.conn, self.s.run_id)
        latest = self.s.conn.execute("SELECT c.snapshot_json FROM v6_decision_tasks t JOIN research_contexts c "
                                     "USING(context_ref) WHERE t.task_id=?", (frozen,)).fetchone()
        waiting = json.loads(latest[0])["research_progress"]
        self.assertIn({"kind": "operator", "approval_id": request["approval_id"]}, waiting["waiting_for"])
        self.assertEqual(waiting["consecutive_no_progress"], 1)
        human = Principal(subject="scripted-operator", scope="operator", source=approvals.INTERACTIVE_SOURCE)
        approvals.decide(Ctx(self.s.conn, human), approval_id=request["approval_id"], decision="DENIED",
                         expected_subject_sha256=target.sha256, note="Keep the current settings.")
        self.assertEqual(self.next()["research_progress"], waiting)
        self.decide()
        settled = self.next()["research_progress"]
        self.assertEqual((settled["phase"], settled["consecutive_no_progress"]), ("convergence_due", 2))
        self.assertEqual(settled["waiting_for"], [])

    def test_new_arrival_retires_stale_convergence_and_resolves_only_visible_grounded_gap(self):
        report, progress = self.round(1, "Identify missing information",
                                      gaps=["The independent series lacks an instrument reading."])
        self.assertEqual(progress["rounds"][0]["outcome"], "progress")
        gap = progress["open_gaps"][0]["gap_ref"]
        self.assertEqual(progress["open_gaps"][0]["report_ref"], report["report_ref"])
        measurement = self.measurement()
        with self.assertRaises(StaleRevision) as stale:
            self.decide("stale", decision=self.convergence(report["report_ref"]))
        self.assertEqual(stale.exception.details["reason"], "convergence_context_changed")
        refreshed = self.next()
        self.assertIn(measurement, refreshed["evidence_refs"])
        with self.assertRaises(ScopeDenied):
            self.decide("invent-gap", decision={**draft(evidence=[measurement]), "resolved_gaps": ["invented"]})
        with self.assertRaises(SchemaInvalid):
            self.decide("no-grounds", decision={**draft(evidence=[report["report_ref"]]), "resolved_gaps": [gap]})
        resulting = self.decide("resolve-gap", decision={**draft(evidence=[measurement]), "resolved_gaps": [gap]})
        [event] = self.s.rows("SELECT payload_json FROM events WHERE kind='v62.gap.resolved'")
        self.assertEqual(json.loads(event["payload_json"])["decision_ref"], resulting["decision_ref"])
        self.assertEqual(self.s.rows("SELECT status FROM gaps WHERE gap_id=?", gap), [{"status": "RESOLVED"}])
        self.assertEqual(self.s.rows("SELECT event_id FROM events WHERE kind='v62.convergence.entered'"), [])


if __name__ == "__main__":
    unittest.main()
