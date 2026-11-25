"""Protein methods are selected per Run and the original installed pin remains usable."""
import json
import os
from contextlib import closing
from pathlib import Path
import tempfile
import time
import unittest

from gx_core.artifacts import ArtifactService

from gx_core import config
from gx_core.capabilities import shipped
from gx_core.errors import ScopeDenied
from gx_core.p0 import v6_decisions, v6_goal, v6_jobs, v6_method_context, v6_native
from gx_core.persistence import store
from gx_core.plugins.catalog import package_digest
from tests.test_v6_decisions import bind_native_fixture, branch, draft


PREVIOUS_PIN = "org.gx.protein@0.1.0-v6.1"
PREVIOUS_DIGEST = "ac5b5d5cf96e942bfc57dc8492aa56b77ad9ecee0716735a3e6338463630fce9"
GEOMETRY_TOOL = "gx_reevaluate_geometry"


class ProteinProvider(unittest.TestCase):
    def test_grant_precedes_direct_calls_as_well_as_tool_discovery(self):
        with tempfile.TemporaryDirectory() as tmp, closing(store.open_store(Path(tmp) / "gx.db")) as conn:
            spec = {"mode": "goal", "project": "other-domain", "objective": "Read a calibration report"}
            run = v6_goal.start(conn, spec, run_key="empty-catalog", current=None)["run_id"]
            brief = json.loads(conn.execute("SELECT snapshot_json FROM research_contexts WHERE run_id=?", (run,)).fetchone()[0])
            self.assertEqual(brief["capabilities"], [])
            self.assertNotIn(GEOMETRY_TOOL, {s["name"] for s in v6_native.tool_specs([], brief, conn=conn, run_id=run)})
            self.assertIsNone(v6_native.callback_for(GEOMETRY_TOOL, conn=conn, run_id=run))
            identity = bind_native_fixture(conn, run, Path(tmp))
            with self.assertRaises(ScopeDenied):
                v6_method_context.call_tool(conn, GEOMETRY_TOOL, {"measurement_refs": [], "criteria": {}},
                                            **identity, context_ref=brief.get("context_ref", "empty"))
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_exact_previous_package_identity_through_cold_pending_job(self):
        installed = config.HARNESS_ROOT / "plugins/installed/org.gx.protein/0.1.0-v6.1"
        self.assertEqual(package_digest(installed), PREVIOUS_DIGEST)
        structure = config.HARNESS_ROOT / "adapters/scientific/tl1a_geometry/data/2RE9.cif"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gx.db"
            with closing(store.open_store(path)) as conn:
                spec = {"mode": "goal", "project": "old-pin", "objective": "Inventory deposited positions",
                        "input_files": [{"role": "structure", "path": str(structure)}],
                        "plugins": [PREVIOUS_PIN], "capabilities": [shipped.QUALITY]}
                run = v6_goal.start(conn, spec, run_key="old-pin", current=None)["run_id"]
                raw = json.loads(conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run,)).fetchone()[0])
                provider = raw["inputs"]["capability_catalog"][0]["plugin"]
                self.assertEqual((provider["version"], provider["sha256"]), ("0.1.0-v6.1", PREVIOUS_DIGEST))
                call = bind_native_fixture(conn, run, Path(tmp))
                operation = dict(op="add_node", branch="local:sample", node="local:inventory",
                                 capability=shipped.QUALITY, inputs=[raw["files"][0]["artifact_ref"]],
                                 parameters={}, depends_on=[], question="How many coordinates are shared?")
                result = v6_decisions.adopt(conn, **call, decision=draft(
                    {**branch(), "budget_limits": {"job_submissions": 1, "cpu_seconds": 60}}, operation))
                node_id = result["aliases"]["local:inventory"]
                self.assertEqual(conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (run,)).fetchone()[0], 0)
            with closing(store.open_store(path)) as conn:
                end = time.monotonic() + 30
                while time.monotonic() < end:
                    v6_jobs.advance(conn, run)
                    state = conn.execute("SELECT status,job_id,result_ref FROM v6_nodes WHERE node_id=?", (node_id,)).fetchone()
                    if state["status"] in v6_jobs.NODE_TERMINAL:
                        break
                    time.sleep(.03)
                self.assertEqual(state["status"], "SUCCEEDED")
                first = tuple(state)
            with closing(store.open_store(path)) as conn:
                v6_jobs.advance(conn, run)
                final = conn.execute("SELECT status,job_id,result_ref FROM v6_nodes WHERE node_id=?", (node_id,)).fetchone()
                self.assertEqual(tuple(final), first)
                self.assertEqual(conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (run,)).fetchone()[0], 1)
                if out := os.environ.get("GX_V61_PROTEIN_RECEIPT_DIR"):
                    directory = Path(out)
                    directory.mkdir(parents=True, exist_ok=True)
                    budget = conn.execute("SELECT budget_ref FROM runs WHERE run_id=?", (run,)).fetchone()[0]
                    receipt = {
                        "test": "V6.1 opens new Goal explicitly selecting the historical V6 plugin",
                        "run_id": run, "selected_provider": provider, "decision_receipt": result,
                        "pending_jobs_at_first_shutdown": 0, "settled_job": state["job_id"],
                        "cold_job": final["job_id"], "node_id": node_id, "outcome": final["status"],
                        "artifact": ArtifactService(conn).describe(final["result_ref"]),
                        "account": store.budget_state(conn, budget), "model_calls": 0,
                    }
                    (directory / "old-pin-cold-pending.json").write_text(json.dumps(receipt, indent=2) + "\n")
