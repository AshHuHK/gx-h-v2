"""Exercise native turn admission with explicit and omitted module token limits."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from acceptance.v62a.interleavings_fixture import Scenario
from gx_core import budgets
from gx_core.errors import BudgetExceeded, HoldActive
from gx_core.p0 import v62_budget


class ModuleTokenGrant(unittest.TestCase):
    def setUp(self):
        work = Path(self.enterContext(TemporaryDirectory()))
        self.s = self.enterContext(Scenario(work))

    def child(self, key, *, tokens=None):
        s = self.s
        payload = s.open_args(key)
        if tokens is None:
            del payload["sub_budget"]["tokens"]
        else:
            payload["sub_budget"]["tokens"] = tokens
        opened = s.call(s.lead, "gx_open_module", payload, key)
        module = s.conn.execute("SELECT * FROM v62_modules WHERE module_instance_id=?",
                                (opened["module_instance_id"],)).fetchone()
        return module, s.next_actor(module["scope"])

    def request(self, actor, name):
        row, fresh = self.s.roots.delivery._prepare(actor.thread, "Compare the coordinate counts.",
                                                    name, False, False, "request-" + name, None)
        self.assertTrue(fresh)
        self.assertEqual(row["method"], "turn/start")
        return row

    def test_sibling_omission_funds_both_native_turns(self):
        parent = budgets.budget_for(self.s.conn, run_id=self.s.run_id)
        available = budgets.state(self.s.conn, parent)["available"]["tokens"]
        holds = []
        left, left_actor = self.child("first")
        right, right_actor = self.child("second")
        for module, actor in [(left, left_actor), (right, right_actor)]:
            self.assertIsNone(v62_budget.exhausted(self.s.conn, module))
            row = self.request(actor, module["module_instance_id"])
            self.assertGreater(row["token_budget"], 0)
            self.assertLessEqual(row["token_budget"], self.s.roots.token_budget)
            holds.append((module, row))
        self.assertEqual(2, self.s.conn.execute(
            "SELECT count(*) FROM budget_reservations WHERE subject_kind='native_turn' AND status='HELD'").fetchone()[0])

        actual_total = 0
        for (module, row), delta in zip(holds, (-1, 2), strict=True):
            spent = row["token_budget"] + delta
            actual_total += spent
            for replay in (False, True):
                receipt = budgets.settle(self.s.conn, row["reservation_ref"],
                                         actual={"tokens": spent, "api_calls": 1}, actor="fixture")
                self.assertEqual(receipt["reused"], replay)
                self.assertEqual(receipt["unbudgeted"], {})
                self.assertEqual(receipt["overrun"], {"tokens": delta} if delta > 0 else {})
                self.assertEqual(receipt["over_limit"], {})
            for _ in range(2):
                self.assertTrue(v62_budget.close_if_settled(self.s.conn, module["budget_ref"]))
        parent_state = budgets.state(self.s.conn, parent)
        self.assertEqual(parent_state["spent"]["tokens"], actual_total)
        self.assertEqual(parent_state["reserved"].get("tokens", 0), 0)
        self.assertEqual(parent_state["available"]["tokens"], available - actual_total)

    def test_omission_obeys_three_remaining_run_tokens(self):
        parent = budgets.budget_for(self.s.conn, run_id=self.s.run_id)
        tokens = budgets.state(self.s.conn, parent)["available"]["tokens"] - 3
        prior = budgets.reserve(self.s.conn, budget_ref=parent, subject_kind="fixture_prior_usage",
                                subject_id="previous-measurement", amounts={"tokens": tokens}, actor="fixture")
        budgets.settle(self.s.conn, prior["reservation_ref"], actual={"tokens": tokens}, actor="fixture")
        child, actor = self.child("parent-has-three")
        self.assertIsNone(v62_budget.exhausted(self.s.conn, child))
        view = v62_budget.view_for_scope(self.s.conn, self.s.run_id, child["scope"])
        self.assertEqual(view["token_funding"], "shared_run")
        self.assertNotIn("tokens", view["limits"])
        self.assertEqual(view["available"]["tokens"], 3)
        reserved = self.request(actor, "under-parent")["token_budget"]
        self.assertGreater(reserved, 0)
        self.assertLessEqual(reserved, 3)

    def test_explicit_zero_prevents_native_turn_and_returns_partial(self):
        module, actor = self.child("no-tokens", tokens=0)
        with self.assertRaises((HoldActive, BudgetExceeded)) as refused:
            self.request(actor, "zero")
        self.assertEqual(refused.exception.details["key"], "tokens")
        self.s.advance()
        status, = self.s.conn.execute("SELECT status FROM v62_modules WHERE module_instance_id=?",
                                      (module["module_instance_id"],)).fetchone()
        self.assertEqual(status, "PARTIAL")
        self.assertEqual(0, self.s.conn.execute(
            "SELECT count(*) FROM budget_reservations WHERE subject_kind='native_turn'").fetchone()[0])

    def test_explicit_three_token_limit_survives_a_larger_host_turn_request(self):
        module, actor = self.child("three-tokens", tokens=3)
        self.assertGreater(self.s.roots.token_budget, 3)
        row = self.request(actor, "small")
        self.assertEqual(row["token_budget"], 3)
        budgets.settle(self.s.conn, row["reservation_ref"], actual={"tokens": 3, "api_calls": 1}, actor="fixture")
        self.assertIn("tokens", v62_budget.exhausted(self.s.conn, module))
        self.assertEqual(0, v62_budget.remaining(self.s.conn, module["budget_ref"])["tokens"])


if __name__ == "__main__":
    unittest.main()
