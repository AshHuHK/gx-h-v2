"""The Lead's first short decision commissions a real CPU Job through the normal Goal."""
import json
from pathlib import Path
import tempfile
import unittest

from acceptance.v622.test_close import CloseStudy, actual_packet
from acceptance.v62a.interleavings_fixture import QUALITY, branch
from gx_core.capabilities import manifest, registry
from gx_core.errors import BudgetExceeded, SchemaInvalid, ScopeDenied
from gx_core.jobs.service import JobService


def short(s):
    return {"rationale": "Measure the deposited coordinates before selecting a comparison.",
            "ops": [{"op": "add_node", "capability": QUALITY, "inputs": [s.materials[0]],
                     "question": "How complete is the deposited record?"}]}


class FirstMeasurement(unittest.TestCase):
    def study(self, **updates):
        work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "run"
        return CloseStudy(work, spec_updates={"research_effort": "quick", "research_modules": [], **updates})

    def test_first_decision_wakes_on_actual_job_and_replays_once(self):
        with self.study() as s:
            submitted = short(s)
            actor = s.lead
            receipt = s.call(actor, "gx_decide_revision", {"decision": submitted}, "first")
            saved, = s.rows("SELECT request_json FROM v6_decisions")
            operations = json.loads(saved["request_json"])["decision"]["ops"]
            account, measurement = operations
            minimum = JobService._budget_amounts(manifest.effective_resources(
                registry.candidate(s.conn, QUALITY)["manifest"], {}))
            self.assertEqual(account, {"op": "open_branch", "branch": measurement["branch"],
                                       "budget_limits": minimum})
            self.assertEqual(measurement["question"], submitted["ops"][0]["question"])
            self.assertEqual((measurement["depends_on"], measurement["parameters"]), ([], {}))
            nid = receipt["aliases"][measurement["node"]]
            self.assertIn({"event": "job_settled", "subject_ref": nid}, receipt["reconsider_when"])
            result = s.settle(nid)
            next_lead = s.next_actor("run")
            self.assertNotEqual(next_lead.task_id, actor.task_id)
            self.assertIn(result["result_ref"], actual_packet(s, next_lead)["packet"]["evidence_refs"])
            self.assertEqual(s.call(actor, "gx_decide_revision", {"decision": submitted}, "first"), receipt)
            self.assertEqual(s.rows("SELECT job_id FROM jobs"), [{"job_id": result["job_id"]}])
            self.assertEqual(len(s.rows("SELECT 1 FROM v6_decisions")), 1)

    def test_explicit_science_branch_and_ambiguous_selection(self):
        with self.study() as s:
            opened = [branch("one"), branch("two")]
            draft = short(s)
            draft["ops"][:0] = opened
            with self.assertRaises(SchemaInvalid) as err:
                s.call(s.lead, "gx_decide_revision", {"decision": draft}, "ambiguous")
            self.assertEqual(err.exception.details["options"], ["local:one", "local:two"])
            self.assertIsNone(s.head())
            draft["ops"][-1]["branch"] = "local:two"
            del opened[1]["budget_limits"]
            receipt = s.call(s.lead, "gx_decide_revision", {"decision": draft}, "chosen")
            route, = s.rows("SELECT spec_json FROM v6_branches WHERE branch_id=?", receipt["aliases"]["local:two"])
            spec = json.loads(route["spec_json"])
            self.assertEqual(spec["hypothesis"], opened[1]["hypothesis"])
            self.assertEqual(spec["stop_condition"], opened[1]["stop_condition"])
            self.assertEqual(spec["budget_limits"]["job_submissions"], 1)

    def test_short_form_cannot_bypass_grant_initial_limit_or_supply_internal_branch(self):
        with self.study(initial_job_limit=0) as s:
            draft = short(s)
            with self.assertRaises(BudgetExceeded):
                s.call(s.lead, "gx_decide_revision", {"decision": draft}, "zero-grant")
            draft["ops"][0]["capability"] = "not-in-catalog@1"
            with self.assertRaises(ScopeDenied):
                s.call(s.lead, "gx_decide_revision", {"decision": draft}, "unknown-method")
            thin = {"op": "open_branch", "branch": "local:unattested", "budget_limits": {"job_submissions": 1}}
            with self.assertRaises(SchemaInvalid):
                s.call(s.lead, "gx_decide_revision", {"decision": {"rationale": "Open study.", "ops": [thin]}}, "thin")
            self.assertIsNone(s.head())
            self.assertFalse(s.rows("SELECT 1 FROM jobs"))

    def test_heavy_job_needs_explicit_funding_and_parent_budget_remains_binding(self):
        from unittest.mock import patch
        from gx_core import config

        with self.study() as s:
            draft = short(s)
            limits = config.limits()
            limits["budget_profiles"]["native_mvp"]["cpu_seconds"] = 29
            with patch.object(config, "limits", return_value=limits):
                with self.assertRaises(SchemaInvalid) as err:
                    s.call(s.lead, "gx_decide_revision", {"decision": draft}, "not-single-job")
                self.assertEqual(err.exception.details["field"], "ops[].branch")
                self.assertIsNone(s.head())
                route = branch("study")
                route["budget_limits"]["cpu_seconds"] = 29
                draft["ops"].insert(0, route)
                with self.assertRaises(BudgetExceeded):
                    s.call(s.lead, "gx_decide_revision", {"decision": draft}, "too-small")
                route["budget_limits"]["cpu_seconds"] = 1201
                with self.assertRaises(BudgetExceeded):
                    s.call(s.lead, "gx_decide_revision", {"decision": draft}, "over-parent")
                self.assertFalse(s.rows("SELECT 1 FROM v6_decisions"))
                route["budget_limits"]["cpu_seconds"] = 30
                reply = s.call(s.lead, "gx_decide_revision", {"decision": draft}, "funded")
            self.assertIn("local:study", reply["aliases"])
            self.assertEqual(len(s.rows("SELECT 1 FROM v6_nodes")), 1)

    def test_delegated_short_form_uses_its_granted_branch(self):
        with self.study(research_effort="medium", research_modules=["data_inventory@1"]) as s:
            opened = s.open("inventory", inputs=[s.materials[0]], caps=[QUALITY])
            actor = s.bind(opened["task_id"])
            body = short(s)
            outside = {**body["ops"][0], "inputs": [s.materials[-1]]}
            with self.assertRaises(ScopeDenied):
                s.call(actor, "gx_decide_revision", {"decision": {**body, "ops": [outside]}}, "ungranted-input")
            self.assertIsNone(s.head())
            accepted = s.call(actor, "gx_decide_revision", {"decision": body}, "delegated")
            allocated = s.brief(actor)["module"]["branch_id"]
            row, = s.rows("SELECT node_id,branch_id FROM v6_nodes")
            self.assertEqual(row["branch_id"], allocated)
            self.assertEqual(s.rows("SELECT branch_id FROM v6_branches"), [{"branch_id": allocated}])
            self.assertIn({"event": "job_settled", "subject_ref": row["node_id"]}, accepted["reconsider_when"])
