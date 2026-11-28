"""Ordinary GX native tool rights, parent-owned delegation and post-round append."""
from pathlib import Path
import json
import tempfile
import unittest

from gx_core.errors import BudgetExceeded, ScopeDenied
from gx_core.plugins.author_api import _ResearchModules
from gx_core.p0 import v62_reports, v621_policy
from acceptance.harness_refactor.modules.research_study import ResearchStudy


class ModuleAutonomy(unittest.TestCase):
    def world(self, **spec):
        case = {"research_effort": "medium", "evidence_policy": v621_policy.VERSION,
                "capabilities": [], "plugins": [],
                "capability_collections": ["public_databases"]}
        case.update(spec)
        return self.enterContext(ResearchStudy(Path(self.enterContext(tempfile.TemporaryDirectory())), spec_updates=case))

    def open(self, s, key, number, **changes):
        args = s.open_args(key, inputs=s.materials[:1], caps=[], round=number)
        args.update(module_id="ad_hoc", focus="Compare the origin and coverage of the records.",
                    expected_delivery="Report the coverage with a traceable source.", **changes)
        return s.call(s.lead, "gx_open_module", args, key)

    def test_medium_plugin_can_declare_a_new_knowledge_focus(self):
        modules = _ResearchModules()
        modules.register(module_id="archive_reader", version="1", purpose="compare sources",
                         output="cited readings", cost_class="local", focuses=["source interpretation"],
                         instructions="Read the assigned originals and qualify the claims.",
                         capabilities=[], tools=["gx_submit_module_report"], input_roles=["*"],
                         efforts=["medium"])
        self.assertIn(("archive_reader", "1"), modules._seal())

    def test_quick_and_medium_have_the_original_source_tools_without_delegation(self):
        for effort in ("quick", "medium"):
            with self.subTest(effort=effort):
                study = self.world(research_effort=effort, research_modules=[], branch_delegation=False)
                packet = study.brief(study.lead)
                self.assertFalse(packet["branch_delegation"])
                tools = study.rows("SELECT tool_names_json FROM p0_roots WHERE task_id=?", study.lead.task_id)[0]
                self.assertNotIn("gx_open_module", json.loads(tools["tool_names_json"]))
                source = study.materials[0]
                listing = study.call(study.lead, "gx_read_source", {"source_ref":source, "query":"A17"}, "list-original")
                original = study.call(study.lead, "gx_read_source",
                    {"source_ref":source, "segment_id":listing["segments"][0]["segment_id"]}, "read-original")
                self.assertIn("Public archive index", original["text"])
                self.assertTrue(original["receipt_id"])

    def test_quick_default_allows_one_funded_researcher_and_explicit_round_cap_is_enforced(self):
        study = self.world(research_effort="quick")
        self.assertTrue(self.open(study, "quick-help", 1)["scope"].startswith("module:"))
        with self.assertRaises(BudgetExceeded):
            self.open(study, "quick-parallel", 1)
        bounded = self.world(module_policy={"max_rounds":1})
        with self.assertRaises(ScopeDenied):
            self.open(bounded, "exceeds-pi-cap", 2)

    def test_adhoc_with_a_named_researcher_inherits_only_selected_choices(self):
        s = self.world()
        cap = "yeast-archive-lookup@1"
        first = self.open(s, "archive-read", 1, researcher="Archive reader", method_choices=[cap],
                          communication="isolated")
        self.assertEqual(first["effective_grant"]["researcher"], "Archive reader")
        self.assertEqual(first["effective_grant"]["communication"], "isolated")
        self.assertEqual(list(first["effective_grant"]["method_options"]), [cap])
        self.assertEqual(first["effective_grant"]["capabilities"], [])
        self.assertIn("gx_read_source", first["effective_grant"]["tools"])
        self.assertEqual(self.open(s, "archive-read", 1, researcher="Archive reader", method_choices=[cap],
                                   communication="isolated")["task_id"], first["task_id"])
        with self.assertRaises(ScopeDenied):
            self.open(s, "bad", 1, method_choices=["not-installed@1"])

    def test_append_to_a_prior_round_without_displacing_existing_modules(self):
        s = self.world()
        early = self.open(s, "first", 1)
        later = self.open(s, "other-route", 2)
        extra = self.open(s, "new-evidence-on-first-route", 1)
        self.assertEqual(len({r["task_id"] for r in (early,later,extra)}), 3)
        self.assertEqual(len(s.rows("SELECT 1 FROM v62_modules WHERE status='OPEN'")), 3)
        for opened in (early, extra):
            actor = s.next_actor(opened["scope"])
            s.call(actor, "gx_read_artifact", {"ref":s.materials[0]}, "read-original")
            report = s.report(actor, s.materials[0])
            self.assertGreater(report["wake_event_id"], 0)
        released = v62_reports._released(s.conn, s.run_id)
        self.assertEqual(released, {early["module_instance_id"], extra["module_instance_id"]})
        self.assertFalse(v62_reports.reconcile_rounds(s.conn, s.run_id))
        self.assertEqual(len(s.rows("SELECT 1 FROM v62_module_wakes WHERE run_id=?", s.run_id)), 2)
        self.assertEqual(s.rows("SELECT status FROM v62_modules WHERE scope=?", later["scope"])[0]["status"], "OPEN")
