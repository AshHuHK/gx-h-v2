"""SC11/P03: exact turn history after a lost native spawn receipt (offline peer)."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from gx_core.errors import IdempotencyConflict
from gx_core.hostobs import host_policy, native_app_server, native_spawn
from gx_core.hostobs.app_server_client import ReceivedMessage
from gx_core.persistence import store


class NativeSpawnRecovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "gx.db"
        self.conn = store.open_store(self.path)
        self.addCleanup(lambda: self.conn.close())
        # Independent durable Runtime ledger counts every physical creation including duplicates.
        self.runtime = sqlite3.connect(Path(tmp.name) / "peer.db")
        self.addCleanup(self.runtime.close)
        self.runtime.execute("CREATE TABLE creations(item TEXT, child TEXT)")
        self.locator = dict(project_ref="p", session_id="root", parent_thread_id="root", thread_id="child")
        self.started = dict(id="op7", type="collabAgentToolCall", tool="spawnAgent",
                            senderThreadId="root", prompt="Compare both measurements",
                            receiverThreadIds=[], status="inProgress", agentsStates={})
        self.complete = {**self.started, "receiverThreadIds": ["child"], "status": "completed"}
        self.router = native_app_server.NativeRouter(self.conn, project_ref="p", max_turns=8, max_workers=4, tool_policy={"allow_mutations": True, "max_callbacks": 8})
        policy = host_policy.native_codex_policy()[0]
        self.router.register_root({"thread": {"id": "root", "sessionId": "root", "parentThreadId": None,
            "forkedFromId": None, "cliVersion": "0.169.0", "model": policy["pinned_model"]["id"],
            "reasoningEffort": policy["native_coordinator_observation"]["default_effort"]}}, 1)
        self.router.on_notification(ReceivedMessage(2, {"method": "turn/started", "params": {
            "threadId": "root", "turn": {"id": "turn"}}}))
        self.seq = 2

    def notify(self, method, item):
        self.seq += 1
        self.router.on_notification(ReceivedMessage(self.seq, {"method": method, "params": {
            "threadId": "root", "turnId": "turn", "item": item}}))

    def spawn_and_lose_ack(self):
        self.notify("item/started", self.started)
        with self.runtime:
            self.runtime.execute("INSERT INTO creations VALUES (?,?)", ("op7", "child"))
        self.conn.close()
        self.conn = store.open_store(self.path)

    def creation_count(self):
        return self.runtime.execute("SELECT count(*) FROM creations").fetchone()[0]

    def reconcile(self, pages):
        return native_spawn.reconcile(self.conn, project_ref="p", session_id="root", list_items=pages)

    def test_exact_turn_item_recovers_original_child(self):
        self.spawn_and_lose_ack()
        child, = self.runtime.execute("SELECT child FROM creations WHERE item=?", ("op7",)).fetchone()
        saved_item = {**self.complete, "receiverThreadIds": [child]}
        pages = Mock(side_effect=[
            {"data": [{"turnId": "other", "item": self.complete}], "nextCursor": "next"},
            {"data": [{"turnId": "turn", "item": saved_item}], "nextCursor": None}])
        recovered, = self.reconcile(pages)
        self.assertEqual(recovered["child_thread_id"], child)
        for call in pages.call_args_list:
            self.assertEqual(call.args[0]["threadId"], "root")
            self.assertEqual(call.args[0]["turnId"], "turn")
        self.assertEqual(self.reconcile(pages), [])
        self.assertEqual(pages.call_count, 2)
        self.assertEqual(self.creation_count(), 1)
        op = native_spawn.dispatch_for_worker(self.conn, **self.locator)
        self.assertTrue(op.startswith("spawn:"))
        self.assertIsNone(native_spawn.dispatch_for_worker(self.conn, **{**self.locator, "session_id": "other"}))

    def test_failed_incomplete_and_absent_receipts_stay_unknown(self):
        self.spawn_and_lose_ack()
        for item in ({**self.complete, "status": "failed"}, {**self.complete, "receiverThreadIds": []},
                     {**self.complete, "senderThreadId": "other"}, {**self.complete, "id": "other"}):
            page = {"data": [{"turnId": "turn", "item": item}]}
            pending, = self.reconcile(lambda _: page)
            self.assertEqual(pending["status"], "outcome_unknown")
            self.assertIsNone(pending["child_thread_id"])
        self.assertEqual(self.creation_count(), 1)
        self.assertIsNone(native_spawn.dispatch_for_worker(self.conn, **self.locator))

    def test_duplicate_receipt_and_conflicting_identity(self):
        self.notify("item/started", self.started)
        with self.assertRaises(IdempotencyConflict):
            self.notify("item/started", {**self.started, "prompt": "different request"})
        self.notify("item/completed", self.complete)
        self.notify("item/completed", self.complete)
        with self.assertRaises(IdempotencyConflict):
            self.notify("item/completed", {**self.complete, "receiverThreadIds": ["another"]})
        n = self.conn.execute("SELECT count(*) FROM events WHERE new_state='runtime_created'").fetchone()[0]
        self.assertEqual(n, 1)
        self.assertIsNotNone(native_spawn.dispatch_for_worker(self.conn, **self.locator))

    def test_native_started_activity_ties_creation_to_child(self):
        self.notify("item/completed", {"id": "activity", "type": "subAgentActivity", "kind": "started",
                                       "agentPath": "/root/child", "agentThreadId": "child"})
        self.assertIsNotNone(native_spawn.dispatch_for_worker(self.conn, **self.locator))
        self.assertIsNone(native_spawn.dispatch_for_worker(self.conn, **{**self.locator, "project_ref": "q"}))
