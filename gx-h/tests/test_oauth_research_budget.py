"""Ordinary subscription research and diagnostic sessions keep separate resource limits."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from gx_core import budgets
from gx_core.errors import BudgetExceeded
from gx_core.persistence import store


class SubscriptionBudget(unittest.TestCase):
    def test_ordinary_grant_and_diagnostic_limits(self):
        included = budgets.profile("oauth_included")["limits"]
        ordinary = budgets.profile("oauth_research")["limits"]
        self.assertEqual(ordinary, {**budgets.profile("p0")["limits"], "tokens": included["tokens"]})
        grant = {"job_submissions": 10, "cpu_seconds": 1200, "worker_tasks": 40, "api_calls": 24,
                 "tokens": 9007199254740991, "gpu_jobs": 0, "gpu_minutes": 0, "wall_hours": 1}
        with tempfile.TemporaryDirectory() as tmp, closing(store.open_store(Path(tmp) / "gx.db")) as db:
            ref = budgets.create_root(db, run_id=None, actor="test", profile_name="oauth_research", limits=grant)
            saved, = db.execute("SELECT limits_json FROM budgets WHERE budget_ref=?", (ref,)).fetchone()
            self.assertEqual(json.loads(saved), grant)
            for key, ceiling in {"job_submissions": 4, "cpu_seconds": 300, "worker_tasks": 24}.items():
                with self.subTest(resource=key), self.assertRaises(BudgetExceeded) as denied:
                    budgets.create_root(db, run_id=None, actor="test", profile_name="oauth_included",
                                        limits={key: grant[key]})
                self.assertEqual(denied.exception.details, {"key": key, "requested": grant[key], "ceiling": ceiling})
            with self.assertRaises(BudgetExceeded):
                budgets.create_root(db, run_id=None, actor="test", profile_name="oauth_research",
                                    limits={"job_submissions": ordinary["job_submissions"] + 1})
            self.assertEqual(db.execute("SELECT count(*) FROM budgets").fetchone()[0], 1)
