"""Raw runtime reports and optional telemetry failure behavior."""
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from gx_core import metrics
from gx_core.hostobs.app_server_client import ReceivedMessage
from gx_core.hostobs.native_app_server import NativeRouter
from gx_core.hostobs.runtime import runtime_adapter
from gx_core.persistence import store


class Measurements(TestCase):
    def test_router_preserves_multiple_snapshots_and_missing_fields(self):
        with TemporaryDirectory() as tmp, sqlite3.connect(":memory:") as db:
            trace = Path(tmp) / "trace.jsonl"
            router = NativeRouter(db, project_ref="p01", max_turns=1, max_workers=1,
                                  tool_policy={"allow_mutations": False, "max_callbacks": 1})
            router.authority.root_thread_id = "root"
            with mock.patch.dict(os.environ, GX_METRICS_FILE=str(trace)):
                for seq, total in enumerate((7, 7, 5), 1):
                    message = {"method": "thread/tokenUsage/updated", "params": {
                        "threadId": "root", "turnId": "one-turn",
                        "tokenUsage": {"total": {"totalTokens": total}}}}
                    router.on_notification(ReceivedMessage(seq, message))
            rows = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual([row["total"]["totalTokens"] for row in rows], [7, 7, 5])
            self.assertEqual(len({row["report_id"] for row in rows}), 3)
            self.assertTrue(all(row["last"] is None and row["total"]["inputTokens"] is None for row in rows))

    def test_unwritable_trace_keeps_transaction(self):
        with TemporaryDirectory() as tmp, sqlite3.connect(":memory:") as db:
            with mock.patch.dict(os.environ, GX_METRICS_FILE=tmp):
                with store.transaction(db):
                    db.execute("CREATE TABLE committed(value integer)")
                    db.execute("INSERT INTO committed VALUES (1)")
            self.assertEqual(db.execute("SELECT value FROM committed").fetchone(), (1,))

    def test_capabilities_match_selected_adapter(self):
        caps = runtime_adapter().capabilities("fixture-store")
        self.assertEqual(caps["runtime"]["runtime_namespace"], "fixture-store")
        self.assertIn("resume_session", caps["operations"])
        self.assertIn("cancel", caps["operations"])
        self.assertEqual(caps["cancel_semantics"], "delivery_only")
