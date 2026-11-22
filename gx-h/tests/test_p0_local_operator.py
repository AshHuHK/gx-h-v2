"""Structured operator inputs, exact approvals and funded run allocations across cold connections."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import PropertyMock, patch

from gx_core import budgets
from gx_core.context import Principal
from gx_core.errors import BudgetExceeded, PermissionDenied, SchemaInvalid, StaleRevision
from gx_core.p0 import cli, intent, operator
from gx_core.p0.driver import Driver, enqueue
from gx_core.persistence import records, store


class LocalOperator(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.conn = store.open_store(self.state / "gx.db")
        self.addCleanup(self.conn.close)
        self.ctx = operator.local_context(self.conn)
        self.budget = budgets.create_root(self.conn, run_id=None, actor="test", profile_name="p0")
        self.rid = self.run_record("one", self.budget)
        self.conn.execute("UPDATE budgets SET run_id=? WHERE budget_ref=?", (self.rid, self.budget))

    def run_record(self, name, budget):
        return records.create_run(self.conn, objective=name, project_ref="shared", actor="test", budget_ref=budget,
                                  workflow_id="science_grid", workflow_version="1",
                                  inputs={"inputs": {"p0": True, "p0_spec": {}}})

    @staticmethod
    def edit(key, kind, value, version=0):
        return {"kind": "change", "items": [{"item_id": key, "item_type": kind, "value": value,
                                             "expected_version": version}]}

    def submit(self, action, mid, rid=None):
        return operator.submit(self.ctx, rid or self.rid, action, mid)["result"]

    def cli(self, verb, *args, ok=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main([verb, "--state", str(self.state), *args])
        self.assertEqual(code, 0 if ok else 1, err.getvalue())
        return json.loads(out.getvalue()) if ok else err.getvalue()

    def test_bootstrap_without_native_identity_then_native_binding_and_queued_replay(self):
        edit = self.edit("objective", "objective", "Observe")
        boot = operator.apply_bootstrap(self.conn, self.rid, edit, "boot")
        self.assertEqual(boot["status"], "committed")
        self.assertEqual(boot, self.submit(edit, "boot"))
        local = self.conn.execute("SELECT * FROM p0_human_inputs WHERE message_id='boot'").fetchone()
        self.assertIsNone(local["receipt_turn_id"])
        self.assertEqual(self.conn.execute("SELECT thread_id FROM p0_intent_main WHERE run_id=?", (self.rid,)).fetchone()[0], "")
        with self.assertRaises(PermissionDenied):
            intent.stage_input(self.conn, self.rid, "native", {"kind": "text", "text": "hello"})
        intent.bind_main(self.conn, self.rid, "genuine-thread")
        with self.assertRaises(PermissionDenied):
            intent.accept_input(self.conn, self.rid, "genuine-thread", "model-turn", "boot")
        cap = self.edit("tokens", "resource_limit", {"resource": "tokens", "unit": "reserved_tokens", "max": 10})
        operator.submit(self.ctx, self.rid, cap, "queued", queue_only=True)
        with patch.object(Driver, "native", new_callable=PropertyMock, side_effect=AssertionError("native contacted")):
            Driver(self.conn, self.state, self.rid).messages()
        self.assertEqual(self.submit(cap, "queued")["status"], "committed")
        self.assertEqual(intent.read_item(self.conn, self.rid, "tokens")["version"], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM p0_root_deliveries").fetchone()[0], 0)
        self.assertTrue(intent.unbind_empty_main(self.conn, self.rid, "genuine-thread"))
        self.assertEqual(intent.bind_main(self.conn, self.rid, "replacement"), "replacement")

    def test_existing_uncertain_delivery_stays_with_native_owner(self):
        edit = self.edit("objective", "objective", "Observe")
        operator.submit(self.ctx, self.rid, edit, "already-sent", queue_only=True)
        self.conn.execute("INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,human,"
                          "interrupt,state) VALUES(?,'human:already-sent','old-thread','already-sent','abc',1,0,'sending')",
                          (self.rid,))
        self.assertEqual(self.submit(edit, "already-sent")["status"], "pending_native_receipt")
        self.assertFalse(intent.active_items(self.conn, self.rid))
        row = self.conn.execute("SELECT * FROM p0_operator_messages WHERE message_id='already-sent'").fetchone()
        self.assertFalse(operator.consume_queued(self.ctx, self.rid, row))

    def test_exhausted_native_input_does_not_block_a_queued_local_relief(self):
        enqueue(self.conn, self.rid, {"kind": "text", "text": "Explain the findings."}, "Explain the findings.", "talk")
        edit = self.edit("hard", "resource_limit", {"resource": "cpu_seconds", "max": 80})
        operator.submit(self.ctx, self.rid, edit, "relief", queue_only=True)
        class Exhausted:
            def ensure_main(self):
                return "main-thread"

            def human_turn(self, *args, **kwargs):
                raise BudgetExceeded("The main has exhausted its allowance")

        driver = Driver(self.conn, self.state, self.rid)
        with patch.object(Driver, "native", new_callable=PropertyMock, return_value=Exhausted()):
            driver.messages()
        self.assertEqual(intent.read_item(self.conn, self.rid, "hard")["value"]["max"], 80)
        self.assertIsNone(self.conn.execute("SELECT result_json FROM p0_operator_messages WHERE message_id='talk'").fetchone()[0])

    def test_operator_authority_is_the_transport_not_fields(self):
        message = self.edit("objective", "objective", "Run")
        for ctx in (self.ctx.child(principal=Principal("m", "worker")),
                    self.ctx.child(principal=Principal("h", "operator", ("unrelated",))),
                    self.ctx.child(transport="native_app_server"), self.ctx.child(native_call={"operator": True})):
            with self.assertRaises(PermissionDenied):
                operator.submit(ctx, self.rid, message)
        with self.assertRaises(SchemaInvalid):
            self.submit({**message, "actor": "operator"}, "spoof")
        self.assertFalse(intent.active_items(self.conn, self.rid))

    def test_budget_cannot_be_confirmed_before_actual_display_or_applied_after_aba(self):
        plan = self.submit({"kind": "budget_preview", "resource": "tokens", "limit": 80}, "preview")
        ok = {"kind": "budget_apply", "preview_id": "preview", "preview_sha256": plan["preview_sha256"]}
        operator.submit(self.ctx, self.rid, ok, "too-early", queue_only=True)
        self.cli("operator", "pending")
        with self.assertRaises(PermissionDenied):
            self.submit(ok, "too-early")
        budgets.tighten(self.conn, self.budget, limits={"tokens": 90}, actor="test", reason="Intervening edit")
        with self.assertRaises(StaleRevision):
            self.submit(ok, "stale")
        limited = self.cli("operator", "budget-preview", "--resource", "tokens", "--limit", "80")["result"]
        fresh = {**ok, "preview_id": limited["preview_id"], "preview_sha256": limited["preview_sha256"]}
        self.assertEqual(self.submit(fresh, "later")["limit"], 80)
        self.assertEqual(self.submit(fresh, "later")["limit"], 80)
        with self.assertRaises(PermissionDenied):
            self.submit(fresh, "twice")

    def test_named_run_resize_preserves_parent_and_sibling_funding(self):
        shared = self.budget
        budgets.tighten(self.conn, shared, limits={"tokens": 100}, actor="test", reason="test")
        one = self.run_record("a", shared)
        two = self.run_record("b", shared)
        allocated = {}
        for rid, amount in ((one, 30), (two, 50)):
            child = budgets.open_child(self.conn, parent_ref=shared, subject_kind="run", subject_id=rid,
                                       limits={"tokens": amount}, actor="test")["budget_ref"]
            self.conn.execute("UPDATE runs SET budget_ref=? WHERE run_id=?", (child, rid))
            self.conn.execute("UPDATE budgets SET run_id=? WHERE budget_ref=?", (rid, child))
            allocated[rid] = child
        plan = self.cli("operator", "budget-preview", "--run-id", one, "--resource", "tokens", "--limit", "42")["result"]
        ok = {"kind":"budget_apply", "preview_id":plan["preview_id"], "preview_sha256":plan["preview_sha256"]}
        with self.assertRaises(PermissionDenied):
            self.submit(ok, "other-run-confirm", two)
        self.assertEqual(self.submit(ok, "confirm-one", one)["limit"], 42)
        self.assertEqual(budgets.state(self.conn, shared)["reserved"]["tokens"], 92)
        self.assertEqual(budgets.state(self.conn, allocated[two])["limits"]["tokens"], 50)
        self.assertTrue(budgets.verify(self.conn, shared)["ok"])
        try_more = self.cli("operator", "budget-preview", "--run-id", one, "--resource", "tokens", "--limit", "51")["result"]
        with self.assertRaises(BudgetExceeded):
            self.submit({"kind": "budget_apply", "preview_id": try_more["preview_id"],
                         "preview_sha256": try_more["preview_sha256"]}, "overalloc", one)
        self.assertTrue(budgets.verify(self.conn, shared)["ok"])
        self.assertEqual(budgets.state(self.conn, shared)["available"]["tokens"], 8)
