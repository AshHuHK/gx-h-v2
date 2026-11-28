"""Cross-module publication and metadata inspection through real GX leases and report receipts."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
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
            CREATE TABLE events(run_id,kind,event_id,payload_json,subject_kind,subject_id);
            CREATE TABLE artifacts(run_id,artifact_ref,kind,sha256,published);
            CREATE TABLE v62_modules(run_id,scope,branch_id);
            INSERT INTO v62_modules VALUES('r','module:other','other_branch');
            INSERT INTO events VALUES('r','v6.observation.added',5,'{"artifact_ref":"local","scope":"other_branch"}','source','other');
            INSERT INTO events VALUES('r','v6.observation.added',11,'{"artifact_ref":"future","scope":"run"}','source','future');
        """)
        frozen = {"materials": [], "nodes": []}
        task = {"run_id": "r", "event_cursor": 10, "visible_refs_json": '["full-brief","local","future"]'}
        with patch.object(v62b_handoff, "_publications", return_value=({}, {})), patch.object(v62b_handoff, "ArtifactService") as assets:
            for ref in ("full-brief", "local", "future", "unknown"):
                with self.subTest(ref=ref), self.assertRaises(ScopeDenied):
                    v62b_handoff.select_materials(conn, task, frozen, [ref], ["*"])
            assets.return_value.describe.assert_not_called()
            assets.return_value.verify.assert_not_called()

    def test_secondary_output_uses_its_own_completion_pin_and_published_nature(self):
        from gx_core.p0 import v6_decisions, v6_goal, v6_jobs
        from gx_core.persistence import store
        from tests.test_v62_outputs import METHOD, start_series_goal
        from tests.test_v6_decisions import bind_native_fixture, branch, draft
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        conn = self.enterContext(closing(store.open_store(directory / "gx.db")))
        rid, raw = start_series_goal(self, conn, directory, effort="deep")
        op = {"op": "add_node", "branch": "local:sample", "node": "local:both", "capability": METHOD,
              "inputs": [raw], "parameters": {}, "depends_on": [], "question": "What are the mean and forecast?"}
        change = draft({**branch(), "budget_limits": {"job_submissions": 1, "cpu_seconds": 60}}, op)
        change["reconsider_when"] = [{"event": "job_settled", "subject_ref": "local:both"}]
        accepted = v6_decisions.adopt(conn, **bind_native_fixture(conn, rid, directory), decision=change)
        node = accepted["aliases"]["local:both"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            v6_jobs.advance(conn, rid)
            row = conn.execute("SELECT status,result_ref FROM v6_nodes WHERE node_id=?", (node,)).fetchone()
            if row["status"] in v6_jobs.NODE_TERMINAL:
                break
            time.sleep(0.05)
        self.assertEqual(row["status"], "SUCCEEDED")
        tid = v6_goal.ensure_task(conn, rid)
        task = conn.execute("SELECT d.*,c.event_cursor,c.snapshot_json FROM v6_decision_tasks d "
                            "JOIN research_contexts c USING(context_ref) WHERE d.task_id=?", (tid,)).fetchone()
        frozen = json.loads(task["snapshot_json"])
        secondary, = [art for art in frozen["nodes"][0]["outputs"] if art["artifact_ref"] != row["result_ref"]]
        ref, nature = secondary["artifact_ref"], secondary["nature"]
        chosen = v62b_handoff.select_materials(conn, task, frozen, [ref], [nature], published_only=True)
        self.assertEqual([(m["artifact_ref"], m["nature"], m["role"]) for m in chosen], [(ref, nature, nature)])
        self.assertEqual(len(chosen[0]["publication"]["completion_sha256"]), 64)
        v62b_handoff.require_materials(conn, rid, {raw}, chosen)


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
        note = directory / "source.txt"
        note.write_text("The north gauge rose during the second visit.")
        files.append({"path": str(note), "role": "literature"})
        settings = {"research_effort": "deep", "plugins": [], "capabilities": [], "input_files": files,
                    "research_modules": ["data_inventory@1", "method_workflow_survey@1", "offline_literature_mechanism@1"]}
        self.s = self.enterContext(Scenario(directory / "study", spec_updates=settings))

    def test_report_transfers_only_its_confirmed_sources_and_contrary_findings(self):
        s = self.s
        first, contrary, unpublished = s.materials[:3]
        opened = s.open("initial", inputs=s.materials[:3], caps=[])
        worker = s.bind(opened["task_id"])
        for ref in s.materials[:3]:
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

    def test_lead_extraction_can_be_independently_reviewed_only_after_a_frozen_handoff(self):
        from gx_core.p0 import v6_goal
        s = self.s
        source = s.materials[3]
        segment = {"source_ref": source, "segment_id": "p1"}
        read = s.call(s.lead, "gx_read_source", segment, "lead-read")
        found = s.call(s.lead, "gx_extract_evidence", {
            **segment, "receipt_id": read["receipt_id"], "claim": read["text"], "quote": read["text"],
            "direction": "supports", "applicability": "Second visit to the north gauge.",
            "confidence_basis": "The registered note identifies the gauge and visit."}, "lead-extract")
        args = {**s.open_args("delegate", inputs=[source, found["evidence_ref"]], caps=[]),
                "module_id": "offline_literature_mechanism", "focus": "literature"}
        with self.assertRaises(ScopeDenied):
            s.call(s.lead, "gx_open_module", args, "not-yet-frozen")
        s.call(s.lead, "gx_decide_revision", {"decision": self.draft()}, "wait")
        v6_goal.record_observation(s.conn, s.run_id, s.materials[0], source="new-reading")
        lead = s.next_actor("run")
        opened = s.call(lead, "gx_open_module", args, "delegate")
        reviewer = s.bind(opened["task_id"])
        own = s.call(reviewer, "gx_read_source", segment, "reviewer-read")
        self.assertNotEqual(own["receipt_id"], read["receipt_id"])
        judged = s.call(reviewer, "gx_review_evidence", {"evidence_ref": found["evidence_ref"],
            "receipt_id": own["receipt_id"], "quote_sha256_seen": found["quote_sha256"],
            "verdict": "SUPPORTED", "note": "The gauge and visit agree with the original."}, "review")
        self.assertTrue(judged["accepted"])

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
