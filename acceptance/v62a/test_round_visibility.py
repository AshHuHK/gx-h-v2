"""Lead visibility at unrelated wakes and out-of-order round completion."""
import json
from pathlib import Path
import tempfile
import unittest

if __package__:
    from .interleavings_fixture import Scenario, draft
else:
    from interleavings_fixture import Scenario, draft
from gx_core.errors import IdempotencyConflict, StaleRevision
from gx_core.p0 import v62_reports, v6_goal, v6_native


class RoundVisibility(unittest.TestCase):
    def scenario(self, **spec):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        return self.enterContext(Scenario(Path(tmp) / "public", spec_updates=spec))

    def test_unrelated_wake_hides_module_authored_node_content(self):
        s = self.scenario()
        opened = s.open("first")
        s.open("peer")
        actor = s.bind(opened["task_id"])
        s.call(s.lead, v6_native.DECIDE, {"decision": draft()}, "finish-opening")
        marker = "PUBLIC-UNRELEASED-OBSERVATION"
        op = s.node(actor, "measurement")
        op["question"] = f"The inventory produced {marker}. Does it recur?"
        s.call(actor, v6_native.DECIDE,
               {"decision": draft(op, watches=[{"event": "job_settled", "subject_ref": "local:measurement"}])},
               "stage-measurement")
        v6_goal.record_observation(s.conn, s.run_id, s.materials[-1], source="public-run-observation")
        lead = s.next_actor("run")
        brief = s.brief(lead)
        delivered, = s.conn.execute("SELECT brief FROM p0_roots WHERE lease_id=?", (lead.lease_id,)).fetchone()
        self.assertEqual([], v62_reports.released_report_refs(s.conn, s.run_id))
        self.assertNotIn(marker, json.dumps(brief))
        self.assertNotIn(marker, delivered)
        self.assertEqual(1, len(brief["nodes"]))
        [node] = brief["nodes"]
        self.assertNotIn("commission", node)
        self.assertNotIn("exec_key", node)
        self.assertNotIn("wait", node)
        self.assertFalse(node.get("result_ref"))
        stored, = s.conn.execute("SELECT spec_json FROM v6_nodes WHERE node_id=?", (node["node_id"],)).fetchone()
        self.assertEqual(op["question"], json.loads(stored)["question"])
        branch = next(b for b in brief["branches"] if b["branch_id"] == node["branch_id"])
        self.assertEqual({"module_instance_id": opened["module_instance_id"]}, branch["spec"])

    def test_overlapping_rounds_release_at_distinct_cursors_and_cannot_reopen(self):
        s = self.scenario()
        src = s.materials[0]
        actors = [s.bind(s.open(key, inputs=[src], round=n)["task_id"])
                  for key, n in (("first", 1), ("peer", 1), ("second-round", 2))]
        with self.assertRaises(StaleRevision):
            s.open("late-old-round", round=1, inputs=[src])
        s.call(s.lead, v6_native.DECIDE, {"decision": draft()}, "yield-round-membership")
        for actor in actors:
            s.call(actor, "gx_read_artifact", {"ref": src, "operation_key": "read"}, "read")
        first, peer, newer = actors
        early = s.report(first, src)
        self.assertIsNone(early["wake_event_id"])
        late = s.report(newer, src)
        rest = s.report(peer, src)
        self.assertEqual([late["report_ref"]],
                         v62_reports.released_report_refs(s.conn, s.run_id, late["wake_event_id"]))
        self.assertEqual({r["report_ref"] for r in (early, late, rest)},
                         set(v62_reports.released_report_refs(s.conn, s.run_id, rest["wake_event_id"])))
        with self.assertRaises(IdempotencyConflict):
            s.report(first, src, key="replace-first")
        self.assertEqual(early, s.report(first, src))
        self.assertEqual(["round:2", "round:1"], [r["wake_key"] for r in s.rows(
            "SELECT wake_key FROM v62_module_wakes WHERE run_id=? ORDER BY event_id", s.run_id)])

    def test_aggregate_limit_preserves_earlier_adverse_result(self):
        s = self.scenario(research_effort="deep", module_policy={"report_bytes": 512, "brief_bytes": 4096})
        src = s.materials[0]
        adverse = "Public counterevidence."
        limits = {"api_calls": 2, "tokens": 1000, "job_submissions": 0, "cpu_seconds": 0, "worker_tasks": 1}
        for number in range(1, 4):
            actors = [s.bind(s.open(f"public-{number}-{i}", inputs=[src], round=number, limits=limits)["task_id"])
                      for i in range(4)]
            for i, actor in enumerate(actors):
                s.call(actor, "gx_read_artifact", {"ref": src, "operation_key": "read"}, "read")
                contrary = number == 1 and i == 0
                finding = {"kind": "counterevidence" if contrary else "observation",
                           "text": adverse if contrary else "Routine inventory.", "source_refs": [src]}
                receipt = s.report(actor, src, findings=[finding])
                if contrary:
                    adverse_ref = receipt["report_ref"]
                s.roots.delivery.observe("turn/completed", {"threadId": actor.thread,
                    "turn": {"id": actor.turn, "status": "completed", "items": []}})
        s.call(s.lead, v6_native.DECIDE, {"decision": draft()}, "yield-round-membership")
        lead = s.next_actor("run")
        full = s.brief(lead)
        delivered, = s.conn.execute("SELECT brief FROM p0_roots WHERE lease_id=?", (lead.lease_id,)).fetchone()
        self.assertLessEqual(len(delivered.encode("utf-8")), 4096)
        released = set(v62_reports.released_report_refs(s.conn, s.run_id))
        self.assertEqual(12, len(released))
        index = full["module_report_index"]
        self.assertEqual(released, {r["report_ref"] for r in index})
        self.assertEqual({"COMPLETE"}, {r["status"] for r in index})
        archived = s.call(lead, "gx_read_artifact", {"ref": full["full_brief_ref"]}, "read-full-brief")
        self.assertEqual(index, archived["module_report_index"])
        [contrary] = [r for r in index if r.get("counterevidence")]
        self.assertEqual(adverse_ref, contrary["report_ref"])
        self.assertEqual([{"text": adverse, "source_refs": [src], "evidence_status": "UNVERIFIED"}],
                         contrary["counterevidence"])
        self.assertIn(adverse_ref, full["evidence_refs"])
        # A Lead should learn the sole contrary observation even when it came in the oldest report.
        with self.subTest(packet="full"):
            self.assertTrue(adverse in json.dumps(full), "The frozen full brief dropped the contrary finding")
        with self.subTest(packet="native"):
            self.assertTrue(adverse in delivered, "The delivered compact brief dropped the contrary finding")
            self.assertTrue(adverse_ref in delivered, "The counterevidence must include a path to its full report")

    def test_founder_cannot_close_over_an_unseen_published_report(self):
        for watched, attempted_close in ((False, True), (True, True), (False, False)):
            with self.subTest(watched=watched, attempted_close=attempted_close):
                s = self.scenario()
                src = s.materials[0]
                actor = s.bind(s.open("module", inputs=[src], wake_on=["counterevidence"] if watched else [])["task_id"])
                s.call(actor, "gx_read_artifact", {"ref": src, "operation_key": "read"}, "read")
                receipt = s.report(actor, src, findings=[
                    {"kind": "counterevidence", "text": "The public records disagree.", "source_refs": [src]}])
                self.assertEqual(watched, receipt["wake_event_id"] is not None)
                self.assertEqual([], s.brief(s.lead)["module_reports"])
                close = {"op": "close_run", "reason": "Archive this inventory.", "claim_refs": [],
                         "unresolved": ["No inference beyond the supplied records."]}
                founder = s.lead
                if attempted_close:
                    with self.assertRaises(StaleRevision):
                        s.call(founder, v6_native.DECIDE, {"decision": draft(close)}, "stale-close")
                    self.assertEqual("CANCELLED", s.conn.execute(
                        "SELECT status FROM tasks WHERE task_id=?", (founder.task_id,)).fetchone()[0])
                    self.assertFalse(s.rows("SELECT decision_ref FROM v6_decisions WHERE task_id=?", founder.task_id))
                else:
                    s.call(founder, v6_native.DECIDE, {"decision": draft()}, "yield-for-report")
                self.assertEqual("OPEN", s.conn.execute("SELECT status FROM v6_goals").fetchone()[0])
                fresh = s.next_actor("run")
                self.assertNotEqual(founder.task_id, fresh.task_id)
                self.assertIn(receipt["report_ref"], s.brief(fresh)["evidence_refs"])
                adopted = s.call(fresh, v6_native.DECIDE,
                                 {"decision": draft(close, evidence=[receipt["report_ref"]])}, "informed-close")
                self.assertEqual("adopted", adopted["outcome"])
                s.advance()
                self.assertEqual("CLOSED", s.conn.execute("SELECT status FROM v6_goals").fetchone()[0])
                self.assertEqual([receipt["report_ref"]], v62_reports.released_report_refs(s.conn, s.run_id))
