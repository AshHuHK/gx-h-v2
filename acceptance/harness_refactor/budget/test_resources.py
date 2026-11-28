"""Fixed outbound call slots require a distinct funded unit and the site's paid-services switch."""
from contextlib import closing, redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gx_core import budgets, config
from gx_core.capabilities import manifest, registry, shipped
from gx_core.errors import BudgetExceeded, SchemaInvalid
from gx_core.jobs import limits
from gx_core.jobs.service import JobService



class Resources(unittest.TestCase):
    def test_paid_slot_discovery_admission_and_settlement(self):
        from tests.test_v6_jobs import DynamicJobs
        from tests.test_v62_module_funding import ModuleFunding
        checked = DynamicJobs()
        self.addCleanup(checked.doCleanups)
        checked.setUp()
        bundle = registry.candidate(checked.conn, shipped.GRID)["bundle"]
        m = bundle["manifest"]
        self.assertNotIn("paid_api_calls", manifest.effective_resources(m, {}))
        m["resources"]["paid_api_calls"] = 2
        self.assertEqual(manifest.bundle_problems(bundle), [])
        for invalid in (0, -1, False, "2", 2.5):
            changed = copy.deepcopy(bundle)
            changed["manifest"]["resources"]["paid_api_calls"] = invalid
            self.assertTrue(manifest.bundle_problems(changed), invalid)
        from gx_core.p0 import v62_catalog
        cost = manifest.cost_card(m["resources"])
        self.assertEqual(cost, {"cost_class": "paid_api", "paid_api_calls": 2})
        card = v62_catalog._public_card(dict(capability_version="local-index@1", method=m["method_card"],
                                             inputs=manifest.input_roles(m), outputs=m["outputs"], **cost))
        self.assertEqual(card["paid_api_calls"], 2)
        self.assertEqual(v62_catalog._method_summary(card)["cost_class"], "paid_api")
        resources = manifest.effective_resources(m, {})
        self.assertEqual([f.details["switch"] for f in limits.switch_findings(resources, limits={})],
                         ["paid_external_services"])
        site = {"switches": {"paid_external_services": {"value": True}}}
        self.assertEqual(limits.switch_findings(resources, limits=site), [])
        amounts = JobService._budget_amounts(resources)
        self.assertEqual(amounts["paid_api_calls"], 2)
        self.assertNotIn("api_calls", amounts)
        self.assertEqual(JobService._actual({"started": True}, resources)["paid_api_calls"], 2)
        self.assertEqual(JobService._actual({"started": None}, resources)["paid_api_calls"], 2)
        self.assertEqual(JobService._actual({"started": False}, resources)["paid_api_calls"], 0)
        db = ModuleFunding()
        self.addCleanup(db.doCleanups)
        db.setUp()
        account = db.open("oauth-only", api_calls=3, job_submissions=1, cpu_seconds=60)
        with self.assertRaises(BudgetExceeded) as blocked:
            budgets.check_job_room(db.conn, account, "paid-test", amounts)
        needed = blocked.exception.details["shortfalls"]
        self.assertEqual([(d["key"], d["short_by"]) for d in needed], [("paid_api_calls", 2)])
        self.assertEqual(budgets.state(db.conn, account)["reserved"], {})

        # An explicit grant fits only a configured site profile. Override its returned
        # document for this test; the installed paid switch remains disabled.
        site = copy.deepcopy(config.limits())
        site["budget_profiles"]["p0"].pop("paid_api_calls", None)
        with patch.object(config, "limits", return_value=site), self.assertRaises(SchemaInvalid):
            budgets.create_root(db.conn, run_id=None, profile_name="p0",
                                limits={"paid_api_calls": 2}, actor="test-pi")
        site["budget_profiles"]["p0"]["paid_api_calls"] = 2
        with patch.object(config, "limits", return_value=site):
            parent = budgets.create_root(db.conn, run_id=None, profile_name="p0",
                                         limits={**amounts, "paid_api_calls": 2}, actor="test-pi")
        child = budgets.open_child(db.conn, parent_ref=parent, subject_kind="research_module",
                                   subject_id="local-api-control", limits=amounts, actor="lead")["budget_ref"]
        before = db.conn.total_changes
        budgets.check_job_room(db.conn, child, "preflight", amounts)
        self.assertEqual(db.conn.total_changes, before)
        hold = budgets.reserve(db.conn, budget_ref=child, subject_kind="job", subject_id="no-network",
                               amounts=amounts, actor="jobs")
        with self.assertRaises(BudgetExceeded) as blocked:
            budgets.check_job_room(db.conn, child, "second", amounts)
        lines = {d["key"]:d for d in blocked.exception.details["shortfalls"]}
        self.assertEqual(lines["paid_api_calls"]["short_by"], 2)
        budgets.settle(db.conn, hold["reservation_ref"], actual=JobService._actual({"started": True}, resources),
                       actor="jobs")
        self.assertEqual(budgets.state(db.conn, child)["spent"]["paid_api_calls"], 2)

    def test_site_quota_requires_explicit_parent_and_run_grants_at_public_entry(self):
        from gx_core.p0 import cli
        from gx_core.persistence import store
        site = copy.deepcopy(config.limits())
        site["budget_profiles"]["p0"]["paid_api_calls"] = 5
        cases = (({}, {}, (None, None)), ({"paid_api_calls": 4}, {}, (4, None)),
                 ({"paid_api_calls": 4}, {"paid_api_calls": 2}, (4, 2)))
        for workspace, run, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as folder:
                path = Path(folder)
                cfg = path / "grant.json"
                spec = {"capabilities": [], "research_modules": []}
                if workspace:
                    spec["workspace_budget"] = workspace
                if run:
                    spec["run_budget"] = run
                cfg.write_text(json.dumps(spec))
                with patch.object(config, "limits", return_value=site), redirect_stdout(io.StringIO()), patch.object(
                        cli, "run_until_stopped", side_effect=lambda db, args, rid, timeout: cli.snapshot(db, rid)):
                    self.assertEqual(cli.main(["research", "--state", str(path / "state"), "--run-key", "credits",
                                               "--goal", "Inspect the local arithmetic records.", "--goal-config", str(cfg)]), 0)
                with closing(store.open_store(path / "state/gx.db")) as db:
                    run_ref, = db.execute("SELECT budget_ref FROM runs").fetchone()
                    parent, = db.execute("SELECT parent_ref FROM budgets WHERE budget_ref=?", (run_ref,)).fetchone()
                    self.assertEqual(tuple(budgets.state(db, ref)["limits"].get("paid_api_calls")
                                           for ref in (parent, run_ref)), expected)
                    self.assertEqual(0, db.execute("SELECT count(*) FROM jobs").fetchone()[0])

    def test_compute_units_are_declared_by_resources_and_never_by_method_name(self):
        base = {"cpu_threads": 8, "wall_seconds": 121, "gpu": None}
        self.assertEqual(JobService._budget_amounts(base), {"job_submissions": 1, "cpu_seconds": 968})
        gpu = {**base, "gpu": {"scope":"local", "memory_mib": 1}}
        self.assertEqual(JobService._budget_amounts(gpu),
                         {"job_submissions": 1, "cpu_seconds": 968, "gpu_jobs": 1, "gpu_minutes": 3})
