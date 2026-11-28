"""The researcher's attachment recipient survives Main interpretation, freezes and replay."""
import json
from pathlib import Path
import tempfile
import unittest

from interleavings_fixture import Scenario, branch, draft
from gx_core.errors import ScopeDenied
from gx_core.p0 import conversation, intent, pi_materials, v621_assignment
from gx_core.p0.driver import Driver


class PIMaterials(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory(prefix="gx-pi-materials-"))
        self.w = self.enterContext(Scenario(Path(temp), spec_updates={"research_effort": "deep"}))
        self.driver = Driver(self.w.conn, self.w.state, self.w.run_id)
        self.addCleanup(self.driver.close)
        intent.bind_main(self.w.conn, self.w.run_id, "main")

    def attach(self, mid, original, target=None):
        w = self.w
        text = "请核对这份附加材料。"
        conversation.send(w.conn, w.run_id, text, mid, branch_id=target, attachments=[original])
        intent.stage_input(w.conn, w.run_id, mid, {"kind": "text", "text": text})
        intent.accept_input(w.conn, w.run_id, "main", "turn-" + mid, mid)
        return pi_materials.for_message(w.conn, w.run_id, mid)[0]["artifact_ref"]

    def route(self, mid, ref, target):
        return self.driver.main_read("gx_route_pi_message", {"clauses": [
            {"kind": "material", "quote": "请核对这份附加材料。", "branch_id": target, "artifact_refs": [ref]}]},
            {"thread_id": "main", "turn_id": "turn-" + mid, "call_id": "route-" + mid})

    def test_module_only_input_freezes_and_does_not_leave_through_released_report(self):
        w = self.w
        a, b = w.open("A"), w.open("B")
        actor_a, actor_b = w.bind(a["task_id"]), w.bind(b["task_id"])
        frozen = [w.brief(agent) for agent in (w.lead, actor_a, actor_b)]
        body = "The reference row carries a manually supplied batch label."
        original = {"name": "batch-note.txt", "text": body}
        ref = self.attach("pi-a", original, a["module_instance_id"])
        result = self.route("pi-a", ref, a["branch_id"])
        self.assertEqual(result["clauses"][0]["status"], "routed", result)
        self.assertEqual(self.route("pi-a", ref, a["branch_id"]), result)
        self.assertEqual(w.conn.execute("SELECT count(*) FROM events WHERE kind=? AND subject_id=?",
                                        (v621_assignment.GRANTED, a["module_instance_id"])).fetchone()[0], 1)
        self.assertEqual([w.brief(agent) for agent in (w.lead, actor_a, actor_b)], frozen)
        self.assertNotIn(ref, json.dumps(frozen))
        with self.assertRaises(ScopeDenied):
            w.call(actor_b, "gx_read_artifact", {"ref": ref}, "b-guesses-private-ref")
        with self.assertRaises(ScopeDenied):
            w.call(actor_a, "gx_read_artifact", {"ref": ref}, "a-first-frozen")
        with self.assertRaises(ScopeDenied):
            conversation.send(w.conn, w.run_id, "Reuse this original", "pi-b", branch_id=b["module_instance_id"],
                              attachments=[{"artifact_ref": ref}])
        w.call(actor_a, "gx_decide_revision", {"decision": draft()}, "finish-first-a")
        w.advance()
        receiver = w.next_actor(a["scope"])
        self.assertIn(ref, [m["artifact_ref"] for m in w.brief(receiver)["materials"]])
        read = w.call(receiver, "gx_read_artifact", {"ref": ref}, "a-received")
        self.assertIn(body, json.dumps(read))
        with self.assertRaises(ScopeDenied) as denied:
            w.report(receiver, ref, key="a-private-in-public-report")
        self.assertEqual(denied.exception.code, "SCOPE_DENIED")
        self.assertEqual(denied.exception.message, "The module must read the cited artifact or complete its producing Job")
        self.assertEqual(denied.exception.details, {})
        w.call(actor_b, "gx_read_artifact", {"ref": w.materials[0]}, "b-original")
        self.assertEqual(w.report(actor_b, w.materials[0], key="finish-b")["status"], "COMPLETE")
        self.assertFalse(w.conn.execute("SELECT 1 FROM jobs WHERE run_id=?", (w.run_id,)).fetchone())

    def test_public_graph_delegation_receives_only_its_own_private_original(self):
        w = self.w
        w.call(w.lead, "gx_decide_revision", {"decision": draft(branch("a"), branch("b"))}, "two-branches")
        rows = w.rows("SELECT branch_id FROM v6_branches WHERE run_id=? ORDER BY rowid", w.run_id)
        a, b = [r["branch_id"] for r in rows]
        original = {"name": "one-route.txt", "text": "A private acquisition hypothesis"}
        ref = self.attach("restricted-graph-input", original, a)
        self.assertEqual(self.route("restricted-graph-input", ref, a)["clauses"][0]["status"], "routed")
        lead = w.next_actor("run")
        self.assertNotIn(ref, json.dumps(w.brief(lead)))
        receivers = []
        for key, owner in (("a", a), ("b", b)):
            receipt = w.call(lead, "gx_delegate_branch", {"branch_id": owner, "question": "Inspect assigned inputs."},
                             "delegate-" + key)
            receivers.append(w.bind(receipt["task_id"]))
        self.assertIn(original["text"], json.dumps(w.call(receivers[0], "gx_read_artifact", {"ref": ref}, "read-a")))
        self.assertNotIn(ref, json.dumps(w.brief(receivers[1])))
        with self.assertRaises(ScopeDenied):
            w.call(receivers[1], "gx_read_artifact", {"ref": ref}, "b-cross-route")

    def test_unrelated_ref_and_wider_scope_are_rejected_but_shared_original_is_delivered(self):
        w = self.w
        a, b = w.open("A"), w.open("B")
        original = {"name": "notes.md", "text": "Recorded by the PI. Independent measurement remains pending."}
        private = self.attach("private", original, a["module_instance_id"])
        rejected = self.route("private", private, b["branch_id"])
        self.assertEqual(rejected["clauses"][0]["status"], "needs_clarification")
        common = self.attach("forged", {"artifact_ref": w.materials[0]})
        rejected = self.route("forged", private, "run")
        self.assertEqual(rejected["clauses"][0]["status"], "needs_clarification")
        self.assertEqual(common, w.materials[0])
        shared = self.attach("share", original)
        self.assertEqual(self.route("share", shared, "run")["clauses"][0]["status"], "routed")
        w.call(w.lead, "gx_decide_revision", {"decision": draft()}, "complete-frozen-lead")
        lead = w.next_actor("run")
        received = w.call(lead, "gx_read_artifact", {"ref": shared}, "read-common")
        self.assertIn(original["text"], json.dumps(received))
        paragraph = w.call(lead, "gx_read_source", {"source_ref": shared, "segment_id": "p1"}, "read-paragraph")
        self.assertEqual(paragraph["text"], original["text"])
        self.assertEqual((paragraph["source_message_id"], paragraph["source_kind"]), ("share", "pi_material"))
        self.assertNotIn(private, json.dumps(w.brief(lead)))
        self.assertFalse(w.conn.execute("SELECT 1 FROM events WHERE run_id=? AND kind='v6.observation.added' "
                                        "AND json_extract(payload_json,'$.artifact_ref')=?", (w.run_id, shared)).fetchone())


if __name__ == "__main__":
    unittest.main()
