"""Cross-module publication and metadata inspection through real GX leases and report receipts."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from gx_core.artifacts import ArtifactService
from gx_core.errors import ScopeDenied
from gx_core.p0 import v62b_handoff

PRODUCT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PRODUCT))


class FrozenVisibility(unittest.TestCase):
    def test_internal_artifact_or_unpublished_observation_cannot_be_regranted(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE events(run_id,kind,event_id,payload_json);
            CREATE TABLE v62_modules(run_id,scope,branch_id);
            INSERT INTO v62_modules VALUES('r','module:other','other_branch');
            INSERT INTO events VALUES('r','v6.observation.added',5,'{"artifact_ref":"local","scope":"other_branch"}');
            INSERT INTO events VALUES('r','v6.observation.added',11,'{"artifact_ref":"future","scope":"run"}');
        """)
        frozen = {"materials": [], "nodes": []}
        task = {"run_id": "r", "event_cursor": 10, "visible_refs_json": '["full-brief","local","future"]'}
        with patch.object(v62b_handoff, "_publications", return_value=({}, {})), patch.object(v62b_handoff, "ArtifactService") as assets:
            for ref in ("full-brief", "local", "future", "unknown"):
                with self.subTest(ref=ref), self.assertRaises(ScopeDenied):
                    v62b_handoff.select_materials(conn, task, frozen, [ref], ["*"])
            assets.return_value.describe.assert_not_called()
            assets.return_value.verify.assert_not_called()


class PublishedHandoff(unittest.TestCase):
    def setUp(self):
        from acceptance.v62a.interleavings_fixture import Scenario, draft
        self.draft = draft
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        files = []
        for name in ("first", "contrary", "unpublished"):
            path = directory / (name + ".json")
            path.write_text(json.dumps({"label": name}))
            files.append({"path": str(path), "role": "material"})
        settings = {"research_effort": "deep", "plugins": [], "capabilities": [], "input_files": files,
                    "research_modules": ["data_inventory@1", "method_workflow_survey@1"]}
        self.s = self.enterContext(Scenario(directory / "study", spec_updates=settings))

    def test_report_transfers_only_its_confirmed_sources_and_contrary_findings(self):
        s = self.s
        first, contrary, unpublished = s.materials
        opened = s.open("initial", inputs=s.materials, caps=[])
        worker = s.bind(opened["task_id"])
        for ref in s.materials:
            s.call(worker, "gx_read_artifact", {"ref": ref}, ref)
        original_lead = s.lead
        s.call(original_lead, "gx_decide_revision", {"decision": self.draft()}, "wait")
        receipt = s.report(worker, first, findings=[
            {"text": "An observation.", "source_refs": [first]},
            {"text": "A contrary observation.", "kind": "counterevidence", "source_refs": [contrary]},
        ])
        s.next_actor("run")
        reference = receipt["report_ref"]
        next_open = s.open("follow-up", inputs=[reference], caps=[], round=2)
        self.assertEqual(set(next_open["effective_grant"]["input_refs"]), {reference, first, contrary})
        second = s.bind(next_open["task_id"])
        self.assertEqual(set(s.brief(second)["evidence_refs"]), {reference, first, contrary})
        report = s.call(second, "gx_read_artifact", {"ref": reference}, "read-report")
        self.assertEqual(report["findings"][1]["source_refs"], [contrary])
        self.assertEqual(s.call(second, "gx_read_artifact", {"ref": contrary}, "contrary"), {"label": "contrary"})
        with self.assertRaises(ScopeDenied):
            s.call(second, "gx_read_artifact", {"ref": unpublished}, "private-read")

    def test_exact_public_catalog_read_is_citeable_and_grants_no_execution(self):
        s = self.s
        payload = {**s.open_args("survey", inputs=[], caps=[]), "module_id": "method_workflow_survey", "focus": "model"}
        opened = s.call(s.lead, "gx_open_module", payload, "survey")
        actor = s.bind(opened["task_id"])
        query = {"kind": "method", "reference": "fixture-grid@1"}
        card = s.call(actor, "gx_query_catalog", query, "catalog")
        self.assertEqual(card, s.call(actor, "gx_query_catalog", query, "redelivered"))
        self.assertEqual(card["spec"]["capability_version"], query["reference"])
        self.assertEqual(s.brief(actor)["allowed_method_ids"], [])
        self.assertFalse({"entry", "required_data", "weights", "provider_closure", "content_artifact_ref"} & card["spec"].keys())
        self.assertEqual(ArtifactService(s.conn).describe(card["source_ref"])["kind"], "v62_public_method_spec")
        self.assertEqual(s.call(actor, "gx_read_artifact", {"ref": card["source_ref"]}, "read-card"), card["spec"])
        with self.assertRaises(ScopeDenied):
            s.call(actor, "gx_read_artifact", {"ref": s.materials[0]}, "unassigned")
        with self.assertRaises(ScopeDenied):
            s.call(actor, "gx_decide_revision", {"decision": self.draft()}, "execute")
        log = s.rows("SELECT payload_json FROM events WHERE run_id=? AND kind='v62.module.tool_completed' "
                     "AND json_extract(payload_json,'$.tool')='gx_query_catalog'", s.run_id)
        self.assertEqual([json.loads(row["payload_json"])["artifact_refs"] for row in log], [[card["source_ref"]]])
        self.assertIn(card["source_ref"], s.report(actor, card["source_ref"])["source_refs"])


if __name__ == "__main__":
    unittest.main()
