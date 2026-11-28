"""Ordinary Goal admission freezes selectable providers without activating them."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gx_core.errors import SchemaInvalid, ScopeDenied, VersionImmutable
from gx_core.p0 import capability_views as views, research
from gx_core.persistence import store
from tests.test_v62_modules_publication import Scenario


class GoalMethodCollections(unittest.TestCase):
    def test_public_research_command_freezes_default_options_and_resumes(self):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        args = SimpleNamespace(command="research", state=state, run_key="public", run_id=None,
                               goal="Which peer reviewed estimates report the observation window?",
                               goal_file=None, goal_config=None, research_effort=None, capability=None,
                               input=None, timeout=0)
        with patch.dict("os.environ", GX_CODEX_OFFLINE="1"):
            started = research.execute(args, state)
            with closing(store.open_store(state / "gx.db")) as conn:
                inputs = json.loads(conn.execute("SELECT inputs_json FROM runs").fetchone()[0])["inputs"]
                self.assertEqual(inputs["discovery_policy"], views.DISCOVERY_POLICY)
                self.assertIn("public_databases", inputs["method_scope"]["capability_collections"])
                choice = views.goal_options(inputs)["scholarly-search@1"]
                self.assertEqual(choice["provider"], "org.gx.scholarly-sources@1.0.0")
                self.assertRegex(choice["provider_digest"], "^[0-9a-f]{64}$")
                self.assertEqual(views.current(conn, started["run_id"])["active"], {})
            with patch("gx_core.p0.catalog_index.installed_methods", side_effect=AssertionError("scope re-expanded")):
                resumed = research.execute(args, state)
            self.assertEqual((started["run_id"], resumed["invocation"]["action"]), (resumed["run_id"], "resume"))

    def test_original_replay_and_explicit_empty_never_expand(self):
        for spec in ({}, {"capabilities": []}, {"historical_replay": True, "capability_options": []}):
            self.assertIsNone(views.freeze_scope(spec))
        marker = {"discovery_policy": views.DISCOVERY_POLICY}
        with patch("gx_core.p0.catalog_index.installed_methods", side_effect=AssertionError("unexpected expansion")):
            for key in ("capability_options", "capabilities", "capability_collections"):
                self.assertEqual(views.freeze_scope({**marker, key: []})["capability_options"], [])
        self.assertTrue(research._same_goal({"capabilities": []}, {"capabilities": [], **marker}))
        self.assertFalse(research._same_goal({"capabilities": []}, {"capability_collections": [], **marker}))
        with self.assertRaises(SchemaInvalid):
            views.freeze_scope({"historical_replay": True, "capability_collections": ["cpu_analysis"]})

    def test_identical_method_names_require_an_explicit_provider_choice(self):
        rows = [dict(capability_version="index@1", provider=owner+"@1", provider_sha256=digest*64,
                     portable_sha256="a"*64, collections=["cpu_analysis"], cost_class="local_cpu")
                for owner, digest in (("vendor.a", "a"), ("vendor.b", "b"))]
        gpu = dict(rows[0], capability_version="other@1", cost_class="gpu")
        spec = {"capability_collections": ["cpu_analysis"]}
        with patch("gx_core.p0.catalog_index.installed_methods", return_value=[*rows, gpu]):
            with self.assertRaisesRegex(SchemaInvalid, "Multiple providers"):
                views.freeze_scope(spec)
            named = {"method": "index@1", "provider": "vendor.a@1", "digest": "a"*64}
            expected = [{**named, "provider_digest": "a"*64}]
            self.assertEqual(views.freeze_scope({**spec, "capability_options": [named]})["capability_options"], expected)
            standalone_request = {"discovery_policy": views.DISCOVERY_POLICY, "capability_options": [named]}
            self.assertEqual(views.freeze_scope(standalone_request)["capability_options"], expected)
            wrong_pin = {**named, "provider_digest": "c"*64}
            self.assertEqual(views.freeze_scope({**spec, "capability_options": [wrong_pin]})["capability_options"], [wrong_pin])
            absent = {"method": "absent@1", "digest": "d"*64}
            frozen = views.freeze_scope({**standalone_request, "capability_options": [absent]})
            self.assertEqual((frozen["capability_options"], frozen["unavailable_options"]), ([], [absent]))

    def test_scoped_selection_binds_saved_package_bytes(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        grant = dict(evidence_policy="gx.evidence/6.2.4", research_effort="quick", research_modules=[],
                     capabilities=[], plugins=[], capability_collections=["public_sources"])
        s = self.enterContext(Scenario(directory, spec_updates=grant))
        method = "scholarly-search@1"
        brief = s.brief(s.lead)
        self.assertNotIn("available_options", brief["capability_view"])
        tools = {t["name"] for t in s.roots._tools_for(s.roots._binding(s.lead.thread))}
        self.assertIn("gx_select_capability", tools)
        discovery = s.call(s.lead, "gx_query_catalog", {"query": "Crossref", "kind": "all"}, "discover")
        self.assertIn(method, [r["reference"] for r in discovery["items"] if r["status"] == "selectable"])
        self.assertGreaterEqual(brief["capability_view"]["option_count"], 1)
        frozen = json.loads(s.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (s.run_id,)).fetchone()[0])
        choice = views.goal_options(frozen["inputs"])[method]
        from gx_core.capabilities import registry
        self.assertIsNone(registry.asset_row(s.conn, "scholarly-search", "1"))
        with self.assertRaises(VersionImmutable):
            views.change(s.conn, s.run_id, enable=method, provider=choice["provider"], expected_digest=choice["digest"],
                         expected_provider_digest="0"*64, expected_revision=0, request_id="wrong-provider")
        self.assertIsNone(registry.asset_row(s.conn, "scholarly-search", "1"))
        self.assertEqual(views.current(s.conn, s.run_id)["active"], {})
        select = dict(action="enable", revision=0, method=method)
        with self.assertRaises(ScopeDenied):
            s.call(s.lead, "gx_select_capability", dict(select, method="pcna-archive-fetch@1"), "outside")
        chosen = s.call(s.lead, "gx_select_capability", select, "select")
        self.assertEqual(chosen["active"], {method: choice["digest"]})
        self.assertEqual(s.call(s.lead, "gx_select_capability", select, "select"), chosen)

    def test_successor_keeps_parent_choices_without_reexpanding_installation(self):
        from acceptance.v622.test_close import close_decision
        from gx_core.p0 import conversation, successor

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        grant = dict(evidence_policy="gx.evidence/6.2.4", research_effort="deep", research_modules=[],
                     capabilities=[], plugins=[], capability_collections=["public_sources"])
        s = self.enterContext(Scenario(directory, spec_updates=grant))
        s.call(s.lead, "gx_query_catalog", {"kind": "all"}, "first")
        s.call(s.lead, "gx_decide_revision", {"decision": close_decision()}, "close")
        s.advance()
        def inputs(rid):
            row = s.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (rid,)).fetchone()
            return json.loads(row[0])["inputs"]
        expected = views.goal_options(inputs(s.run_id))
        self.assertIn("scholarly-fetch@1", expected)
        for options, choices in (({}, expected), ({"parent_method_options": []}, {})):
            mid = "follow-" + str(len(choices))
            text = "Inspect the new comparison using its own evidence."
            conversation.send(s.conn, s.run_id, text, mid, successor=options)
            child = successor.request(s.conn, s.state, s.run_id, mid, text, options, native_owner=True)
            frozen = inputs(child["run_id"])
            self.assertEqual(frozen["discovery_policy"], views.DISCOVERY_POLICY)
            self.assertEqual(views.goal_options(frozen), choices)
            self.assertEqual(frozen["method_scope"]["capability_collections"], [])
