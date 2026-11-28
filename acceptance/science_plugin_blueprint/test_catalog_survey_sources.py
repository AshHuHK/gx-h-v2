"""The next survey can still discover methods for earlier evidence and a released child finding."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from gx_core.native_release import native_state
from gx_core.p0 import cli, v6_goal, v62_sources
from gx_core.persistence import store
from tests import test_v62_sources
from tests.test_v6_decisions import draft


class SurveySources(unittest.TestCase):
    attach = test_v62_sources.SourceGoal.attach
    bind = test_v62_sources.SourceGoal.bind
    call = test_v62_sources.SourceGoal.call
    open = test_v62_sources.SourceGoal.open
    paragraph = test_v62_sources.SourceGoal.paragraph
    interpretation = staticmethod(test_v62_sources.SourceGoal.interpretation)

    def setUp(self):
        work = Path(self.enterContext(TemporaryDirectory()))
        self.directory = work / "state"
        (work / "article.nxml").write_text("<article><body><p>The second station remained dry.</p></body></article>")
        (work / "manual.md").write_text("# Records\n\nTransect notes.")
        config = work / "goal.json"
        config.write_text(json.dumps({"research_effort": "deep", "research_modules": ["offline_literature_mechanism@1"],
                          "input_files": [{"role": "literature", "path": "article.nxml"},
                                          {"role": "source", "path": "manual.md"}]}))
        with patch.object(cli, "run_until_stopped", side_effect=lambda conn, _, rid, timeout: cli.snapshot(conn, rid)), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["research", "--state", str(self.directory), "--run-key", "field-survey",
                "--goal", "Which accounts describe sampling conditions?", "--goal-config", str(config)]), 0)
        self.enterContext(native_state(self.directory))
        self.path = self.directory / "gx.db"
        self.conn = store.open_store(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.enterContext(patch("gx_core.p0.roots.tempfile.gettempdir", return_value=str(work)))
        self.run, task = self.conn.execute("SELECT run_id,task_id FROM v6_decision_tasks WHERE scope='run'").fetchone()
        self.attach()
        self.lead = self.bind(task)
        self.refs = {m["name"]: m["artifact_ref"] for m in self.call(self.lead, "gx_task_context", {})["materials"]}

    def test_released_sources_and_previous_reading_survive_later_update_and_restart(self):
        lead, paper = self.lead, self.refs["article.nxml"]
        _, author = self.open(lead, [paper])
        def extract(actor, claim):
            paragraph = self.paragraph(actor, "article.nxml", "p1")
            return self.call(actor, v62_sources.EXTRACT,
                             self.interpretation(paragraph, claim=claim))["evidence_ref"]
        lead_evidence = extract(lead, "The original records a dry station.")
        private = extract(author, "An alternative interpretation awaits independent verification.")
        shared = extract(author, "The second station remained dry.")
        self.call(lead, "gx_query_catalog", {"pi_method_scope": []})
        self.call(lead, "gx_decide_revision", {"decision": draft()})
        finding = {"kind": "counterevidence", "text": "The second station remained dry.",
                   "source_refs": [paper, shared]}
        report = self.call(author, "gx_submit_module_report", {
            "operation_key": "dry", "status": "COMPLETE", "findings": [finding], "gaps": [],
            "next_steps": [], "artifact_refs": []})
        next_lead = self.bind(v6_goal.ensure_task(self.conn, self.run))
        def catalog_context(actor):
            packet = self.call(actor, "gx_task_context", {})
            if packet.get("full_brief_ref"):
                packet = self.call(actor, "gx_read_artifact", {"ref":packet["full_brief_ref"]})
            return packet["catalog_discovery"]
        first = catalog_context(next_lead)
        inputs = {item["artifact_ref"] for item in first["available_inputs"]}
        self.assertLessEqual({paper, shared, lead_evidence, report["report_ref"]}, inputs)
        self.assertNotIn(private, inputs)
        updates = [msg for delta in first["updates"] for msg in delta.get("researcher_messages", [])]
        self.assertTrue(any(msg["finding"]["text"] == finding["text"] and msg["author_kind"] == "MODULE"
                            for msg in updates))
        self.call(next_lead, "gx_query_catalog", {})
        self.call(next_lead, "gx_decide_revision", {"decision": draft()})
        self.conn.close()
        self.conn = store.open_store(self.path)
        self.attach()
        v6_goal.record_observation(self.conn, self.run, self.refs["manual.md"], source="new-field-note")
        third = catalog_context(self.bind(v6_goal.ensure_task(self.conn, self.run)))
        after = {entry["artifact_ref"]:entry for entry in third["available_inputs"]}
        self.assertIn(shared, after)
        self.assertEqual(after[lead_evidence]["role"], "v62.source.extracted")
        self.assertNotIn(private, after)
        self.assertFalse(any(u.get("researcher_messages") for u in third["updates"]))


if __name__ == "__main__":
    unittest.main()
