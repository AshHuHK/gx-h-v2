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
