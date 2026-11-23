"""Role catalogs at the GX claim, the official host settings and the fenced callback."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gx_core.errors import ScopeDenied
from gx_core.hostobs import host_policy
from gx_core.orchestration.hooks import DEFAULT_HOOKS
from gx_core.p0 import schema
from gx_core.p0.roots import Roots, WORKER_INSTRUCTIONS, WORKER_TOOLS
from gx_core.persistence import records, store


class RoleTools(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.directory = Path(work.name)
        self.conn = store.open_store(self.directory / "gx.sqlite")
        self.addCleanup(self.conn.close)
        schema.install(self.conn)
        self.base = {"gx_task_context", "gx_read_artifact", "gx_submit_product"}
        self.compute = {"gx_capability_inspect", "gx_job_submit", "gx_job_status", "gx_wait_job"}

    @contextmanager
    def project(self, role, *, version="3", overrides=None, grid_tool=True):
        profile = self.directory / "profile.json"
        profile.write_text(json.dumps({"roles": {role: overrides or {}}}))
        with patch.dict(os.environ, {"GX_P0_PROFILE": str(profile), "GX_P0_PROVIDER": "openai",
                                     "GX_P0_TEST_PROVIDER": "", "GX_P0_TEST": "0"}):
            document, digest, host = host_policy.p0_host()
        profile.unlink()
        rid = records.create_run(self.conn, objective="Compare the frozen grid", project_ref="policy", actor="test",
                                 workflow_id="science_grid", workflow_version=version, host=host, agent_policy=document,
                                 pins={"agent_policy_sha256": digest})
        self.conn.execute("UPDATE runs SET status='ACTIVE' WHERE run_id=?", (rid,))
        task = records.create_task(self.conn, run_id=rid, actor="test", role=role, kind="research_round")
        store.transition(self.conn, machine="task", subject_id=task, to_state="READY", actor="test")
        extra = [{"name": "gx_grid_measurement"}] if grid_tool else []
        roots = Roots(self.conn, run_id=rid, cwd=self.directory, worker_tools=extra)
        # The domain graph is independently exercised by the science_grid acceptance run.
        with patch("gx_core.p0.roots.hooks_for_run", return_value=DEFAULT_HOOKS), \
                patch("gx_core.p0.roots.tempfile.gettempdir", return_value=self.directory):
            row = roots._claim(task)
            yield roots, row

    def check_catalog(self, roots, row, expected):
        settings = roots._settings(row, new=True)
        self.assertEqual(set(json.loads(row["tool_names_json"])), expected)
        brief = json.loads(row["brief"].split("\n", 1)[1])
        self.assertEqual(set(brief["gx_tools"]), expected)
        self.assertEqual({spec["name"] for spec in settings["dynamicTools"]}, expected)
        self.assertNotIn("baseInstructions", settings)
        return settings

    def test_frozen_roles_receive_only_available_evidence_reads_and_delivery(self):
        for role in ("evidence_reviewer", "planner"):
            with self.subTest(role=role), self.project(role) as (roots, row):
                settings = self.check_catalog(roots, row, self.base | {"gx_grid_measurement"})
                self.assertFalse(settings["config"]["agents.enabled"])
                self.assertFalse(settings["config"]["features.multi_agent"])
                self.assertFalse(settings["config"]["features.multi_agent_v2.enabled"])
                self.assertFalse(settings["config"]["features.multi_agent_v2_dynamic_tools"])
                self.assertNotIn("gx_job_submit", settings["developerInstructions"])
                tid = roots._bind(row, {"id": role, "cwd": row["cwd"]})
                roots.active_turns[tid] = "turn"
                with self.assertRaisesRegex(ScopeDenied, "does not expose"):
                    roots._call({"threadId": tid, "turnId": "turn", "callId": "denied",
                                 "tool": "gx_job_submit", "arguments": {}})
        with self.project("evidence_reviewer", grid_tool=False) as (roots, row):
            self.check_catalog(roots, row, self.base)

    def test_compute_roles_keep_the_tools_for_the_assigned_job(self):
        for role, extra in (("executor", set()), ("researcher", {"gx_job_cancel"})):
            with self.subTest(role=role), self.project(role) as (roots, row):
                settings = self.check_catalog(roots, row, self.base | self.compute | extra)
                self.assertIn("gx_wait_job", settings["developerInstructions"])
                self.assertFalse(settings["config"]["features.multi_agent_v2.enabled"])

    def test_explicit_review_computation_and_delegation_survive_profile_removal(self):
        assigned = sorted(self.base | self.compute)
        with self.project("evidence_reviewer", overrides={"tools": assigned, "multi_agent": True}) as (roots, row):
            self.check_catalog(roots, row, set(assigned))
            reopened = Roots(self.conn, run_id=roots.run_id, cwd=self.directory)
            settings = reopened._settings(reopened._row(row["root_key"]), new=False)
            self.assertTrue(settings["config"]["agents.enabled"])
            self.assertTrue(settings["config"]["features.multi_agent_v2.enabled"])
            self.assertTrue(settings["config"]["features.multi_agent_v2_dynamic_tools"])
            self.assertIn("direct native children", settings["developerInstructions"])
            self.assertNotIn("dynamicTools", settings)
        with self.assertRaisesRegex(ValueError, "multi_agent must be a boolean"), self.project(
                "planner", overrides={"multi_agent": "false"}):
            pass
        with self.assertRaisesRegex(ValueError, "GX tools unavailable"), self.project(
                "executor", overrides={"tools": ["gx_submit_product", "invented_tool"]}):
            pass

    def test_original_v2_settings_and_worker_instructions(self):
        with self.project("evidence_reviewer", version="2") as (roots, row):
            names = {"gx_grid_measurement"} | {tool["name"] for tool in WORKER_TOOLS}
            settings = self.check_catalog(roots, row, names)
            self.assertEqual(settings["developerInstructions"], WORKER_INSTRUCTIONS + "\n" + row["brief"])
            self.assertNotIn("agents.enabled", settings["config"])
            self.assertTrue(settings["config"]["features.multi_agent_v2_dynamic_tools"])
            self.assertTrue(settings["config"]["features.multi_agent_v2.enabled"])
            self.assertTrue(settings["config"]["features.multi_agent"])


if __name__ == "__main__":
    unittest.main()
