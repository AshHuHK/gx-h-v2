"""Run-scoped comparisons must have an actual operation and registered scientific target."""
from pathlib import Path
import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v622.test_close import CloseStudy, actual_packet, close_decision
from acceptance.v623.test_decision_action import comparison_open
from acceptance.v62a.interleavings_fixture import draft
from gx_core.errors import SchemaInvalid, ScopeDenied, StaleRevision
from gx_core.p0 import v6_decisions, v6_goal, v624_comparison
from tests.test_v6_decisions import branch


METHOD = "deposited-chain-geometry@2"
PLUGINS = {"plugins": ["org.gx.protein@0.3.0"], "capabilities": [METHOD], "research_modules": []}


class ComparisonInference(unittest.TestCase):
    def scenario(self, **updates):
        work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "comparison"
        return self.enterContext(CloseStudy(work, spec_updates={**PLUGINS, **updates}))

    def call(self, s, actor, body, key):
        return s.call(actor, "gx_decide_revision", {"decision": body}, key)

    def wake(self, s, material):
        v6_goal.record_observation(s.conn, s.run_id, material, source="input-review-" + material)
        return s.next_actor("run")

    def test_default_first_job_branch_leaves_comparison_card_available_for_scientific_opening(self):
        root = Path(__file__).resolve().parents[2]
        s = self.scenario(input_files=[
            {"role":"reference", "path":str(root / "gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif")},
            {"role":"workflow_spec", "path":str(root / "acceptance/v623/live/comparison-card.json")},
        ])
        job = {"op":"add_node", "inputs":[s.materials[0]], "capability": METHOD,
               "parameters":{"mask":"common_ca"}, "question":"Which coordinates are present?"}
        first = self.call(s, s.lead, {"rationale":"Inventory the supplied material.", "ops":[job]}, "inventory")
        self.assertNotIn("comparison", first)
        self.assertNotIn("comparison_action", first)
        queued, = s.rows("SELECT node_id FROM v6_nodes")
        s.settle(queued["node_id"])
        actor = s.next_actor("run")
        registered = self.call(s, actor, draft(branch("compare")), "scientific-opening")["comparison"]
        self.assertEqual(registered["method"]["parameters"], {"mask":"common_backbone"})
        self.assertEqual(registered["reference_refs"], [s.materials[0]])

    def test_multiple_registrations_choice_and_explicit_conflict_roll_back(self):
        s = self.scenario()
        first = self.call(s, s.lead, comparison_open(), "first")["comparison"]
        second_open = comparison_open()
        second_open["ops"] = [branch("second")]
        second_open["comparison_plan"].update(branch="local:second", reference_refs=[s.materials[0]])
        second = self.call(s, self.wake(s, s.materials[0]), second_open, "second")["comparison"]
        actor = self.wake(s, s.materials[1])
        choices = [first["comparison_id"], second["comparison_id"]]
        stop = dict(close_decision(), disposition="STOP", stop_reason="INSUFFICIENT_DATA")
        for body, field, reason in (
                (dict(stop, comparison_id="cmp_unknown"), "comparison_id", "comparison_not_found"),
                (stop, "comparison_id", "comparison_choice_required"),
                (dict(stop, comparison_id=choices[0], stop_reason=None), "stop_reason", "stop_reason_required"),
                (dict(draft({"op":"stop_branch", "branch":first["branch"], "reason":"Withdraw this protocol"}),
                      comparison_id=choices[0], disposition="MAINTAIN"), "disposition", "comparison_action_conflict")):
            if body.get("stop_reason", "") is None:
                del body["stop_reason"]
            with self.subTest(reason=reason), self.assertRaises(SchemaInvalid) as caught:
                self.call(s, actor, body, reason)
            self.assertEqual(caught.exception.details["field"], field)
            self.assertEqual(caught.exception.details["reason"], reason)
            if field == "comparison_id":
                self.assertEqual(set(caught.exception.details["options"]), set(choices))
        self.assertEqual(len(s.rows("SELECT 1 FROM v6_decisions")), 2)
        result = self.call(s, actor, dict(stop, comparison_id=choices[0]), "chosen")
        self.assertEqual(result["comparison_action"]["comparison_id"], choices[0])
        self.assertEqual(result["comparison_action"]["disposition"], "STOP")

    def test_branch_withdrawal_then_normal_or_administrative_closure(self):
        for reason in (None, "TIME_LIMIT", "BUDGET", "OPERATOR"):
            with self.subTest(reason=reason):
                s = self.scenario()
                first = self.call(s, s.lead, comparison_open(), "first")["comparison"]
                actor = self.wake(s, s.materials[0])
                action = self.call(s, actor, draft({"op":"stop_branch", "branch":first["branch"],
                                                     "reason":"Withdraw the comparator"}), "withdrawal")
                self.assertEqual(action["comparison_action"]["disposition"], "CHANGE")
                self.assertEqual(action["comparison_action"]["comparison_id"], first["comparison_id"])
                self.assertTrue(s.rows("SELECT 1 FROM events WHERE kind='v6.branch.stop_requested'"))
                close = close_decision()
                if reason:
                    close["stop_reason"] = reason
                done = self.call(s, self.wake(s, s.materials[1]), close, "close")
                self.assertNotIn("comparison_action", done)
                if reason:
                    self.assertEqual(done["endpoint_use"]["stop_reason"], reason)

    def test_selected_row_honors_auxiliary_input_even_when_projection_only_names_the_specimen(self):
        db = self.enterContext(closing(sqlite3.connect(":memory:")))
        db.executescript("""
            CREATE TABLE events(run_id, kind, subject_id, event_id, payload_json);
            CREATE TABLE p0_execution_manifests(job_id, admission_sha256, admission_json);
        """)
        method = {"capability":"method@1", "metric_id":"coverage", "unit":"1", "output_name":"result",
                  "parameters":{"setting":"frozen"}}
        binding = {"method":method, "reference_refs":["reference","map"], "material_role":"specimen",
                   "decision_ref":"open", "method_manifest_sha256":"pin"}
        task = {"run_id":"run","event_cursor":5,"goal_version":1}
        db.execute("INSERT INTO events VALUES('run','v621.material.assigned','specimen',3,?)",
                   (json.dumps({"goal_version":1,"decision_ref":"open"}),))
        rows = {}
        for job, (specimen, mapping) in enumerate((("reference","map"),("specimen","map"),("specimen","other-map"))):
            role = {"role":"specimen", "artifact_ref":specimen}
            admitted = {"inputs":[role, {"role":"mapping","artifact_ref":mapping}],"parameters":method["parameters"]}
            db.execute("INSERT INTO p0_execution_manifests VALUES(?,?,?)", (job,str(job),json.dumps(admitted)))
            ref = "metric_" + str(job) * 64
            rows[ref] = {"metric_ref":ref,"metric_id":"coverage","display":"0.1","unit":"1","inputs":[role],
                         "job_id":job, "admission_sha256":str(job), "output_name":"result",
                         "method":{"id":"method", "version":"1", "sha256":"pin"}}
        matched = v624_comparison._applicable_metrics(db, task, binding, rows)
        self.assertEqual(matched, set(list(rows)[:2]))

    def test_late_evidence_supersedes_nonclosing_comparison_before_branch_and_budget_change(self):
        s = self.scenario()
        first = self.call(s, s.lead, comparison_open(), "opening")["comparison"]
        actor = self.wake(s, s.materials[0])
        before = s.head()
        budget = s.rows("SELECT limits_json,spent_json,reserved_json FROM budgets ORDER BY budget_ref")
        decision = draft({"op": "stop_branch", "branch": first["branch"], "reason": "Withdraw this comparison."})
        prepare = v6_decisions.prepare
        late = {}
        def receive(*args, **kwargs):
            prepared = prepare(*args, **kwargs)
            late.update(v6_goal.record_observation(s.conn, s.run_id, s.materials[1], source="late-material"))
            return prepared

        with patch.object(v6_decisions, "prepare", side_effect=receive), self.assertRaises(StaleRevision) as caught:
            self.call(s, actor, decision, "superseded")
        self.assertEqual(caught.exception.details["reason"], "comparison_context_changed")
        self.assertEqual(caught.exception.details["event_id"], late["event_id"])
        self.assertEqual(s.head(), before)
        self.assertEqual(s.rows("SELECT limits_json,spent_json,reserved_json FROM budgets ORDER BY budget_ref"), budget)
        self.assertEqual(s.rows("SELECT status FROM v6_branches"), [{"status":"OPEN"}])
        self.assertEqual(len(s.rows("SELECT 1 FROM v6_decisions")), 1)
        fresh = s.next_actor("run")
        self.assertNotEqual(actor.task_id, fresh.task_id)
        self.assertGreaterEqual(actual_packet(s, fresh)["packet"]["trigger"]["through_cursor"], late["event_id"])
        accepted = self.call(s, fresh, decision, "fresh")
        self.assertEqual(accepted["comparison_action"]["disposition"], "CHANGE")

    def test_unrelated_work_wait_and_replacement(self):
        s = self.scenario()
        target, unrelated, wake_source = s.materials
        opening = comparison_open()
        opening["comparison_plan"]["reference_refs"] = [target]
        opening["ops"][0]["budget_limits"] = {"job_submissions": 3, "cpu_seconds": 150}
        first = self.call(s, s.lead, opening, "opening")["comparison"]
        actor = self.wake(s, target)
        remote = s.node(actor, "other-material", unrelated, branch_id=first["branch"],
                        capability=METHOD, parameters={"mask": "common_ca"})
        with self.assertRaises(SchemaInvalid) as conflict:
            self.call(s, actor, dict(draft(remote), comparison_id=first["comparison_id"]), "wrong-target")
        self.assertEqual(conflict.exception.details["reason"], "comparison_action_ambiguous")
        independent = self.call(s, actor, draft(remote, watches=[{"event": "job_settled",
                    "subject_ref": "local:other-material"}]), "unrelated")
        self.assertNotIn("comparison_action", independent)
        s.settle(independent["aliases"]["local:other-material"])
        waiting = self.call(s, s.next_actor("run"), draft(), "wait")
        self.assertNotIn("comparison_action", waiting)
        actor = self.wake(s, wake_source)
        new_method = s.node(actor, "replacement", target, branch_id=first["branch"], capability=METHOD,
                            parameters={"mask": "common_backbone"})
        result = self.call(s, actor, draft(new_method), "replace")
        self.assertEqual(result["comparison_action"]["disposition"], "CHANGE")
        self.assertEqual(s.settle(result["aliases"]["local:replacement"])["status"], "SUCCEEDED")

    def test_exact_material_method_rows_and_mixed_evidence(self):
        s = self.scenario()
        material, other = s.materials[:2]
        opening = comparison_open()
        opening["comparison_plan"]["reference_refs"] = [material]
        opening["ops"][0]["budget_limits"] = {"job_submissions": 4, "cpu_seconds": 180}
        measure = s.node(s.lead, "baseline", material, branch_id="local:route",
                         capability=METHOD, parameters={"mask": "common_ca"})
        opening["ops"].extend([measure, s.node(s.lead, "unrelated", other, branch_id="local:route",
                                                capability=METHOD, parameters={"mask": "common_ca"})])
        opening["reconsider_when"] = [{"event":"job_settled","subject_ref":"local:unrelated"}]
        first = self.call(s, s.lead, opening, "open")
        work = s.settle(first["aliases"]["local:baseline"])
        peer = s.settle(first["aliases"]["local:unrelated"])
        actor = s.next_actor("run")
        source = work["result_ref"]
        entry = next(x for x in actual_packet(s, actor)["packet"]["observations"] if x["artifact_ref"] == source)
        coverage = next(row for row in entry["endpoint_rows"] if row["id"] == "coverage")
        unrelated_source = peer["result_ref"]
        unrelated = next(x for x in actual_packet(s, actor)["packet"]["observations"]
                         if x["artifact_ref"] == unrelated_source)
        wrong = next(row for row in unrelated["endpoint_rows"] if row["id"] == "coverage")
        plan = s.node(actor, "repeat", material, branch_id=first["comparison"]["branch"],
                      capability=METHOD, parameters={"mask":"common_ca"})
        body = dict(draft(plan, watches=[{"event":"job_settled","subject_ref":"local:repeat"}]),
                    metric_refs=[unrelated_source + "#" + wrong["key"]])
        with self.assertRaises((SchemaInvalid, ScopeDenied)) as caught:
            self.call(s, actor, body, "wrong-row")
        self.assertEqual(caught.exception.details.get("reason"), "comparison_metric_mismatch")
        body.update(metric_refs=[source + "#" + coverage["key"]])
        action = self.call(s, actor, body, "matching-row")
        self.assertEqual(action["comparison_action"]["disposition"], "MAINTAIN")
        self.assertEqual(action["comparison_action"]["metric_refs"], action["endpoint_use"]["used_metric_refs"])
        self.assertEqual(action["comparison_action"]["comparison_id"], first["comparison"]["comparison_id"])
        self.assertIn(s.settle(action["aliases"]["local:repeat"])["status"], ("SUCCEEDED", "REUSED"))
        done = self.call(s, s.next_actor("run"), close_decision(), "normal-close")
        self.assertNotIn("comparison_action", done)
