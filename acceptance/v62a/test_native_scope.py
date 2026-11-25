"""Exercise the scoped native callback, including operation idempotency independent of the host call."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from interleavings_fixture import Scenario, draft
from gx_core.errors import IdempotencyConflict, ScopeDenied
from gx_core.hostobs.app_server_client import NO_TOOL_RESPONSE, ReceivedMessage
from gx_core.p0 import v62_native


class NativeModuleScope(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="gx-v62a-native-"))
        self.world = self.enterContext(Scenario(Path(directory)))
        self.opening = self.world.open("open-data", inputs=self.world.materials[:1])
        self.actor = self.world.next_actor(self.opening["scope"])
        self.row = self.world.roots._binding(self.actor.thread)

    def test_frozen_tools_and_private_workspace(self):
        current = self.world.roots._settings(self.row, new=True)
        direct = next(spec["tools"] for spec in current["dynamicTools"] if spec["name"] == "gx")
        self.assertEqual({tool["name"] for tool in direct}, {"gx_decide_revision", "gx_submit_module_report"})
        self.assertEqual({s["name"] for s in current["dynamicTools"]},
                         {"gx_task_context", "gx_read_artifact", "gx_query_catalog", "gx"})
        self.assertFalse(current["config"]["features.shell_tool"])
        self.assertFalse(current["config"]["agents.enabled"])
        self.assertEqual(current["config"]["web_search"], "disabled")
        self.assertEqual(current["environments"], [])
        self.assertEqual(current["approvalPolicy"], "never")
        self.assertIn("delegated local research module", current["developerInstructions"])
        (Path(self.row["cwd"]) / "unrelated.txt").write_text("public sentinel")
        with self.assertRaises(ScopeDenied):
            self.world.roots._settings(self.row, new=False)

    def test_read_receipt_reuse_and_cancelled_or_unknown_scope(self):
        w = self.world
        allowed = w.materials[0]
        args = {"ref": allowed, "operation_key": "inspect-pages"}
        first = w.call(self.actor, "gx_read_artifact", args, "read-1")
        with patch.object(w.roots, "_read_artifact", side_effect=AssertionError("duplicated operation")):
            self.assertEqual(w.call(self.actor, "gx_read_artifact", args, "another-native-call"), first)
            with self.assertRaises(IdempotencyConflict):
                w.call(self.actor, "gx_read_artifact", {**args, "offset": 20}, "changed-page")
        [receipt] = v62_native.operation_receipts(w.conn, w.run_id, self.opening["module_instance_id"])
        self.assertEqual(receipt["operation_key"], "inspect-pages")
        self.assertEqual(receipt["source_refs"], [allowed])
        self.assertEqual(receipt["lease_id"], self.actor.lease_id)
        with self.assertRaises(ScopeDenied):
            w.call(self.actor, "gx_read_artifact", {"ref": w.materials[-1]}, "marker")
        cancel = {"operation_key": "cancel-it", "module_instance_id": self.opening["module_instance_id"],
                  "reason": "The operator does not need further inventory."}
        w.call(w.lead, "gx_cancel_module", cancel, "cancel")
        with self.assertRaises(ScopeDenied):
            w.call(self.actor, "gx_read_artifact", args, "read-1")

    def test_open_delivery_after_parent_release(self):
        w = self.world
        call = w.conn.execute("SELECT request_json FROM p0_v6_native_calls WHERE thread_id=? AND call_id=?",
                              (w.lead.thread, self.opening["call_id"])).fetchone()
        original = json.loads(call[0])
        w.call(w.lead, "gx_decide_revision", {"decision": draft()}, "wait-for-inventory")
        w.roots.active_turns.pop(w.lead.thread, None)
        self.assertEqual(w.roots._call({**original, "callId": "redeliver-delegation"}), self.opening)
        conflicting = {**original["arguments"], "expected_delivery": "Changed deliverable"}
        with self.assertRaises(IdempotencyConflict):
            w.roots._call({**original, "callId": "changed-delivery", "arguments": conflicting})
        fresh = {**original["arguments"], "operation_key": "new-work-after-release"}
        with self.assertRaises(ScopeDenied):
            w.roots._call({**original, "callId": "fresh-delivery", "arguments": fresh})

    def test_late_read_denied_after_persisted_turn_end(self):
        w = self.world
        args = {"ref": w.materials[0], "operation_key": "read-before-interrupt"}
        w.call(self.actor, "gx_read_artifact", args, "read-1")
        # Another Driver may already have persisted the official end while this one
        # still considers the same turn active in memory.
        w.roots.delivery.observe("turn/completed", {"threadId": self.actor.thread,
            "turn": {"id": self.actor.turn, "status": "interrupted"}})
        self.assertEqual(w.roots.active_turns[self.actor.thread], self.actor.turn)
        for key in ("read-1", "read-2"):
            with self.subTest(key=key), self.assertRaises(ScopeDenied):
                w.call(self.actor, "gx_read_artifact", args, key)
        with self.assertRaises(ScopeDenied):
            w.call(self.actor, "gx_query_catalog", {"kind": "method"}, "stale-catalog")

    def test_report_native_reply_is_cancellation_owned_and_replayable(self):
        w = self.world
        ref = w.materials[0]
        w.call(self.actor, "gx_read_artifact", {"ref": ref}, "original")
        p = {"callId": "publish", "threadId": self.actor.thread, "turnId": self.actor.turn,
             "namespace": "gx", "tool": "gx_submit_module_report", "arguments": {
                "operation_key": "report", "status": "PARTIAL",
                "findings": [{"text": "Read the source file.", "source_refs": [ref]}],
                "gaps": ["Only one input was inspected."], "artifact_refs": [ref], "next_steps": []}}
        reply = w.roots._callback(ReceivedMessage(1, {"method": "item/tool/call", "params": p}))
        self.assertIs(reply, NO_TOOL_RESPONSE)
        receipt = w.roots._call(p)
        self.assertEqual(receipt["status"], "PARTIAL")
        self.assertTrue(receipt["report_ref"])
        self.assertIsNotNone(w.roots.terminal_products.for_thread(self.actor.thread))
        w.roots.active_turns.pop(self.actor.thread, None)
        self.assertEqual(w.roots._call({**p, "callId": "repeat-publish"}), receipt)
        changed = {**p["arguments"], "gaps": ["A different report."]}
        with self.assertRaises(IdempotencyConflict):
            w.roots._call({**p, "callId": "conflict", "arguments": changed})
        self.assertEqual(w.roots.terminal_products.for_thread(self.actor.thread)["call_id"], "publish")


if __name__ == "__main__":
    unittest.main()
