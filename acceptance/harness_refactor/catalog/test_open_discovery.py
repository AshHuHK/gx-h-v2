"""Public research entry, scoped catalogue and new-evidence acceptance, with controlled native callbacks."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v622.test_close import CloseStudy
from acceptance.v62a.interleavings_fixture import draft
from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid, ScopeDenied
from gx_core.p0 import capability_views, cli, v6_goal
from tests.test_v6_decisions import branch

LOOKUP = "yeast-archive-lookup@1"
WATCH = [{"event": "goal_changed", "subject_ref": "run"}]


class OpenDiscovery(unittest.TestCase):
    def admit(self, question, options=None):
        if output := os.environ.get("GX_HARNESS_CATALOG_STATES"):
            root = Path(output)
            root.mkdir(parents=True, exist_ok=True)
            path = Path(tempfile.mkdtemp(prefix="catalog-", dir=root))
        else:
            path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        args = ["research", "--state", str(path / "state"), "--run-key", "question", "--goal", question]
        if options is not None:
            (path / "goal.json").write_text(json.dumps(options))
            args.extend(["--goal-config", str(path / "goal.json")])
        with patch.object(cli, "run_until_stopped", side_effect=lambda db, _, rid, timeout: cli.snapshot(db, rid)), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args), 0)
        s = self.enterContext(CloseStudy(path, reopen=True))
        self.inputs = json.loads(s.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (s.run_id,)).fetchone()[0])["inputs"]
        return s, s.next_actor("run")

    def test_unqualified_question_honors_site_authority_without_activating_every_method(self):
        s, _ = self.admit("資料中有何關於糖的直接測定？")
        choices = capability_views.goal_options(self.inputs)
        self.assertTrue(LOOKUP in choices, "No scoped archive query in frozen site choices")
        self.assertIn("literature-query@2", choices)
        self.assertNotIn("fixture-grid@1", choices)
        self.assertEqual(capability_views.current(s.conn, s.run_id)["active"], {})

    def test_literature_only_and_explicit_empty_scope_remain_narrow(self):
        for question, options, expected in [
                ("只查文獻。有哪些公開測量？", None, True), ("Use only the literature. Which studies have paired data?", None, True),
                ("What evidence exists?", {"capability_collections":[]}, False)]:
            with self.subTest(question=question):
                s, lead = self.admit(question, options)
                clauses = [{"kind":"only", "quote":question.split("。")[0].split(".")[0],
                            "targets":["public_sources"]}] if expected else []
                s.call(lead, "gx_query_catalog", {"kind":"all","pi_method_scope":clauses}, "overview")
                choices = capability_views.active_options(s.conn, s.run_id, self.inputs)
                self.assertNotIn(LOOKUP, choices)
                self.assertEqual("scholarly-search@1" in choices, expected)

    def test_literal_unquoted_operator_instruction_is_distinct_from_a_title(self):
        for question, scoped in (
                ("本題只查文獻，不要另查。有哪些觀察？", True),
                ("What does ‘use only the literature’ mean?", False),
                ("這篇摘要說「精度不足。只查文獻。結論未知」。它有資料支持嗎？", False),
                ("只查文獻？這種方法會漏掉什麼？", False),
                ("Literature and archival databases report different coverage. Why?", False)):
            with self.subTest(question=question):
                s, lead = self.admit(question)
                clauses = [{"kind":"only","quote":"本題只查文獻，不要另查","targets":["public_sources"]}] if scoped else []
                s.call(lead, "gx_query_catalog", {"kind":"all","pi_method_scope":clauses}, "overview")
                self.assertEqual(LOOKUP in capability_views.active_options(s.conn, s.run_id, self.inputs), not scoped)

    def test_project_method_ban_is_checked_against_the_actual_requested_version(self):
        grant = {"intent_items": [{"item_id": "methods", "item_type": "method_ban",
                                  "value": {"forbidden_capabilities": ["yeast-archive-lookup"]}}]}
        s, lead = self.admit("What observations are available?", grant)
        with self.assertRaises(ScopeDenied):
            s.call(lead, "gx_query_catalog", {"kind": "method", "reference": LOOKUP}, "restricted")
        with self.assertRaises(ScopeDenied):
            s.call(lead, "gx_select_capability", {"action": "enable", "method": LOOKUP, "revision": 0}, "select-ban")
        self.assertEqual(capability_views.current(s.conn, s.run_id)["active"], {})

    def test_installed_provider_cannot_award_itself_the_site_collection(self):
        from gx_core.p0 import catalog_index, harness_policy
        existing = next(r for r in catalog_index.installed_methods() if r["capability_version"] == LOOKUP)
        mimic = dict(existing, provider="example.reading@1", collections=["public_sources", "public_databases"])
        with patch.object(catalog_index, "installed_methods", return_value=[mimic]):
            scope = capability_views.freeze_scope({"harness_policy": harness_policy.VERSION,
                    "objective": "Does a title or quota confer a method?", "discovery_policy": capability_views.DISCOVERY_POLICY})
        self.assertEqual(scope["capability_options"], [])

    def test_opening_overview_accepts_legacy_text_method_with_current_cards(self):
        ref = "tl1a-coordinate-quality@1"
        s, lead = self.admit("What is the quality of these deposited coordinates?",
                             {"plugins": ["org.gx.protein@0.2.0"], "capabilities": [ref]})
        answer = s.call(lead, "gx_query_catalog", {"kind": "all", "query": ref, "pi_method_scope": []}, "overview")
        self.assertTrue(any(item["reference"] == ref and item["execution_granted"] for item in answer["items"]))
        self.assertIn("survey_event", answer)

    def test_cold_reopen_accepts_decision_on_new_evidence_without_overview(self):
        s, lead = self.admit("Are these observations comparable?")
        with self.assertRaises(SchemaInvalid) as denied:
            s.call(lead, "gx_decide_revision", {"decision": draft(watches=WATCH)}, "before-survey")
        self.assertEqual(denied.exception.details["reason"], "CATALOG_SURVEY_REQUIRED")
        s.call(lead, "gx_query_catalog", {"query":"quuxnomatchqzx", "pi_method_scope":[]}, "initial")
        s.call(lead, "gx_decide_revision", {"decision":draft(watches=WATCH)}, "wait")
        ref = ArtifactService(s.conn).publish_bytes(namespace=s.run_id + "/sources", kind="operator_source",
                rel_path="paired.json", data=b'{"note":"paired"}', run_id=s.run_id, media_type="application/json",
                provenance={"source": "controlled-test"}).artifact_ref
        v6_goal.record_observation(s.conn, s.run_id, ref, source="paired-observation")
        s.stack.close()
        with CloseStudy(s.directory, reopen=True) as other:
            lead = other.next_actor("run")
            from gx_core.p0 import v62_lead
            brief = other.brief(lead)
            self.assertIn(ref, [x["artifact_ref"] for x in brief["catalog_discovery"]["available_inputs"]])
            _, compact = v62_lead.deliver(other.conn, {**brief, "goal": brief["goal"] * 500}, 8192)
            self.assertIn("full_brief_ref", compact)
            self.assertEqual(compact["catalog_discovery"]["opening_survey_event"],
                             brief["catalog_discovery"]["opening_survey_event"])
            other.call(lead, "gx_decide_revision", {"decision":draft(watches=WATCH)}, "use-evidence")
            self.assertEqual(other.conn.execute("SELECT COUNT(*) FROM events WHERE kind='research.catalog.surveyed'").fetchone()[0], 1)

    def test_method_facts_separate_deployment_and_scientific_execution(self):
        from acceptance.harness_final.catalog.archive_snapshots import require_archive
        require_archive(self)
        sample = Path(self.enterContext(tempfile.TemporaryDirectory())) / "query.json"
        sample.write_text(json.dumps({"schema":"gx.yeast-archive-query/1","taxid":559292,"substrate":"lactose"}))
        s, lead = self.admit("Which yeast records are present?", {"input_files":[{"path":str(sample),"role":"query"}]})
        survey = s.call(lead, "gx_query_catalog", {"query":"酵母反應", "pi_method_scope":[], "required_outputs":["parent-records.json"]}, "initial")
        hit = next(item for item in survey["items"] if item["reference"]==LOOKUP)
        self.assertEqual(hit["capability_facts"], {"reference_only":False,"installed":True,"dependency_ready":True,"executed_in_run":False})
        archived = s.call(lead, "gx_query_catalog", {"reference":"gx-understand@legacy"}, "history")
        self.assertTrue(archived["capability_facts"]["reference_only"])
        s.call(lead, "gx_select_capability", {"action":"enable","revision":0,"method":LOOKUP}, "select")
        # A successful CPU Job performs the normal database lookup. It may legitimately return zero records.
        b = branch("archive")
        b["budget_limits"]["cpu_seconds"] = 120
        op = s.node(lead, "lookup", s.materials[0], branch_id=b["branch"], capability=LOOKUP)
        d = s.call(lead, "gx_decide_revision", {"decision":draft(b, op, watches=[{"event":"job_settled","subject_ref":"local:lookup"}])}, "query")
        settled = s.settle(d["aliases"]["local:lookup"])
        next_lead = s.next_actor("run")
        exact = s.call(next_lead, "gx_query_catalog", {"reference":LOOKUP}, "after-result")
        self.assertTrue(exact["capability_facts"]["executed_in_run"])
        self.assertEqual(exact["execution_health"]["job_id"], settled["job_id"])
        from gx_core.capabilities import manifest
        original = manifest.resolve_path
        def disconnected(path, **kwargs):
            if "/var/science-assets/" in path:
                return sample.parent / "unmounted"
            return original(path, **kwargs)
        with patch.object(manifest, "resolve_path", side_effect=disconnected):
            missing = s.call(next_lead, "gx_query_catalog", {"reference":LOOKUP}, "mount-lost")
        self.assertFalse(missing["capability_facts"]["dependency_ready"])
        self.assertTrue(missing["capability_facts"]["executed_in_run"])
        is_file = Path.is_file
        def missing_code(path):
            if path.parts[-3:] == ("org.gx.yeast-enzyme-archive", "1.0.0", "archive.py"):
                return False
            return is_file(path)
        with patch.object(Path, "is_file", missing_code):
            lost = s.call(next_lead, "gx_query_catalog", {"reference": LOOKUP}, "code-lost")
        self.assertEqual(lost["status"], "pinned_deployment_missing")
        self.assertFalse(lost["capability_facts"]["installed"])
        self.assertTrue(lost["capability_facts"]["executed_in_run"])
        from gx_core.p0 import catalog_index
        remaining = [row for row in catalog_index.installed_methods() if row["capability_version"] != LOOKUP]
        with patch.object(catalog_index, "installed_methods", return_value=remaining):
            removed = s.call(next_lead, "gx_query_catalog", {"reference": LOOKUP}, "package-uninstalled")
        self.assertEqual(removed["capability_facts"], lost["capability_facts"])
        if os.environ.get("GX_HARNESS_CATALOG_STATES"):
            from gx_core.p0 import v62_outputs
            receipt = {"transport": "controlled native callbacks", "run_id": s.run_id,
                       "candidate_state": str(s.state), "job_id": settled["job_id"], "node_id": settled["node_id"],
                       "result_ref": settled["result_ref"], "input_ref": s.materials[0],
                       "before_job": hit["capability_facts"], "after_job": exact["capability_facts"],
                       "missing_mount_injected_after_job": missing["capability_facts"],
                       "only_reference": archived["capability_facts"], "health": exact["execution_health"],
                       "named_outputs": v62_outputs.published_outputs(s.conn, s.run_id, settled["job_id"])}
            (s.directory / "catalog-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
