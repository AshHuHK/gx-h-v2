"""Replay the effect of actual GX decisions independently of names and rationale wording."""
from contextlib import closing
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from acceptance.v62a.interleavings_fixture import branch, draft
from acceptance.v62b.study import receipts
from acceptance.v62c import materials, witness as original
from acceptance.v622.public.witness import _named_job
from acceptance.v622.test_close import CloseStudy, actual_packet
from acceptance.v623.live.action_witness import effect_for
from gx_core.errors import SchemaInvalid
from gx_core.p0 import v621_assignment, v623_comparison, v6_goal


METHOD = "deposited-chain-geometry@2"
PARAMS = {"mask": "common_backbone"}


class ComparisonLanguage(unittest.TestCase):
    def test_enacted_comparison_survives_renaming_negation_and_false_change_statement(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        corpus = folder / "materials"
        oracle = materials.prepare(corpus)
        cases = [
            ("P4", "MAINTAIN", "control", "reference coverage", "Coverage is not insufficient. Continue the comparison."),
            ("P4", "MAINTAIN", "route-a", "左岸法", "覆蓋並非不足，沿用原比較。"),
            ("K7", "CHANGE", "control", "reference coverage", "Coverage is not sufficient. Withdraw this comparison."),
            ("K7", "CHANGE", "route-a", "左岸法", "目前資料不足，不能維持原比較。"),
            ("K7", "MAINTAIN", "switch", "alternative procedure", "I changed the scientific route."),
        ]
        outcomes = []
        for i, (code, op, alias, family, reason) in enumerate(cases):
            with self.subTest(material=code, family=family, rationale=reason):
                work = folder / str(i)
                with CloseStudy(work, spec_updates={
                        "plugins": ["org.gx.protein@0.3.0"], "capabilities": [METHOD], "research_modules": []}) as study:
                    outcome = self.exercise(study, corpus, oracle[code], code, op, alias, family, reason)
                outcomes.append(outcome)
        self.assertEqual([r["enacted"] for r in outcomes], ["MAINTAIN", "MAINTAIN", "CHANGE", "CHANGE", "MAINTAIN"])
        self.assertEqual([r["agrees_with_registered_cutoff"] for r in outcomes], [True, True, True, True, False])
        if path := os.getenv("GX_CLOSEOUT_ACTION_RECEIPT"):
            Path(path).write_text(json.dumps({"transport": "scripted native tool calls", "paid_calls": 0,
                "cases": outcomes}, ensure_ascii=False, indent=2) + "\n")

    def exercise(self, s, corpus, specimen, code, op, alias, family, reason):
        reference = s.materials[0]
        v6_goal.record_observation(s.conn, s.run_id, reference, source="archive-source",
                                   acquisition=materials.parent_acquisition())
        route = dict(branch(alias), method_family=family, budget_limits={"job_submissions": 3, "cpu_seconds": 180})
        opening = dict(draft(route), rationale="Maintain the reference comparison if at least three fifths of the "
            "observed positions retain the shared backbone in the assigned specimen. Otherwise withdraw it.",
            comparison_plan={"branch": "local:"+alias, "material_role": "structure", "reference_refs": [reference],
              "method": {"capability": METHOD, "parameters": PARAMS, "output_name": "geometry.json", "metric_id": "coverage", "unit": "1"}})

        def decide(actor, body, key):
            return s.call(actor, "gx_decide_revision", {"decision": body}, key)

        first = decide(s.lead, opening, "opening")
        binding = first["comparison"]
        slot = corpus / code
        descriptor = {**materials.parent_acquisition(), "derived_from": [{"artifact_ref": reference, "sha256": materials.PARENT_SHA}],
            "processing": ["coordinate-record withholding/"+specimen["transform_sha256"]],
            "transformation": json.loads((slot/"transform.json").read_bytes())}
        path = s.directory / "acquisition.json"
        path.write_text(json.dumps(descriptor))
        assigned = v621_assignment.assign(s.conn, s.run_id, after_decision=first["decision_ref"], actor="operator",
            source="assigned-specimen", path=slot/"specimen.cif", sha256=specimen["sha256"], role="structure",
            metadata_path=path, media_type="chemical/x-mmcif")
        actor = s.next_actor("run")
        measure = s.node(actor, "measure", assigned["artifact_ref"], capability=METHOD, parameters=PARAMS, branch_id=binding["branch"])
        scheduled = decide(actor, draft(measure, watches=[{"event": "job_settled", "subject_ref": "local:measure"}]), "measure")
        output = s.settle(scheduled["aliases"]["local:measure"])["result_ref"]
        actor = s.next_actor("run")
        packet = actual_packet(s, actor)
        obs = next(item for item in packet["packet"]["observations"] if item["artifact_ref"] == output)
        row = next(item for item in obs["endpoint_rows"] if item["id"] == "coverage")
        self.assertTrue(row["value"].startswith(f'{specimen["selected"]}/{specimen["observed"]} '))
        key = output + "#" + row["key"]
        before = s.head()
        with self.assertRaises(SchemaInvalid) as err:
            decide(actor, dict(draft(evidence=[output]), rationale=reason, comparison_id=binding["comparison_id"],
                              disposition="CHANGE", metric_refs=[key]), "words-alone")
        self.assertEqual(err.exception.details["reason"], "comparison_action_ambiguous")
        self.assertEqual(before, s.head())
        self.assertIsNone(s.conn.execute("SELECT 1 FROM v6_decisions WHERE call_id='words-alone'").fetchone())

        if op == "MAINTAIN":
            operation = s.node(actor, "renamed-reference", reference, capability=METHOD, parameters=PARAMS, branch_id=binding["branch"])
        else:
            operation = {"op": "stop_branch", "branch": binding["branch"], "reason": reason}
        adopted = decide(actor, dict(draft(operation, evidence=[output]), rationale=reason, metric_refs=[key]), "apply")
        if op == "MAINTAIN":
            terminal = s.settle(adopted["aliases"]["local:renamed-reference"])
            self.assertEqual(terminal["status"], "SUCCEEDED")
        else:
            s.advance()

        # Open the actual store in a new read-only transaction. The independent witness
        # checks graph ancestry, immutable output bytes, the Job manifest and the branch owner.
        with closing(receipts.database(s.directory)) as db:
            targets = v623_comparison.bindings(db, s.run_id, first["goal_version"])
            self.assertEqual(targets, [binding])
            decision = db.execute("SELECT * FROM v6_decisions WHERE run_id=? AND decision_ref=?",
                                  (s.run_id, adopted["decision_ref"])).fetchone()
            self.assertEqual(json.loads(decision["receipt_json"]), adopted)
            jobs = receipts.job_inventory(db, s.directory, s.run_id)
            effect = receipts.decision_effect(db, s.run_id, decision, jobs)
            action = original._action(db, s.run_id, {"decision": effect}, {j["node_id"]: j for j in jobs})
            goal_status, = db.execute("SELECT status FROM v6_goals WHERE run_id=?", (s.run_id,)).fetchone()
            trace = {"completed_jobs": [_named_job(j) for j in jobs], "closed": goal_status == "CLOSED"}
            enacted, event = effect_for(action, trace, binding, None, {})
            self.assertEqual(enacted, op)
            self.assertEqual(effect["rationale"], reason)
            self.assertEqual(adopted["comparison_action"]["disposition"], enacted)
            self.assertEqual(adopted["comparison_action"]["comparison_id"], binding["comparison_id"])
            metric, = adopted["endpoint_use"]["used_metric_refs"]
            delivery = adopted["endpoint_use"]["receipts"][metric]
            self.assertEqual(delivery["source_ref"], output)
            self.assertLess(delivery["event_id"], adopted["event_id"])
            self.assertIn(metric, adopted["comparison_action"]["metric_refs"])
            event_kind = "v6.node.settled" if op == "MAINTAIN" else "v6.branch.stopped"
            event_id, = db.execute("SELECT event_id FROM events WHERE run_id=? AND kind=? AND subject_id=?",
                                   (s.run_id, event_kind, event["ref"])).fetchone()
            self.assertLess(adopted["event_id"], event_id)
        expected = "MAINTAIN" if specimen["selected"] * 5 >= specimen["observed"] * 3 else "CHANGE"
        return {"run": s.run_id, "submitted_rationale": reason, "method_family": family, "comparison_id": binding["comparison_id"],
            "received_value": row["value"], "metric_ref": metric, "used_delivery_event": delivery["event_id"],
            "enacted": enacted, "decision_event": adopted["event_id"], "effect": event, "terminal_event": event_id,
            "agrees_with_registered_cutoff": enacted == expected, "words_alone_refusal": err.exception.details["reason"]}
