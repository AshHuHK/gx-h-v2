"""Report format repair on the native terminal callback, including cold redelivery."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from acceptance.v621.test_verified_metrics import METHOD, SOURCE_METADATA
from acceptance.v62a.interleavings_fixture import Scenario, draft
from gx_core.artifacts import ArtifactService
from gx_core.errors import ScopeDenied
from gx_core.hostobs.app_server_client import NO_TOOL_RESPONSE, ReceivedMessage
from gx_core.p0 import v6_goal, v6_native, v62_native


def send(s, actor, key, args):
    params = {"threadId": actor.thread, "turnId": actor.turn, "callId": key,
              "tool": v62_native.REPORT, "namespace": "gx", "arguments": args}
    response = s.roots._callback(ReceivedMessage(1, {"id": 1, "method": "item/tool/call", "params": params}))
    if response is NO_TOOL_RESPONSE:
        return None
    return json.loads(response["contentItems"][0]["text"])


def trace(record, *, partial=False):
    if name := os.getenv("GX_R4_REPAIR_TRACE"):
        path = Path(name)
        if partial:
            path = path.with_name(path.stem + "-partial" + path.suffix)
        path.write_text(json.dumps(record, indent=2) + "\n")


class ReportRetries(unittest.TestCase):
    def study(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "study"
        s = Scenario(self.path, spec_updates={"research_effort": "deep", "research_modules": ["data_inventory@2"],
                                             "plugins": ["org.gx.protein@0.3.0"], "capabilities": [METHOD]})
        with s:
            raw = s.materials[0]
            v6_goal.record_observation(s.conn, s.run_id, raw, source="public-archive",
                                       acquisition=json.loads(SOURCE_METADATA.read_bytes()))
            opened = s.call(s.lead, "gx_open_module", {**s.open_args("inventory", inputs=[raw], caps=[METHOD]),
                                                      "module_version": "2"}, "commission")
            worker = s.bind(opened["task_id"])
            s.call(s.lead, v6_native.DECIDE, {"decision": draft()}, "seal")
            created = s.submit_node(worker, "coverage", input_ref=raw, capability=METHOD,
                                    parameters={"mask": "ordered_backbone"})
            measured = s.settle(created["aliases"]["local:coverage"])
            return opened["scope"], measured["result_ref"]

    @staticmethod
    def jobs_and_charges(s):
        job_ledger = s.rows("""SELECT j.job_id,j.status,r.reservation_ref,r.status AS settlement,e.event_id,
                                    e.payload_json AS settlement_receipt FROM jobs j
                             JOIN budget_reservations r USING(reservation_ref)
                             LEFT JOIN events e ON e.kind='budget.settled'
                                 AND json_extract(e.payload_json,'$.reservation_ref')=r.reservation_ref
                             ORDER BY j.job_id,e.event_id""")
        # The delegated funding envelope legitimately settles when the module finishes.
        other = s.rows("SELECT reservation_ref,status,amounts_json FROM budget_reservations "
                       "WHERE subject_kind!='research_module' ORDER BY reservation_ref")
        return job_ledger, other

    @staticmethod
    def good(ref):
        return {"operation_key": "result", "status": "COMPLETE", "findings": [
            {"text": "Archive coverage {{metric:coverage}}.", "source_refs": [ref]}],
            "artifact_refs": [ref], "gaps": [], "next_steps": []}

    def test_limit_survives_reopen_and_next_attempt_reuses_job(self):
        scope, ref = self.study()
        valid = self.good(ref)
        invalid = {**valid, "findings": [{"text": "Archive coverage {{metric:undeclared}}.", "source_refs": [ref]}]}
        with Scenario(self.path, reopen=True) as s:
            worker = s.next_actor(scope)
            charges = self.jobs_and_charges(s)
            self.assertEqual(len(charges[0]), 1)
            self.assertIsNotNone(charges[0][0]["event_id"])
            refused = send(s, worker, "repair", invalid)
            duplicate = send(s, worker, "repair", invalid)
            shadow = send(s, worker, "repair", valid)
            count = s.conn.execute("SELECT count(*) FROM p0_product_repairs").fetchone()[0]
            record = {"run_id": s.run_id, "refused": refused, "duplicate": duplicate, "changed_payload": shadow,
                      "count": count, "settlements_before": charges[0], "paid_calls": 0}
            trace(record)
            self.assertEqual(refused.get("failure_count"), 1, refused)
            self.assertEqual(refused["category"], "module_report_structure")
            self.assertEqual(refused, duplicate)
            self.assertIn("conflicting", shadow["error"].lower())
            with self.assertRaisesRegex(ScopeDenied, "Conflicting"):
                s.call(worker, v6_native.DECIDE, {"decision": draft()}, "repair")
            self.assertEqual(count, 1)
            self.assertEqual(charges, self.jobs_and_charges(s))
        with Scenario(self.path, reopen=True) as s:
            active = s.next_actor(scope)
            self.assertEqual(active, worker)
            self.assertEqual(send(s, active, "repair", invalid), refused)
            self.assertIsNone(send(s, active, "limit", invalid))
            self.assertIsNone(send(s, active, "limit", invalid))
            self.assertEqual(s.conn.execute("SELECT count(*) FROM p0_product_repairs").fetchone()[0], 2)
            self.assertEqual(charges, self.jobs_and_charges(s))
        with Scenario(self.path, reopen=True) as s:
            self.assertIsNone(send(s, worker, "limit", invalid))
            successor = s.next_actor(scope)
            self.assertEqual(successor.task_id, worker.task_id)
            self.assertNotEqual(successor.lease_id, worker.lease_id)
            self.assertIsNone(send(s, successor, "valid", valid))
            self.assertIsNone(send(s, successor, "valid", valid))
            row, = s.rows("SELECT receipt_json FROM v62_module_reports")
            result = json.loads(row["receipt_json"])
            self.assertEqual(result["status"], "COMPLETE")
            self.assertIn(ref, result["source_refs"])
            self.assertEqual(charges, self.jobs_and_charges(s))
            failures = s.rows("SELECT thread_id,turn_id,call_id,response_json FROM p0_product_repairs ORDER BY rowid")
            self.assertEqual(len(failures), 2)
            trace({**record, "failures_after_replay": failures, "successor_lease": successor.lease_id,
                   "original_lease": worker.lease_id, "completed_report": result,
                   "settlements_after": self.jobs_and_charges(s)[0]})

    def test_exhausted_attempts_hand_back_partial_evidence(self):
        scope, ref = self.study()
        invalid = {**self.good(ref), "status":"INVALID"}
        with Scenario(self.path, reopen=True) as s:
            worker = s.next_actor(scope)
            charges = self.jobs_and_charges(s)
            attempts, = s.conn.execute("SELECT max_attempts FROM tasks WHERE task_id=?", (worker.task_id,)).fetchone()
            for n in range(attempts):
                if n:
                    worker = s.next_actor(scope)
                # A reference outside the granted scientific sources consumes no repair.
                foreign = send(s, worker, "outside", self.good("unread-reference"))
                self.assertIn("error", foreign)
                first = send(s, worker, "malformed", invalid)
                self.assertEqual(first.get("failure_count"), 1, first)
                self.assertIsNone(send(s, worker, "last", invalid))
        with Scenario(self.path, reopen=True) as s:
            s.advance()
            s.advance()
            row, = s.rows("SELECT receipt_json FROM v62_module_reports")
            receipt = json.loads(row["receipt_json"])
            artifact = ArtifactService(s.conn).read_json(receipt["report_ref"])
            self.assertEqual(receipt["status"], "PARTIAL")
            self.assertIn(ref, receipt["source_refs"])
            self.assertIn("report", str(artifact["gaps"]).lower())
            self.assertEqual(s.conn.execute("SELECT count(*) FROM p0_product_repairs").fetchone()[0], 2 * attempts)
            self.assertEqual(charges, self.jobs_and_charges(s))
            terminals = s.rows("SELECT thread_id,turn_id FROM p0_root_terminals WHERE reason='GX_REPAIR_LIMIT'")
            self.assertEqual(len(terminals), attempts)
            trace({"run_id": s.run_id, "attempts": attempts, "terminal_turns": terminals, "report": receipt,
                   "gaps": artifact["gaps"], "settlements_before": charges[0],
                   "settlements_after": self.jobs_and_charges(s)[0], "paid_calls": 0}, partial=True)

    def test_historical_run_keeps_unbounded_callback_receipts(self):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "history"
        with Scenario(path, spec_updates={"evidence_policy": "gx.evidence/6.2.1", "historical_replay": True}) as s:
            opened = s.open("historical")
            worker = s.bind(opened["task_id"])
            for n in range(3):
                refused = send(s, worker, str(n), {"status": "INVALID"})
                self.assertIn("SCHEMA_INVALID", refused["error"])
                self.assertNotIn("failure_count", refused)
            self.assertEqual(s.conn.execute("SELECT count(*) FROM p0_product_repairs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
