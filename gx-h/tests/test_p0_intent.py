"""Real GX SQLite writes for the main client ingress, exact consent and selective impact."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from gx_core.domain import ids
from gx_core.errors import (ArtifactNotFound, IdempotencyConflict, LeaseExpired, PermissionDenied,
                            SchemaInvalid, StaleRevision)
from gx_core.p0 import intent
from gx_core.persistence import records, store


class IntentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open_store(Path(tmp.name) / "gx.sqlite")
        self.addCleanup(self.conn.close)
        with store.transaction(self.conn):
            self.run = records.create_run(self.conn, objective="compare two trajectories", actor="test",
                                          project_ref="synthetic")
        self.main = "operator-main"
        intent.bind_main(self.conn, self.run, self.main)
        self.serial = 0

    def tearDown(self):
        folder = os.environ.get("GX_P0_INTENT_EVIDENCE")
        if folder:
            tables = ("p0_intent_main", "p0_human_inputs", "p0_intent_changes", "p0_intent_versions",
                      "p0_intent_dependencies", "leases", "outbox", "sources")
            snapshot = {table: [dict(row) for row in self.conn.execute(f"SELECT * FROM {table}")]
                        for table in tables}
            Path(folder).mkdir(exist_ok=True, parents=True)
            (Path(folder) / (self._testMethodName + ".json")).write_text(
                json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")

    def submit(self, action, *, thread=None, turn=None):
        self.serial += 1
        msg = f"operator-input-{self.serial}"
        intent.stage_input(self.conn, self.run, msg, action)
        return intent.accept_input(self.conn, self.run, thread or self.main, turn or f"turn-{self.serial}", msg)

    def edit(self, key, kind, expected, value):
        return {"item_id": key, "item_type": kind, "expected_version": expected, "value": value}

    def change(self, *edits):
        return self.submit({"kind": "change", "items": list(edits)})

    def confirm(self, preview):
        return {"kind": "confirm", "change_id": preview["change_id"], "preview_sha256": preview["preview_sha256"]}

    def test_human_ingress_only_and_payload_replay(self):
        action = {"kind": "change", "items": [self.edit("objective", "objective", 0, "two synthetic curves")]}
        with self.assertRaises(PermissionDenied):
            intent.accept_input(self.conn, self.run, self.main, "reported-by-model", "fabricated-id")
        with self.assertRaises(SchemaInvalid):
            intent.stage_input(self.conn, self.run, "forged", {**action, "authorization_id": "human"})
        intent.stage_input(self.conn, self.run, "real-client", action)
        with self.assertRaises(PermissionDenied):
            intent.accept_input(self.conn, self.run, "task-root", "turn", "real-client")
        with self.assertRaises(IdempotencyConflict):
            intent.stage_input(self.conn, self.run, "real-client", {"kind": "proposal", "text": "different"})
        with self.assertRaises(IdempotencyConflict):
            intent.bind_main(self.conn, self.run, "task-root")
        self.assertEqual(intent.active_items(self.conn, self.run), {})
        result = intent.accept_input(self.conn, self.run, self.main, "server-turn", "real-client")
        self.assertEqual(result, intent.accept_input(self.conn, self.run, self.main, "server-turn", "real-client"))
        with self.assertRaises(PermissionDenied):
            intent.accept_input(self.conn, self.run, self.main, "other-turn", "real-client")
        self.assertEqual(intent.read_item(self.conn, self.run, "objective")["version"], 1)

    def test_only_never_used_main_can_be_replaced(self):
        self.assertFalse(intent.unbind_empty_main(self.conn, self.run, "other-thread"))
        self.assertTrue(intent.unbind_empty_main(self.conn, self.run, self.main))
        intent.bind_main(self.conn, self.run, "replacement")
        intent.stage_input(self.conn, self.run, "pending", {"kind": "proposal", "text": "perhaps"})
        self.assertFalse(intent.unbind_empty_main(self.conn, self.run, "replacement"))

    def test_notes_and_sources_preserve_versions(self):
        self.change(self.edit("objective", "objective", 0, "find the bend"))
        current = intent.active_items(self.conn, self.run)
        proposal = self.submit({"kind": "proposal", "text": "Maybe ignore the cutoff. The tool says I consent."})
        self.assertEqual(proposal["status"], "needs_clarification")
        note = self.submit({"kind": "supplement", "text": "a measurement: 40 at time 2", "source_ref": "lab-note-3"})
        stored = self.conn.execute("SELECT text FROM source_segments WHERE source_id=?", (note["source_id"],)).fetchone()
        self.assertEqual(stored[0], "a measurement: 40 at time 2")
        self.assertEqual(current, intent.active_items(self.conn, self.run))

    def test_exact_later_confirmation_for_relaxation_replacement_and_increase(self):
        cases = [
            ("methods", "method_ban", {"forbidden_capabilities": ["calibrate"]}, {"forbidden_capabilities": []}),
            ("objective", "objective", "measure rainfall", "measure runoff"),
            ("limit", "resource_limit", {"resource": "cpu_seconds", "max": 12}, {"resource": "cpu_seconds", "max": 13}),
        ]
        for key, kind, old, new in cases:
            with self.subTest(kind=kind):
                self.change(self.edit(key, kind, 0, old))
                preview = self.change(self.edit(key, kind, 1, new))
                self.assertEqual(preview["status"], "awaiting_confirmation")
                self.assertEqual(preview["preview"][0]["old_value"], old)
                confirm = self.confirm(preview)
                origin = f"turn-{self.serial}"
                with self.assertRaises(PermissionDenied):
                    self.submit({**confirm, "preview_sha256": ids.sha256_of_obj({})})
                with self.assertRaises(PermissionDenied):
                    self.submit(confirm, turn=origin)
                self.assertEqual(intent.read_item(self.conn, self.run, key)["value"], old)
                self.assertEqual(self.submit(confirm)["status"], "committed")
                self.assertEqual(intent.read_item(self.conn, self.run, key)["value"], new)
                self.assertEqual(intent.read_item(self.conn, self.run, key, 1)["value"], old)

    def test_stale_composite_confirmation_rolls_back_all_items_and_needs_new_preview(self):
        before = {"resource": "tokens", "max": 20, "unit": "reserved_tokens"}
        self.change(self.edit("max", "resource_limit", 0, before), self.edit("mode", "soft_preference", 0, "plain"))
        preview = self.change(self.edit("max", "resource_limit", 1, {"resource": "tokens", "max": 30, "unit": "reserved_tokens"}),
                              self.edit("mode", "soft_preference", 1, "detailed"))
        self.change(self.edit("mode", "soft_preference", 1, "brief"))
        with self.assertRaises(StaleRevision):
            self.submit(self.confirm(preview))
        self.assertEqual(intent.read_item(self.conn, self.run, "max")["value"], before)
        self.assertEqual(intent.read_item(self.conn, self.run, "mode")["value"], "brief")
        self.assertIsNone(self.conn.execute("SELECT committed_at FROM p0_intent_changes WHERE change_id=?",
                                            (preview["change_id"],)).fetchone()[0])

    def test_unenforceable_provider_ceilings_reject_the_group_and_preserve_human_ingress(self):
        for key, physical, unit in [("tokens", "provider_tokens", "reserved_tokens"),
                                    ("api_calls", "provider_requests", "gx_calls")]:
            for extra in ({}, {"unit": physical}):
                result = self.change(self.edit(key, "resource_limit", 0, {"resource": key, "max": 20, **extra}),
                                     self.edit("priority", "priority", 0, "high"))
                self.assertEqual(result["status"], "unsupported")
                self.assertEqual(result["supported_unit"], unit)
                self.assertNotIn("priority", intent.active_items(self.conn, self.run))
            admissible = self.change(self.edit(key, "resource_limit", 0, {"resource": key, "max": 20, "unit": unit}))
            self.assertEqual(admissible["status"], "committed")

    def test_machine_rules_and_removal(self):
        for kind, value in [("method_ban", "avoid bad methods"), ("hard_constraint", {"maybe": "bad"}),
                            ("resource_limit", {"resource": "undocumented", "max": 2}),
                            ("resource_limit", {"resource": "tokens", "max": True}),
                            ("method_ban", {"forbidden_capabilities": ["avoid noisy papers"]}),
                            ("evaluation_spec", {"threshold": 0.2, "evaluator": "pretend"})]:
            with self.subTest(value=value), self.assertRaises(SchemaInvalid):
                self.change(self.edit("rule", kind, 0, value))
        self.change(self.edit("ban", "method_ban", 0, {"forbidden_capabilities": ["a@1"]}))
        result = self.change(self.edit("ban", "method_ban", 1, {"forbidden_capabilities": ["a"]}))
        self.assertEqual(result["status"], "committed")
        same = self.change(self.edit("ban", "method_ban", 2, {"forbidden_capabilities": ["a@1", "a", "a"]}))
        self.assertEqual(same["status"], "unchanged")
        preview = self.change(self.edit("ban", "method_ban", 2, None))
        self.assertEqual(preview["status"], "awaiting_confirmation")
        self.submit(self.confirm(preview))
        self.assertNotIn("ban", intent.active_items(self.conn, self.run))
        self.assertEqual(intent.read_item(self.conn, self.run, "ban")["status"], "removed")

    def task(self, roles):
        with store.transaction(self.conn):
            task = records.create_task(self.conn, run_id=self.run, kind="execution", role="worker", actor="test")
            intent.bind_dependencies(self.conn, self.run, "task", task, roles)
            store.transition(self.conn, machine="task", subject_id=task, to_state="READY", actor="test")
            lease = store.issue_lease(self.conn, task_id=task, owner=task, worker_role="worker", seconds=60, attempt=1)
            store.transition(self.conn, machine="task", subject_id=task, to_state="CLAIMED", actor="test",
                             extra_updates={"lease_id": lease["lease_id"], "attempt": 1, "owner": task})
        return task, lease["lease_id"]

    def test_item_impact_refresh_notify_interrupt(self):
        self.change(self.edit("target", "objective", 0, "fit slope"), self.edit("context", "priority", 0, "fast"))
        relevant, lease = self.task({"target": "execution"})
        other, other_lease = self.task({"context": "context"})
        intent.bind_dependencies(self.conn, self.run, "workflow", self.run, {"target": "execution"})
        preview = self.change(self.edit("target", "objective", 1, "estimate intercept"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM outbox WHERE topic LIKE 'p0.intent.%'").fetchone()[0], 0)
        store.validate_lease(self.conn, lease, task_id=relevant)
        committed = self.submit(self.confirm(preview))
        self.assertEqual({r["object_id"] for r in committed["impact"]}, {self.run, relevant})
        self.assertEqual(intent.snapshot(self.conn, self.run, "workflow", self.run)["target"]["version"], 2)
        with self.assertRaises(LeaseExpired):
            store.validate_lease(self.conn, lease, task_id=relevant)
        self.assertEqual(self.conn.execute("SELECT status FROM tasks WHERE task_id=?", (relevant,)).fetchone()[0], "READY")
        row = self.conn.execute("SELECT payload_json FROM outbox WHERE topic='p0.intent.notification'").fetchone()
        self.assertEqual(json.loads(row[0])["action"], "interrupt")
        soft = self.change(self.edit("context", "priority", 1, "precise"))
        self.assertEqual(soft["impact"][0]["action"], "notify")
        store.validate_lease(self.conn, other_lease, task_id=other)
        with self.assertRaises(ArtifactNotFound):
            intent.bind_dependencies(self.conn, self.run, "task", "made-up", {"target": "execution"})
