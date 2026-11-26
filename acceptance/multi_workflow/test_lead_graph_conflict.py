"""A Lead that encounters a changed route can reconsider it from the current graph."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v62a.interleavings_fixture import Scenario, branch, draft
from gx_core.errors import ScopeDenied, StaleRevision
from gx_core.p0 import v6_decisions, v6_goal


DECIDE = "gx_decide_revision"


class LeadGraphConflict(unittest.TestCase):
    def workspace(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory())) / "study"

    def open_route(self, s, label, material):
        grant = s.open(label, inputs=[material])
        worker = s.bind(grant["task_id"])
        committed = s.submit_node(worker, label, material)
        return grant["branch_id"], committed["aliases"]["local:" + label]

    def stop(self, route):
        return {"op": "stop_branch", "branch": route, "reason": "Redirect the investigation to the alternative."}

    def leads(self, s):
        return s.rows("SELECT d.task_id,t.status,d.context_ref,d.base_json FROM v6_decision_tasks d "
                      "JOIN tasks t USING(task_id) WHERE d.run_id=? AND d.scope='run' ORDER BY d.rowid", s.run_id)

    def node(self, s, node_id):
        row, = s.rows("SELECT node_id,branch_id,decision_ref,operation_key,status FROM v6_nodes WHERE node_id=?",
                      node_id)
        return row

    def test_changed_route_gets_one_fresh_lead_and_replays_across_restart(self):
        path = self.workspace()
        with Scenario(path) as s:
            old = s.lead
            target, withdrawn = self.open_route(s, "target", s.materials[0])
            sibling, retained = self.open_route(s, "sibling", s.materials[0])
            sibling_before = self.node(s, retained)
            current = s.head()
            self.assertNotEqual(s.brief(old)["head"], current)
            request = {"decision": draft(self.stop(target), branch("replacement"))}
            with self.assertRaises(StaleRevision) as refusal:
                s.call(old, DECIDE, request, "reroute")
            self.assertEqual(refusal.exception.details["reason"], "lead_graph_conflict")
            self.assertIn(target, refusal.exception.details["changed_subjects"])
            original_refusal = refusal.exception.to_payload()
            v6_goal.ensure_task(s.conn, s.run_id)
            by_task = {r["task_id"]: r for r in self.leads(s)}
            self.assertEqual("CANCELLED", by_task[old.task_id]["status"])
            replacement, = [r for r in by_task.values() if r["status"] == "READY"]
            self.assertNotEqual(replacement["context_ref"], by_task[old.task_id]["context_ref"])
            self.assertEqual(current, s.head())
            self.assertEqual(self.node(s, withdrawn)["status"], "PENDING")

        with Scenario(path, reopen=True) as cold:
            # The same rejected turn and call can be redelivered after its lease has been retired.
            with self.assertRaises(StaleRevision) as replay:
                cold.call(old, DECIDE, request, "reroute")
            self.assertEqual(replay.exception.to_payload(), original_refusal)
            with self.assertRaises((ScopeDenied, StaleRevision)):
                cold.call(old, DECIDE, request, "different-call-on-retired-turn")
            v6_goal.ensure_task(cold.conn, cold.run_id)
            self.assertEqual(by_task, {r["task_id"]: r for r in self.leads(cold)})
            fresh = cold.next_actor("run")
            self.assertEqual(replacement["task_id"], fresh.task_id)
            self.assertEqual(current, cold.brief(fresh)["head"])
            self.assertEqual({n["node_id"] for n in cold.brief(fresh)["nodes"]}, {withdrawn, retained})

            changed = cold.call(fresh, DECIDE, request, "apply-from-new-graph")
            self.assertEqual(changed["result_head"]["version"], current["version"] + 1)
            self.assertEqual("CANCEL_REQUESTED", self.node(cold, withdrawn)["status"])
            self.assertEqual(sibling_before, self.node(cold, retained))
            branches = {r["branch_id"]:r["status"] for r in cold.rows("SELECT branch_id,status FROM v6_branches")}
            # The graph records the stop immediately; pending node cancellation still needs settlement.
            saved, = cold.rows("SELECT graph_json FROM v6_graph_versions WHERE sha256=?", changed["result_head"]["sha256"])
            self.assertEqual(json.loads(saved["graph_json"])["branches"][target]["state"], "STOPPED")
            self.assertEqual(branches[target], "STOPPING")
            self.assertEqual(branches[sibling], "OPEN")
            self.assertEqual(branches[changed["aliases"]["local:replacement"]], "OPEN")
            self.assertEqual([], cold.rows("SELECT decision_ref FROM v6_decisions WHERE task_id=?", old.task_id))

        with Scenario(path, reopen=True) as recovered:
            self.assertEqual(changed, recovered.call(fresh, DECIDE, request, "apply-from-new-graph"))
            self.assertEqual(changed["result_head"], recovered.head())
            self.assertEqual(len(self.leads(recovered)), 2)
            self.assertEqual(sibling_before, self.node(recovered, retained))
            self.assertEqual(len(recovered.rows("SELECT * FROM v6_decisions WHERE task_id=?", fresh.task_id)), 1)

    def test_cancel_and_refusal_are_atomic_if_the_native_callback_dies(self):
        path = self.workspace()
        call_id = "stop-before-disconnect"
        with Scenario(path) as s:
            old = s.lead
            target, _ = self.open_route(s, "target", s.materials[0])
            request = {"decision": draft(self.stop(target))}
            reconsider = v6_decisions.reconsider_module_conflict

            def disconnect_after_cancellation(conn, identity, error):
                reconsider(conn, identity, error)
                state, = conn.execute("SELECT status FROM tasks WHERE task_id=?", (old.task_id,)).fetchone()
                self.assertEqual(state, "CANCELLED")
                raise RuntimeError("local callback disconnected before commit")

            with patch.object(v6_decisions, "reconsider_module_conflict", disconnect_after_cancellation):
                with self.assertRaisesRegex(RuntimeError, "disconnected before commit"):
                    s.call(old, DECIDE, request, call_id)

        def counts(s):
            wake_count = len(s.rows("SELECT event_id FROM events WHERE run_id=? AND subject_id=? "
                                    "AND kind='v6.decision.superseded'", s.run_id, old.task_id))
            refusals = len(s.rows("SELECT result_json FROM p0_root_calls WHERE thread_id=? AND call_id=?",
                                  old.thread, call_id))
            native_calls = len(s.rows("SELECT call_id FROM p0_v6_native_calls WHERE thread_id=? AND call_id=?",
                                      old.thread, call_id))
            return wake_count, refusals, native_calls

        with Scenario(path, reopen=True) as cold:
            self.assertEqual([(old.task_id, "CLAIMED")], [(r["task_id"], r["status"]) for r in self.leads(cold)])
            self.assertEqual(counts(cold), (0, 0, 0))
            # Restore the recorded, still-active native turn at the local transport seam.
            delivery, = cold.rows("SELECT turn_id FROM p0_root_deliveries WHERE thread_id=? AND state='acknowledged' "
                                  "AND method='turn/start'", old.thread)
            self.assertEqual(delivery["turn_id"], old.turn)
            cold.roots.active_turns[old.thread] = delivery["turn_id"]
            with self.assertRaises(StaleRevision) as refusal:
                cold.call(old, DECIDE, request, call_id)
            self.assertEqual(refusal.exception.details["reason"], "lead_graph_conflict")
            original = refusal.exception.to_payload()
            v6_goal.ensure_task(cold.conn, cold.run_id)
            tasks = self.leads(cold)
            self.assertEqual(["CANCELLED", "READY"], [r["status"] for r in tasks])
            self.assertEqual(counts(cold), (1, 1, 1))

        with Scenario(path, reopen=True) as restarted:
            with self.assertRaises(StaleRevision) as replay:
                restarted.call(old, DECIDE, request, call_id)
            self.assertEqual(replay.exception.to_payload(), original)
            v6_goal.ensure_task(restarted.conn, restarted.run_id)
            self.assertEqual(self.leads(restarted), tasks)
            self.assertEqual(counts(restarted), (1, 1, 1))

    def test_unrelated_lead_decision_still_succeeds_and_a_later_observation_can_wake_it(self):
        with Scenario(self.workspace()) as s:
            original = s.lead
            target, node_id = self.open_route(s, "target", s.materials[0])
            changed_graph = s.head()
            wait = {"decision": draft(evidence=[s.materials[0]])}
            accepted = s.call(original, DECIDE, wait, "wait-for-observation")
            self.assertEqual(accepted["result_head"], changed_graph)
            self.assertEqual(len(self.leads(s)), 1)
            self.assertEqual(accepted, s.call(original, DECIDE, wait, "wait-for-observation"))

            v6_goal.record_observation(s.conn, s.run_id, s.materials[0], source="new-observation")
            fresh = s.next_actor("run")
            self.assertNotEqual(fresh.task_id, original.task_id)
            self.assertEqual(s.brief(fresh)["head"], changed_graph)
            s.call(fresh, DECIDE, {"decision": draft(self.stop(target))}, "stop-after-observation")
            self.assertEqual(self.node(s, node_id)["status"], "CANCEL_REQUESTED")
