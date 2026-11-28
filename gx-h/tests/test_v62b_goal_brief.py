"""Verify what reaches the native Lead, including a paginated decision packet."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from gx_core.artifacts import ArtifactService
from gx_core.native_release import native_state
from gx_core.p0 import cli, v6_goal, v6_native, v621_policy, v62_lead
from gx_core.persistence import store


class ResearchBrief(unittest.TestCase):
    def brief(self, effort):
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(native_state(state))
        conn = self.enterContext(closing(store.open_store(state / "gx.db")))
        spec = {"mode": "goal", "project": "brief-scope", "objective": "Compare two reference readings.",
                "research_effort": effort, "capabilities": []}
        if effort != "quick":
            spec.update(research_modules=[], evidence_policy=v621_policy.LEGACY, historical_replay=True)
        rid = cli.start(conn, state, spec, run_key="capture")["run_id"]
        (frozen,) = conn.execute("SELECT snapshot_json FROM research_contexts WHERE run_id=?", (rid,)).fetchone()
        return conn, json.loads(frozen)

    def test_quick_retains_the_direct_wire_vocabulary(self):
        conn, brief = self.brief("quick")
        self.assertNotIn("research_evidence_version", brief)
        tools = {t["name"]: t["inputSchema"]["properties"] for t in
                 v6_native.tool_specs([], brief, conn=conn, run_id=brief["run_id"])}
        self.assertNotIn("gx_open_module", tools)
        self.assertNotIn("gx_read_source", tools)
        self.assertNotIn("prediction", tools["gx_stage_claim"])
        self.assertNotIn("convergence", tools["gx_decide_revision"]["decision"]["properties"])

    def test_recent_scoped_job_failure_survives_a_compact_lead_delivery(self):
        conn, brief = self.brief("deep")
        detail = {"kind": "archive_exclusion", "entry_id": "5E0U", "reason_code": "pcna_assembly_mismatch",
                  "summary": "The three observed PCNA chains do not share a deposited assembly operation."}
        failed = {"node_id": "node_f", "job_id": "job_f", "branch_id": "lead", "status": "FAILED",
                  "failure": detail}
        brief["nodes"] = [{"node_id": "earlier", "commission": {"question": "x" * 10000}}, failed]
        brief["observations"] = [{"node_id": "node_f", "event": "v6.node.settled", "status": "FAILED"}]
        _, delivery = v62_lead.deliver(conn, brief, 4096)
        self.assertIn("full_brief_ref", delivery)
        self.assertEqual(delivery["nodes"], [failed])

    def test_small_deep_brief_delivers_the_actual_decision_options_and_full_original(self):
        conn, brief = self.brief("deep")
        self.assertEqual(brief["research_evidence_version"], "gx.research.v6.2b.1")
        self.assertEqual(brief["research_progress"]["phase"], "exploring")
        brief["goal"] += " Additional calibration context." * 300
        full, delivered = v62_lead.deliver(conn, brief, 4096)
        self.assertEqual(delivered["research_progress"]["decision_options"],
                         brief["research_progress"]["decision_options"])
        self.assertEqual(delivered["research_evidence_version"], brief["research_evidence_version"])
        wire = v6_native.PREFIX + store._j(delivered)
        self.assertLessEqual(len(wire.encode()), 4096)
        self.assertEqual(ArtifactService(conn).read_json(full["full_brief_ref"]), brief)
        self.assertNotIn("gx_read_source", {t["name"] for t in
                         v6_native.tool_specs([], delivered, conn=conn, run_id=brief["run_id"])})
        self.assertIn("convergence", v6_native.decision_tool()["inputSchema"]["properties"]["decision"]["properties"])

    def test_report_watch_matches_only_the_correct_assigned_module(self):
        wake = {"kind":"v62.module.wake", "subject_id":"round:1",
                "payload_json":json.dumps({"scope":"run","module_instance_ids":["m1"]})}
        for subject in ("run","m1","module:m1"):
            self.assertTrue(v6_goal._matches(wake,[{"event":"module_report","subject_ref":subject}],"r"))
        self.assertFalse(v6_goal._matches(wake,[{"event":"module_report","subject_ref":"module:unrelated"}],"r"))
