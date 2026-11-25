import tempfile
import unittest
from pathlib import Path

from gx_core import budgets
from gx_core.p0 import schema
from gx_core.p0.v6_costs import resource_report
from gx_core.persistence import records, store


class ResourceReportTests(unittest.TestCase):
    def test_delegated_budget_reports_only_actual_work_and_retains_units_on_close(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = store.open_store(Path(tmp.name) / "gx.db")
        self.addCleanup(db.close)
        schema.install(db)
        run = records.create_run(db, objective="Compare coordinates", project_ref="test", actor="pi")
        root = budgets.create_root(db, run_id=run, profile_name="p0", actor="pi",
                                   limits={"cpu_seconds": 90, "tokens": 100})
        db.execute("UPDATE runs SET budget_ref=? WHERE run_id=?", (root, run))
        child = budgets.open_child(db, parent_ref=root, subject_kind="scope", subject_id="inventory",
                                   limits={"cpu_seconds": 60}, actor="pi")["budget_ref"]
        work = budgets.reserve(db, budget_ref=child, subject_kind="job", subject_id="sample",
                               amounts={"cpu_seconds": 30}, actor="runner")
        self.assertEqual(resource_report(db, run)["lines"]["cpu_seconds"]["reserved"], 30)
        budgets.settle(db, work["reservation_ref"], actual={"cpu_seconds": 3}, actor="runner")
        for closed in (False, True):
            if closed:
                budgets.release(db, budgets.FUND_PREFIX + child, reason="finished", actor="runner")
            report = resource_report(db, run)
            self.assertEqual(report["lines"]["cpu_seconds"],
                             {"unit": "CPU seconds", "limit": 90, "booked": 3, "reserved": 0, "remaining": 87})
            self.assertEqual(report["provider_charge"], {"unit": "USD", "amount": None,
                                                       "source": "provider_money_cost"})
            self.assertEqual(report["measured_cpu"]["observed_jobs"], 0)
