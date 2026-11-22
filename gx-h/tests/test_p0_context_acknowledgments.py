"""A geometry planning Root may also acknowledge its exact assigned objective and Job limit."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gx_core.errors import SchemaInvalid
from gx_core.jobs.service import JobService
from gx_core.native_release import native_state
from gx_core.orchestration import reactor
from gx_core.p0 import cli, intent, science_inputs
from gx_core.p0.driver import context
from gx_core.persistence import store
from tests.test_tl1a_geometry import OfflineIngress, product_for, submit_peer


class PlanningAcknowledgments(unittest.TestCase):
    def test_required_coordinate_source_and_optional_current_context(self):
        with tempfile.TemporaryDirectory() as directory, native_state(Path(directory)) as state, closing(
                store.open_store(state.db_path)) as db, patch.object(cli, "Driver", OfflineIngress):
            run = cli.start(db, Path(directory), cli.geometry_spec())["run_id"]
            for (job,) in db.execute("SELECT job_id FROM jobs WHERE run_id=?", (run,)).fetchall():
                JobService(db).wait(job, timeout_seconds=15)
            reactor.lazy(context(db), run)
            reactor.drive_science(context(db), run)
            planner = db.execute("SELECT * FROM tasks WHERE run_id=? AND stage='proposal'", (run,)).fetchone()
            intent.stage_input(db, run, "unassigned", {"kind":"change","items":[{
                "item_id":"other-limit","item_type":"resource_limit","expected_version":0,
                "value":{"resource":"gpu_minutes","max":0}}]})
            intent.accept_input(db, run, "test-main", "turn-other-limit", "unassigned")
            acknowledgments = [{"item_id": key, "version": 1, "reason": message} for key, message in (
                ("coordinates", "Use the deposited structure."), ("objective", "Compare the requested atom selections."),
                ("job-limit", "Stay within the authorized CPU Job count."))]
            inputs = store.loads(planner, "input_json")
            self.assertEqual(set(inputs["intent_items"]), {"coordinates"})
            task = {**dict(planner), "input": inputs}
            self.assertEqual(set(science_inputs.task_context(db,run,task["task_id"])["intent"]),
                             {"coordinates","objective","job-limit"})
            for changes in (acknowledgments[1:], [*acknowledgments, acknowledgments[0]], [*acknowledgments,
                    {**acknowledgments[-1], "item_id":"not-in-this-project"}], [*acknowledgments,
                    {**acknowledgments[-1], "item_id":"other-limit"}],
                    [*acknowledgments[:-1], {**acknowledgments[-1], "version":0}]):
                with self.subTest(replies=changes), self.assertRaises(SchemaInvalid):
                    science_inputs.validate_product(db, task, {"intent_responses":changes}, [])
            science_inputs.validate_product(db,task,{"intent_responses":acknowledgments[:1]}, [])
            def live_shape(*args):
                return {**product_for(*args), "intent_responses":acknowledgments}
            with patch("tests.test_tl1a_geometry.product_for",side_effect=live_shape):
                accepted = submit_peer(db, directory, run, planner)
            self.assertEqual(accepted["intent_responses"], acknowledgments)
            self.assertEqual(db.execute("SELECT result FROM submissions WHERE task_id=?",
                                       (planner["task_id"],)).fetchone()[0],"ACCEPTED")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?", (run,)).fetchone()[0],1)
