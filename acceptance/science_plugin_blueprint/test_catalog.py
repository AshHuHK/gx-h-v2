"""Exercise versioned discovery through the normal Goal and the public native callback."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from acceptance.v622.test_close import CloseStudy
from gx_core import config
from gx_core.p0 import cli

from acceptance.science_plugin_blueprint.catalog_probe import Scenario
from gx_core.errors import SchemaInvalid, ScopeDenied
from gx_core.p0 import capability_views, catalog_index, v62_catalog


class CatalogDiscovery(unittest.TestCase):
    def test_shortlist_uses_report_science_and_omits_opaque_storage_ids(self):
        update = {"researcher_messages": [{"finding": {"text":"Observed disagreement in rainfall",
                                                       "source_refs":["gxa_opaque"]},
                                           "gaps":["Missing radar coverage"],"next_steps":["Compare gauge calibration"]}]}
        words = set(catalog_index._context_words({"updates":[update],
                    "available_inputs":[{"name":"weather.json","role":"original","artifact_ref":"gxa_other"}]}))
        self.assertTrue({"disagreement","rainfall","radar","gauge","weather.json","original"} <= words)
        self.assertTrue({"opaque","other","gxa","source","refs"}.isdisjoint(words))

    def test_saved_recipe_preserves_named_slot_and_output_type(self):
        ports = catalog_index._outputs({"outputs": {"reader": [
            {"name": "measurement.json", "kind": "file:numeric", "nature": "analysis"}
        ]}})
        self.assertEqual(ports, {"reader.measurement.json": {"measurement.json", "file:numeric"}})

    def test_unrelated_broken_builtin_does_not_block_a_research_survey(self):
        work = Path(self.enterContext(TemporaryDirectory()))
        conf = work / "config"
        shutil.copytree(config.CONFIG_DIR, conf)
        # Damage one method's registration in a private release config.
        shipped = conf / "shipped.json"
        manifest = json.loads(shipped.read_bytes())
        manifest["capabilities"]["fixture-grid@1"] = "0" * 64
        shipped.write_text(json.dumps(manifest))
        self.enterContext(patch.object(config, "CONFIG_DIR", conf))
        self.enterContext(patch.object(cli, "run_until_stopped",
            side_effect=lambda conn, _, rid, timeout: cli.snapshot(conn, rid)))
        for effort in ("quick", "deep"):
            with self.subTest(effort=effort):
                directory = work / effort
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(output):
                    status = cli.main(["research", "--state", str(directory / "state"), "--run-key", "new",
                                       "--research-effort", effort, "--goal", "Find published rainfall measurements."])
                self.assertEqual(status, 0, output.getvalue())
                with CloseStudy(directory, reopen=True) as s:
                    lead = s.next_actor("run")
                    found = s.call(lead, "gx_query_catalog", {"kind":"all", "query":"Crossref", "pi_method_scope":[]}, "publications")
                    self.assertIn("scholarly-search@1", {r["reference"] for r in found["items"]})
                    self.assertIn("fixture-grid@1", found["unavailable_installed_metadata"])
                    with self.assertRaises(ScopeDenied):
                        s.call(lead, "gx_query_catalog", {"kind":"method", "reference":"fixture-grid@1"}, "bad")

    def test_new_quick_goal_exposes_catalog_and_preserves_selection_boundary(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        run = self.enterContext(Scenario(directory, spec_updates={
            "evidence_policy": "gx.evidence/6.2.4", "research_effort": "quick", "research_modules": [],
            "capabilities": [], "plugins": [], "input_files": [], "capability_collections": ["public_sources"],
        }))

        def query(key, **args):
            return run.call(run.lead, "gx_query_catalog", args, key)

        tools = {item["name"] for item in run.roots._tools_for(run.roots._binding(run.lead.thread))}
        self.assertIn("gx_query_catalog", tools)
        self.assertNotIn("gx_open_research_module", tools)
        query("survey", limit=2)
        en = query("crossref", query="Crossref")["items"]
        self.assertIn("scholarly-search@1", {r["reference"] for r in en})
        traditional = query("trad", query="文獻", kind="research")["items"]
        simplified = query("simp", query="文献", kind="research")["items"]
        self.assertEqual([r["reference"] for r in traditional], [r["reference"] for r in simplified])
        self.assertIn("scholarly-fetch@1", {r["reference"] for r in traditional})
        rows = query("outputs", query="literature", kind="method", required_outputs=["file:journal_article"])["items"]
        read = next(r for r in rows if r["reference"] == "scholarly-fetch@1")
        search = next(r for r in rows if r["reference"] == "scholarly-search@1")
        self.assertEqual(read["output_fit"], "declared")
        self.assertEqual(search["required_outputs_missing"], ["file:journal_article"])
        self.assertLess(rows.index(read), rows.index(search))
        self.assertEqual(read["status"], "selectable")
        self.assertFalse(read["execution_granted"])
        self.assertEqual(read["input_coverage"]["status"], "no_visible_materials")
        self.assertIn("match_reason", read)
        lead = run.lead
        task = dict(run.conn.execute("SELECT * FROM v6_decision_tasks WHERE task_id=?",
                                     (lead.task_id,)).fetchone())
        # A query can locate a method through a subject category. Use its result as
        # grounds before making any explicit kind=method call for that document.
        for method, args in ((read, {}), (search, {"kind": "research"})):
            card = query("cross-card-" + method["reference"], reference=method["reference"],
                         provider=method["provider"], **args)
            self.assertEqual(card["kind"], "method")
            ref = card["source_ref"]
            self.assertEqual({ref}, v62_catalog.method_citations(
                run.conn, task, lead.lease_id, lead.thread, lead.turn, {ref}))
            self.assertFalse(v62_catalog.method_citations(
                run.conn, task, lead.lease_id, lead.thread, "unrelated-turn", {ref}))
        detail = query("ref", kind="method", reference=read["reference"], provider=read["provider"])
        self.assertEqual(detail["spec"]["portable_sha256"], read["digest"])
        self.assertIn("source_ref", detail)
        self.assertNotIn("entry_script", detail["spec"])

        legacy = query("legacy", reference="gx-understand@legacy")
        self.assertEqual(legacy["status"], "reference_only")
        self.assertFalse(legacy["execution_granted"])
        self.assertNotIn("source_ref", legacy)
        self.assertEqual(query("nothing", query="never-a-match-zqxvwx", kind="method")["items"], [])
        self.assertEqual(capability_views.current(run.conn, run.run_id)["active"], {})
        self.assertEqual(run.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        with self.assertRaises(ScopeDenied):
            run.call(run.lead, "gx_select_capability", {"action": "enable", "revision": 0,
                                                        "method": "yeast-reaction-index@2"}, "outside")
        with self.assertRaises(SchemaInvalid) as conflict:
            query("ambiguous", kind="method", reference="tl1a-geometry@1")
        candidates = conflict.exception.details["candidates"]
        self.assertGreater(len({c["provider"] for c in candidates}), 1)
        resolved = query("ambiguity-resolved", kind="method", reference="tl1a-geometry@1", provider=candidates[0]["provider"])
        self.assertEqual(resolved["status"], "catalog_only")
        self.assertEqual(resolved["provider"], candidates[0]["provider"])


if __name__ == "__main__":
    unittest.main()
