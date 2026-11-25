"""Public PI amendment, immutable historical rounds and a late prior-goal report."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import PRODUCT, Scenario, draft
from gx_core.errors import ScopeDenied
from gx_core.p0 import v6_goal, v62_rounds


class GoalVersions(unittest.TestCase):
    def cli(self, s, verb, *args):
        process = subprocess.run([sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), verb,
                                  "--state", str(s.state), "--run-id", s.run_id, *args],
                                 env={**os.environ, "GX_CODEX_OFFLINE": "1"},
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def change_goal(self, s):
        def send(name, body):
            path = s.directory / (name + ".json")
            path.write_text(json.dumps(body))
            return self.cli(s, "message", "--message-id", name, "--action", str(path))["result"]
        preview = send("replace-objective", {"kind": "change", "items": [
            {"item_id": "objective", "item_type": "objective", "expected_version": 1,
             "value": "Which recorded sources can support a second independent question?"}]})
        choice = {"kind": "confirm", "change_id": preview["change_id"], "preview_sha256": preview["preview_sha256"]}
        if preview.get("required_confirmation"):
            choice["acknowledge_consequences"] = preview["required_confirmation"]["acknowledge_consequences"]
        self.assertEqual(send("approve-objective", choice)["status"], "committed")

    def jobless_module(self, s, key, number=1):
        opened = s.open(key, round=number, inputs=[s.materials[-1]], caps=[])
        actor = s.bind(opened["task_id"])
        s.call(actor, "gx_read_artifact", {"ref": s.materials[-1], "operation_key": "read"}, "read")
        return actor

    def test_converged_goal_can_be_replaced_and_round_one_can_be_reused(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "study"
        with Scenario(work) as s:
            actor = self.jobless_module(s, "inventory-A")
            s.call(s.lead, "gx_decide_revision", {"decision": draft()}, "seal-A")
            report_a = s.report(actor, s.materials[-1], gaps=["Which measurement is missing for A?"])
            s.next_actor("run")
            old_gap, = s.brief(s.lead)["research_progress"]["open_gaps"]
            decision = {**draft(evidence=[report_a["report_ref"]]),
                        "convergence": {"spec_version": "rule/1", "candidates": ["case-A"]}}
            convergence = s.call(s.lead, "gx_decide_revision", {"decision": decision}, "converge-A")
            before = self.cli(s, "status")
            self.assertEqual(before["research_progress"]["phase"], "converging")
            balances = s.rows("SELECT budget_ref,limits_json,spent_json FROM budgets ORDER BY budget_ref")
            old_report = s.rows("SELECT report_ref,receipt_json FROM v62_module_reports")
            self.change_goal(s)
            after = self.cli(s, "status")
            self.assertEqual(after["goal_version"], 2)
            self.assertEqual(after["research_progress"]["phase"], "exploring")
            self.assertEqual(after["research_progress"]["rounds"], [])
            self.assertEqual(after["research_progress"]["consecutive_no_progress"], 0)
            self.assertEqual(s.rows("SELECT budget_ref,limits_json,spent_json FROM budgets ORDER BY budget_ref"), balances)
        with Scenario(work, reopen=True) as s:
            s.next_actor("run")
            frozen = s.brief(s.lead)
            self.assertEqual((frozen["goal_version"], frozen["previous_decision"]), (2, None))
            self.assertNotIn(report_a["report_ref"], [r["report_ref"] for r in frozen["module_reports"]])
            self.assertEqual(frozen["research_progress"]["open_gaps"], [])
            worker_b = self.jobless_module(s, "inventory-B")
            s.call(s.lead, "gx_decide_revision", {"decision": draft()}, "seal-B")
            report_b = s.report(worker_b, s.materials[-1])
            s.next_actor("run")
            brief_b = s.brief(s.lead)
            self.assertIn(report_b["report_ref"], brief_b["evidence_refs"])
            scoped = dict(draft(evidence=[report_b["report_ref"]]), resolved_gaps=[old_gap["gap_ref"]])
            with self.assertRaises(ScopeDenied) as rejected:
                s.call(s.lead, "gx_decide_revision", {"decision": scoped}, "resolve-old-gap")
            self.assertIn("frozen", str(rejected.exception))
            self.assertEqual(s.rows("SELECT status FROM gaps WHERE gap_id=?", old_gap["gap_ref"]),
                             [{"status": "OPEN"}])
            current = brief_b["research_progress"]
            self.assertEqual([r["round"] for r in current["rounds"]], [1])
            self.assertEqual(current["rounds"][0]["outcomes"][0]["report_ref"], report_b["report_ref"])
            self.assertEqual(s.rows("SELECT report_ref,receipt_json FROM v62_module_reports ORDER BY rowid LIMIT 1"), old_report)
            history = v62_rounds._history(s.conn, s.run_id, goal_version=1)
            self.assertEqual(history[0]["outcomes"][0]["report_ref"], report_a["report_ref"])
            events = s.rows("SELECT kind,event_id,payload_json FROM events WHERE kind IN "
                            "('v62.round.settled','v62.convergence.entered','v62.module.wake') ORDER BY event_id")
            wake_versions = [json.loads(e["payload_json"])["goal_version"] for e in events if e["kind"] == "v62.module.wake"]
            self.assertEqual(wake_versions, [1, 2])
            self.assertEqual(convergence["convergence"]["goal_version"], 1)
            if os.getenv("GX621_GOAL_TRACE"):
                entries = [{**e, "payload": json.loads(e["payload_json"])} for e in events]
                for e in entries:
                    del e["payload_json"]
                Path(os.environ["GX621_GOAL_TRACE"]).write_text(json.dumps(dict(run_id=s.run_id, events=entries,
                    old_report=report_a, new_report=report_b, cold_open=True, unchanged_budget=True,
                    old_gap=old_gap["gap_ref"], old_gap_resolution_refused=rejected.exception.code,
                    paid_model_calls=0, provider_tokens=None), indent=2) + "\n")

    def test_late_old_round_and_new_round_can_complete_in_reverse_delivery_order(self):
        from gx_core.errors import GxError
        from gx_core.p0 import v62_modules
        work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "study"
        with Scenario(work) as s:
            finished = self.jobless_module(s, "initial")
            withdrawn = self.jobless_module(s, "pending")
            old = s.report(finished, s.materials[-1])
            self.assertIsNone(old["wake_event_id"])
            self.change_goal(s)
        with Scenario(work, reopen=True) as s:
            s.next_actor("run")
            worker = self.jobless_module(s, "new-with-old-undelivered")
            s.call(s.lead, "gx_decide_revision", {"decision": draft(
                watches=[{"event": "module_report", "subject_ref": "run"}])}, "wait-for-new-report")
            v62_modules.advance(s.conn, s.run_id)
            v62_modules.advance(s.conn, s.run_id)
            # GX released A's committed report once after the restart, while B has
            # already commissioned its own first round and is waiting for its worker.
            wakes = s.rows("SELECT w.wake_key,e.payload_json FROM v62_module_wakes w JOIN events e USING(event_id)")
            self.assertEqual(len(wakes), 1)
            self.assertEqual(json.loads(wakes[0]["payload_json"])["goal_version"], 1)
            self.assertEqual(json.loads(wakes[0]["payload_json"])["report_refs"], [old["report_ref"]])
            self.assertIsNone(v6_goal.ensure_task(s.conn, s.run_id))
            self.assertEqual(v62_rounds.brief(s.conn, s.run_id, None)["research_progress"]["rounds"], [])
            with self.assertRaises(GxError):
                s.report(withdrawn, s.materials[-1], "forbidden-after-PI-change")
            newest = s.report(worker, s.materials[-1])
            s.next_actor("run")
            current = s.brief(s.lead)
            self.assertEqual([r["report_ref"] for r in current["module_reports"]], [newest["report_ref"]])
            self.assertEqual(current["research_progress"]["consecutive_no_progress"], 1)
            self.assertEqual(len(current["research_progress"]["rounds"]), 1)
            closure = draft({"op": "close_run", "reason": "The current assignment was delivered.",
                             "claim_refs": [], "unresolved": ["Independent measurements remain unavailable."]},
                            evidence=[newest["report_ref"]])
            self.assertEqual(s.call(s.lead, "gx_decide_revision", {"decision": closure}, "close-new")["goal_version"], 2)
