"""Witness a cancellation from its frozen comparison, GX receipt and terminal node event."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.science_execution.test_witness_version import scorer_projection
from acceptance.v623.live import action_witness
from acceptance.v622.test_close import CloseStudy
from acceptance.v623.test_decision_action import comparison_open
from acceptance.v62a.interleavings_fixture import draft
from gx_core.p0 import v6_goal, v6_jobs, v623_comparison


class WitnessCancellation(unittest.TestCase):
    def opening(self, *, reference=True):
        work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "cancel"
        s = self.enterContext(CloseStudy(work, spec_updates={"research_effort": "quick", "research_modules": [],
                           "capabilities": ["deposited-chain-geometry@2"], "plugins": ["org.gx.protein@0.3.0"]}))
        opening = comparison_open()
        opening["comparison_plan"]["reference_refs"] = [s.materials[0]]
        route, = opening["ops"]
        route["budget_limits"]["cpu_seconds"] = 60
        opening["ops"].append(s.node(s.lead, "planned", s.materials[0 if reference else 1],
                                     branch_id=route["branch"], capability="deposited-chain-geometry@2",
                                     parameters={"mask": "common_ca"}))
        first = s.call(s.lead, "gx_decide_revision", {"decision": opening}, "commission")
        target = first["aliases"]["local:planned"]
        binding = first["comparison"]
        return s, binding, target

    def cancel(self, *, reference=True):
        s, binding, target = self.opening(reference=reference)
        v6_goal.record_observation(s.conn, s.run_id, s.materials[-1], source="new-inventory")
        actor = s.next_actor("run")
        cancel = {"op": "cancel_node", "node": target, "reason": "Withdraw this planned measurement."}
        accepted = s.call(actor, "gx_decide_revision", {"decision": draft(cancel)}, "withdraw")
        row, = s.rows("SELECT request_json,receipt_json FROM v6_decisions WHERE decision_ref=?", accepted["decision_ref"])
        self.assertEqual(json.loads(row["request_json"])["decision"]["ops"], [cancel])
        self.assertEqual(json.loads(row["receipt_json"]), accepted)
        request, = s.rows("SELECT event_id,payload_json FROM events WHERE run_id=? AND kind='v6.node.cancel_requested' AND subject_id=?",
                          s.run_id, target)
        requested_op = json.loads(request["payload_json"])
        self.assertEqual(requested_op["decision_id"], accepted["decision_ref"])
        cancel_op, = accepted["resolved_ops"]
        self.assertEqual(requested_op["op_digest"], cancel_op["op_digest"])
        self.assertEqual(s.rows("SELECT status FROM v6_nodes WHERE node_id=?", target), [{"status": "CANCEL_REQUESTED"}])
        return s, binding, accepted, target, request["event_id"]

    def settle(self, s, target):
        v6_jobs.advance(s.conn, s.run_id)
        terminal, = s.rows("SELECT event_id,new_state,payload_json FROM events WHERE run_id=? AND subject_id=? AND kind='v6.node.settled'",
                           s.run_id, target)
        self.assertEqual(terminal["new_state"], "CANCELLED")
        commission, = s.rows("SELECT decision_ref FROM v6_nodes WHERE node_id=?", target)
        self.assertEqual(json.loads(terminal["payload_json"])["decision_ref"], commission["decision_ref"])
        self.assertFalse(s.rows("SELECT 1 FROM jobs"))
        return terminal["event_id"]

    def test_registered_reference_requires_terminal_cancellation(self):
        s, binding, accepted, node, requested = self.cancel()
        self.assertEqual(accepted["comparison_action"]["comparison_id"], binding["comparison_id"])
        self.assertEqual(accepted["comparison_action"]["disposition"], "CHANGE")
        from acceptance.science_execution.action_effect import verified_change
        self.assertIsNone(verified_change(s.conn, s.run_id, binding, accepted))
        terminal = self.settle(s, node)
        self.assertGreater(terminal, requested)
        effect = verified_change(s.conn, s.run_id, binding, accepted)
        self.assertIsNotNone(effect)
        self.assertEqual((effect["kind"], effect["ref"], effect["event_id"]), ("withdrawn_node", node, terminal))
        scored = self.outcome(s, binding, accepted)
        self.assertEqual(scored["effect"], effect)
        self.assertEqual(scored["enacted"], "CHANGE")
        self.assertTrue(scored["action_matches_intent"])
        self.assertFalse(scored["scientifically_interpretable"])

    def test_unrelated_input_cancellation_never_witnesses_comparison_change(self):
        s, binding, accepted, node, requested = self.cancel(reference=False)
        self.assertNotIn("comparison_action", accepted)
        self.assertGreater(self.settle(s, node), requested)
        from acceptance.science_execution.action_effect import verified_change
        self.assertIsNone(verified_change(s.conn, s.run_id, binding, accepted))

    def outcome(self, s, binding, accepted):
        trace, window = scorer_projection(s, binding, accepted, s.lead)
        opening, = s.rows("SELECT request_json FROM v6_decisions WHERE decision_ref=?", binding["decision_ref"])
        trace["initial_plan"] = json.loads(opening["request_json"])["decision"]
        window["window"] = s.lead.turn
        # Isolate the enacted action. The deciding turn received no numeric clue.
        with patch.object(action_witness, "first_window", return_value=(window, [], "NO_ELIGIBLE_DELIVERY")):
            _, frozen = action_witness.reader_packet(trace)
            from acceptance.science_execution.action_effect import outcome_packet
            return outcome_packet(s.conn, trace, {}, frozen)[1]

    def same_work_on_another_branch(self, *, preregister):
        s, binding, target = self.opening()
        alternate = comparison_open()
        route, = alternate["ops"]
        route.update(branch="local:other", hypothesis="A second registered question uses the same measurements.",
                     budget_limits={"cpu_seconds": 60, "job_submissions": 1})
        alternate["comparison_plan"].update(branch=route["branch"], reference_refs=binding["reference_refs"])
        additions = []
        if preregister:
            v6_goal.record_observation(s.conn, s.run_id, s.materials[-1], source="register-other")
            actor = s.next_actor("run")
            opened = s.call(actor, "gx_decide_revision", {"decision": alternate}, "open-other")
            other = opened["comparison"]
            self.assertNotEqual(other["comparison_id"], binding["comparison_id"])
            self.assertEqual(other["method"], binding["method"])
            self.assertEqual(other["reference_refs"], binding["reference_refs"])
            branch_id = other["branch"]
        else:
            additions.append(route)
            branch_id = route["branch"]
        v6_goal.record_observation(s.conn, s.run_id, s.materials[0], source="withdraw-target")
        actor = s.next_actor("run")
        method = binding["method"]
        repeat = s.node(actor, "another-measurement", s.materials[0], branch_id=branch_id,
                        capability=method["capability"], parameters=method["parameters"])
        cancel = {"op": "cancel_node", "node": target, "reason": "Withdraw the original measurement."}
        decision = dict(draft(cancel, *additions, repeat), comparison_id=binding["comparison_id"])
        receipt = s.call(actor, "gx_decide_revision", {"decision": decision}, "change-original")
        self.assertEqual(receipt["comparison_action"]["comparison_id"], binding["comparison_id"])
        self.assertEqual(receipt["comparison_action"]["disposition"], "CHANGE")
        completed = s.settle(receipt["aliases"]["local:another-measurement"])
        self.assertEqual(completed["status"], "SUCCEEDED")
        self.assertNotEqual(completed["branch_id"], binding["branch"])
        comparisons = v623_comparison.bindings(s.conn, s.run_id, binding["goal_version"], receipt["event_id"] - 1)
        independently_registered = [c for c in comparisons if c["branch"] == completed["branch_id"]]
        self.assertEqual([c["comparison_id"] for c in independently_registered],
                         [other["comparison_id"]] if preregister else [])
        self.assertEqual(s.rows("SELECT job_id FROM jobs"), [{"job_id": completed["job_id"]}])
        terminal, = s.rows("SELECT event_id,new_state FROM events WHERE run_id=? AND subject_id=? "
                           "AND kind='v6.node.settled'", s.run_id, target)
        self.assertEqual(terminal["new_state"], "CANCELLED")
        return self.outcome(s, binding, receipt), target, terminal["event_id"]

    def test_independently_registered_parallel_comparison_does_not_override_cancellation(self):
        score, target, terminal = self.same_work_on_another_branch(preregister=True)
        self.assertEqual(score["enacted"], "CHANGE")
        self.assertTrue(score["action_matches_intent"])
        effect = score["effect"]
        self.assertEqual((effect["kind"], effect["ref"], effect["event_id"]), ("withdrawn_node", target, terminal))

    def test_recommissioning_same_work_on_new_unregistered_branch_does_not_earn_change(self):
        score, _, _ = self.same_work_on_another_branch(preregister=False)
        self.assertIn(score["enacted"], ("OLD_COMPARISON_CONTINUES", "MAINTAIN"))
        self.assertFalse(score["action_matches_intent"])
