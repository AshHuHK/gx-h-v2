"""Native Lead receives the exact location of a rejected graph operation."""
import json
from pathlib import Path
import tempfile
import unittest

from acceptance.v622.test_close import CloseStudy
from acceptance.v624.test_cheap_job import short
from acceptance.v62a.interleavings_fixture import branch
from gx_core.errors import SchemaInvalid


class DecisionPathTests(unittest.TestCase):
    def study(self):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "study"
        return self.enterContext(CloseStudy(path, spec_updates={
            "research_effort": "quick", "research_modules": [],
        }))

    def refused(self, study, body, call, field, operation, *, public_field=None):
        with self.assertRaises(SchemaInvalid) as err:
            study.call(study.lead, "gx_decide_revision", {"decision": body}, call)
        error = err.exception
        self.assertEqual(error.side_effect_status, "NONE")
        self.assertIn(field, error.message)
        self.assertIn(operation, error.message)
        self.assertEqual(error.details["field"], field)
        self.assertEqual(error.details["operation"], operation)
        row, = study.rows("""SELECT payload_json FROM events WHERE kind='v624.decision.rejected'
                             ORDER BY event_id DESC LIMIT 1""")
        self.assertEqual(json.loads(row["payload_json"])["field"], public_field or field)
        self.assertIsNone(study.head())
        self.assertEqual(study.rows("SELECT job_id FROM jobs"), [])
        return error

    def test_stop_condition_belongs_only_to_open_branch(self):
        study = self.study()
        body = short(study)
        route = branch("source")
        body["ops"].insert(0, route)
        stop = route.pop("stop_condition")
        self.refused(study, body, "missing-stop", "ops[0].stop_condition", "open_branch")
        route["stop_condition"] = stop
        body["ops"][1]["stop_condition"] = stop
        self.refused(study, body, "extra-stop", "ops[1].stop_condition", "add_node")
        del body["ops"][1]["stop_condition"]
        body["ops"][1]["private_note"] = "a researcher-provided field"
        self.refused(study, body, "private-field", "ops[1].private_note", "add_node",
                     public_field="ops[1]")
        del body["ops"][1]["private_note"]
        route["budget_limits"]["cpu_seconds"] = 60
        self.assertTrue(study.call(study.lead, "gx_decide_revision", {"decision": body}, "corrected")["event_id"])

    def test_unknown_budget_unit_lists_rejected_and_supported_units(self):
        study = self.study()
        route = branch("source")
        route["budget_limits"].update(worker_tasks=2, wall_hours=1, wall_seconds=2)
        body = short(study)
        body["ops"].insert(0, route)
        error = self.refused(study, body, "bad-unit", "ops[0].budget_limits.wall_hours", "open_branch",
                             public_field="ops[0].budget_limits")
        self.assertEqual(error.details["unknown_units"], ["wall_hours", "wall_seconds"])
        self.assertIn("cpu_seconds", error.details["allowed_units"])
        self.assertIn("worker_tasks", error.details["allowed_units"])
        for unit in error.details["unknown_units"] + error.details["allowed_units"]:
            self.assertIn(unit, error.message)
