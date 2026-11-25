"""The experiment's first-round capacity is independent of the full Run budget."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from gx_core.artifacts import ArtifactService
from gx_core.errors import BudgetExceeded, SchemaInvalid
from gx_core.p0 import v6_decisions, v6_goal
from gx_core.persistence import store
from tests.test_v6_decisions import bind_native_fixture, branch, draft


class OpeningBudget(unittest.TestCase):
    def test_opening_ceiling_expires_on_first_committed_head(self):
        with tempfile.TemporaryDirectory() as directory, closing(store.open_store(Path(directory) / "gx.db")) as conn:
            spec = {"project": "opening", "mode": "goal", "objective": "Compare the available observations",
                    "input_files": [], "capabilities": [], "initial_job_limit": 0}
            rid = v6_goal.start(conn, spec, run_key="opening", current=None)["run_id"]

            def budget():
                frozen = conn.execute("SELECT snapshot_json FROM research_contexts WHERE run_id=? "
                                      "ORDER BY version DESC LIMIT 1", (rid,)).fetchone()
                return json.loads(frozen[0])["budget"]

            self.assertEqual(budget()["initial_job_limit"], 0)
            call = bind_native_fixture(conn, rid, directory)
            node = {"op": "add_node", "node": "local:measure", "branch": "local:sample",
                    "capability": "test-meter@1", "question": "How much did the reading change?",
                    "inputs": [], "depends_on": [], "parameters": {}}
            with self.assertRaisesRegex(BudgetExceeded, "first graph"):
                v6_decisions.adopt(conn, **call, decision=draft(branch(), node))
            self.assertIsNone(v6_decisions.head(conn, rid))
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
            v6_decisions.adopt(conn, **dict(call, call_id="smaller"), decision=draft(branch()))
            ref = ArtifactService(conn).put(run_id=rid, kind="observation", content='{"value":1}',
                media_type="application/json", origin="INGEST", put_by="fixture").artifact_ref
            v6_goal.record_observation(conn, rid, ref, source="new-measurement")
            self.assertIsNotNone(v6_goal.ensure_task(conn, rid))
            self.assertNotIn("initial_job_limit", budget())

    def test_invalid_opening_ceiling_has_no_run_side_effects(self):
        with tempfile.TemporaryDirectory() as directory, closing(store.open_store(Path(directory) / "gx.db")) as conn:
            for invalid in (True, -1, 0.5, None):
                with self.subTest(limit=invalid), self.assertRaisesRegex(SchemaInvalid, "initial_job_limit"):
                    v6_goal.start(conn, {"project": "opening", "mode": "goal", "objective": "Measure bias",
                                        "initial_job_limit": invalid}, run_key="invalid", current=None)
            self.assertEqual(conn.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
