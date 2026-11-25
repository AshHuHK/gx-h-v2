"""Module report boundaries using real Run, lease, receipt, artifact and wake owners."""
import json
import tempfile
import unittest
from pathlib import Path

if __package__:
    from .interleavings_fixture import Scenario, draft
else:
    from interleavings_fixture import Scenario, draft
from gx_core.errors import IdempotencyConflict, ScopeDenied, StaleRevision
from gx_core.p0 import v6_goal, v62_reports
from gx_core.persistence import store


class ModuleReports(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.s = self.enterContext(Scenario(Path(tmp) / "study"))
        self.source = self.s.materials[0]

    def _module(self, key, *, watches=None):
        opened = self.s.open(key, inputs=[self.source], wake_on=watches)
        return self.s.bind(opened["task_id"])

    def _yield_lead(self):
        return self.s.call(self.s.lead, "gx_decide_revision", {"decision": draft()}, "yield")

    def _reopen(self):
        directory = self.s.directory
        self.s.__exit__(None, None, None)
        self.s = self.enterContext(Scenario(directory, reopen=True))

    def _read(self, actor):
        return self.s.call(actor, "gx_read_artifact", {"ref": self.source, "operation_key": "read"}, "read")

    def test_only_performed_scoped_reads_can_be_cited_and_different_contents_conflict(self):
        actor = self._module("one")
        with self.assertRaises(ScopeDenied):
            self.s.report(actor, self.source, key="unread")
        self._read(actor)
        self._yield_lead()
        report = self.s.report(actor, self.source)
        self.assertEqual(report, self.s.report(actor, self.source))
        payload = {"operation_key":"report", "status":"COMPLETE", "gaps":["changed"], "next_steps":[],
                   "findings":[], "artifact_refs":[]}
        with self.assertRaises(IdempotencyConflict):
            v62_reports.submit_report(self.s.conn, run_id=self.s.run_id, task_id=actor.task_id,
                lease_id=actor.lease_id, thread_id=actor.thread, turn_id=actor.turn, call_id="changed",
                payload=payload)
        self.assertEqual([report["report_ref"], self.source],
                         v62_reports.released_evidence_refs(self.s.conn, self.s.run_id))
        self.assertEqual([], v62_reports.released_evidence_refs(self.s.conn, self.s.run_id,
                                                                  report["wake_event_id"]-1))

    def test_watched_report_released_once_and_peer_joins_on_completion(self):
        first = self._module("one", watches=["counterevidence"])
        second = self._module("two")
        self._read(first)
        self._read(second)
        content = {"text":"The selected coordinates are incomplete.", "source_refs":[self.source], "kind":"counterevidence"}
        watched = self.s.report(first, self.source, findings=[content])
        watched_ref = watched["report_ref"]
        self.assertEqual([watched_ref], v62_reports.released_report_refs(self.s.conn, self.s.run_id))
        self._yield_lead()
        peer = self.s.report(second, self.source)
        self.assertEqual([watched_ref, peer["report_ref"]], v62_reports.released_report_refs(self.s.conn, self.s.run_id))
        self.assertEqual([watched_ref], v62_reports.released_report_refs(self.s.conn, self.s.run_id,
                                                                         watched["wake_event_id"]))
        before = self.s.rows("SELECT event_id FROM v62_module_wakes ORDER BY event_id")
        self.assertEqual(2, len(before))
        self.assertEqual(peer, self.s.report(second, self.source))
        self.assertEqual(before, self.s.rows("SELECT event_id FROM v62_module_wakes ORDER BY event_id"))

    def test_retiring_last_peer_releases_waiting_report_and_all_citation_grants_once(self):
        worker = self._module("one")
        sibling = self.s.open("two", inputs=[self.source])
        self._read(worker)
        receipt = self.s.report(worker, self.source)
        self.assertIsNone(receipt["wake_event_id"])
        self.assertEqual([], v62_reports.report_projection(self.s.conn, self.s.run_id))
        self.assertEqual([], v62_reports.released_evidence_refs(self.s.conn, self.s.run_id))
        cancel = {"module_instance_id": sibling["module_instance_id"], "reason": "End the peer question",
                  "operation_key": "cancel"}
        self.s.call(self.s.lead, "gx_cancel_module", cancel, "cancel")
        self.s.call(self.s.lead, "gx_cancel_module", cancel, "cancel")
        self.assertEqual([], v62_reports.released_report_refs(self.s.conn, self.s.run_id))
        self._yield_lead()
        self.assertEqual([receipt["report_ref"], self.source],
                         v62_reports.released_evidence_refs(self.s.conn, self.s.run_id))
        self.assertEqual(1, len(self.s.rows("SELECT event_id FROM v62_module_wakes")))

    def test_report_content_bound_is_measured_in_utf8_and_keeps_the_contrary_original(self):
        actor = self._module("long")
        self._read(actor)
        adverse = "有一條無法複核的紀錄。"
        findings = [{"text":"量測" * 4000, "kind":"observation", "source_refs":[self.source]},
                    {"text":adverse, "kind":"counterevidence", "source_refs":[self.source]}]
        self._yield_lead()
        report = self.s.report(actor, self.source, findings=findings)
        projected = v62_reports.report_projection(self.s.conn, self.s.run_id, per_report_bytes=1024,
                                                  total_bytes=1100)
        self.assertEqual(1, len(projected))
        self.assertTrue(projected[0]["truncated"])
        self.assertEqual(report["report_ref"], projected[0]["report_ref"])
        self.assertEqual(adverse, projected[0]["counterevidence"][0]["text"])
        self.assertLessEqual(v62_reports._size(projected[0]), 1024)
        self.assertEqual([report["report_ref"], self.source],
                         v62_reports.released_evidence_refs(self.s.conn, self.s.run_id))

    def test_enrollment_survives_reporting_before_second_delegation_and_restart(self):
        lead = self.s.lead
        first = self._module("one")
        self._read(first)
        one = self.s.report(first, self.source)
        self.assertIsNone(one["wake_event_id"])
        self._reopen()
        self.s.advance()
        self.assertEqual([], self.s.rows("SELECT 1 FROM v62_module_wakes"))
        self.assertEqual(lead, self.s.bind(lead.task_id))
        second = self._module("two")
        self._read(second)
        two = self.s.report(second, self.source)
        self.assertIsNone(two["wake_event_id"])
        self.assertEqual([], v62_reports.report_projection(self.s.conn, self.s.run_id))
        self._yield_lead()
        self.s.advance()
        new_lead = self.s.next_actor("run")
        with self.assertRaises(StaleRevision):
            self.s.open("late", inputs=[self.source])
        wakes = self.s.rows("SELECT e.payload_json FROM v62_module_wakes w JOIN events e USING(event_id)")
        self.assertEqual(len(wakes), 1)
        self.assertEqual(json.loads(wakes[0]["payload_json"])["report_refs"], [one["report_ref"], two["report_ref"]])
        self.assertNotEqual(lead.task_id, new_lead.task_id)
        self.assertIn(one["report_ref"], json.dumps(self.s.brief(new_lead)))
        self.assertIn(two["report_ref"], json.dumps(self.s.brief(new_lead)))

    def test_cold_driver_releases_pending_report_when_parent_was_retired(self):
        lead = self.s.lead
        first = self._module("one")
        self._read(first)
        one = self.s.report(first, self.source)
        with store.transaction(self.s.conn, transaction_owner="V62AAcceptance", operation="retire_parent"):
            v6_goal.cancel_decision(self.s.conn, self.s.run_id, lead.task_id, reason="Superseded parent snapshot")
        self._reopen()
        self.s.advance()
        self.s.advance()
        self.assertEqual([one["report_ref"]], v62_reports.released_report_refs(self.s.conn, self.s.run_id))
        self.assertEqual(1, len(self.s.rows("SELECT event_id FROM v62_module_wakes")))
