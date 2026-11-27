"""Use the frozen acquisition scope of the selected originals in native typed predictions."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.science_execution.test_signed_native import METHOD, PARAMETERS, PLUGIN, signed_claim
from acceptance.v62a.interleavings_fixture import branch, draft
from acceptance.v622.test_close import SurveyedStudy
from acceptance.v623.test_block_method import synthetic_cif
from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid
from gx_core.p0 import cli, v6_goal, v6_prospective, v621_policy


FROZEN = "Generated coordinates, model one, label chains A/B/C."
STATEMENT = "鏈對變化的中位數會遺漏至少一個達標的局部變化。"
LIMITS = "只比較本組原件；不外推到其他構象。"
TOKEN = "{{material_scope}}"


class PredictionScope(unittest.TestCase):
    def study(self, scopes, *, deformed=True, historical=None, opening=False):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        files = []
        for i, scope in enumerate(scopes):
            coordinate = directory / f"sample-{i}.cif"
            coordinate.write_bytes(synthetic_cif(deformed=deformed) + f"\n# original {i}\n".encode())
            item = {"role": "structure", "path": coordinate.name}
            if scope is not None:
                descriptor = directory / f"source-{i}.json"
                descriptor.write_text(json.dumps({"source": f"synthetic:source-{i}", "applicability": scope}))
                item["acquisition"] = descriptor.name
            files.append(item)
        grant = {"project": "scope-binding", "plugins": [PLUGIN], "capabilities": [METHOD],
                 "research_effort": "deep", "input_files": files}
        if opening:
            grant["prospective_opening"] = {"capabilities": [METHOD], "material_roles": ["structure"],
                                            "metric_ids": ["missed_present"]}
        if historical:
            grant.update(evidence_policy=historical, historical_replay=True)
        config = directory / "grant.json"
        config.write_text(json.dumps(grant))
        with patch.object(cli, "run_until_stopped", side_effect=lambda conn, _, rid, timeout: cli.snapshot(conn, rid)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["research", "--state", str(directory / "state"), "--run-key", "local",
                                       "--goal", "量測局部鏈對的變化。", "--goal-config", str(config)]), 0)
        s = self.enterContext(SurveyedStudy(directory, reopen=True))
        s.next_actor("run")
        return s

    def claim(self, refs, scope=None):
        claim = signed_claim(refs[0])
        claim.update(statement=STATEMENT, limitations=LIMITS, evidence_refs=refs)
        claim["prediction"]["data_refs"] = refs
        if scope is None:
            del claim["scope"]
        else:
            claim["scope"] = scope
        return claim

    def test_omitted_scope_binds_original_and_preserves_scientific_text(self):
        s = self.study([FROZEN, "Independent assembly."])
        request = self.claim([s.materials[0]])
        response = s.call(s.lead, "gx_stage_claim", request, "stage")
        self.assertEqual(response, s.call(s.lead, "gx_stage_claim", request, "stage"))
        claim = ArtifactService(s.conn).read_json(response["claim_ref"])["claim"]
        self.assertEqual((claim["scope"], claim["statement"], claim["limitations"]), (FROZEN, STATEMENT, LIMITS))

    def test_signed_job_only_adjudicates_predictions_for_its_original_scope(self):
        cases = [(TOKEN, True, "supported"), (None, False, "opposed"), ("Different assembly.", True, "undetermined")]
        for scope, deformed, direction in cases:
            with self.subTest(scope=scope, direction=direction):
                # An unconstrained exploratory Goal may execute its Job with a different prediction scope.
                has_opening = direction != "undetermined"
                s = self.study([FROZEN], deformed=deformed, opening=has_opening)
                self.assertEqual(has_opening, v6_prospective.opening_pending(s.conn, s.run_id))
                target, = s.materials
                staged = s.call(s.lead, "gx_stage_claim", self.claim([target], scope), "stage")
                self.assertEqual(has_opening, v6_prospective.opening_pending(s.conn, s.run_id))
                route = branch("signed")
                route["budget_limits"]["cpu_seconds"] = 120
                node = s.node(s.lead, "measure", target, branch_id="local:signed", capability=METHOD,
                              parameters=PARAMETERS)
                result = s.call(s.lead, "gx_decide_revision", {"decision": draft(route, node,
                    evidence=[target, staged["claim_ref"]],
                    watches=[{"event": "job_settled", "subject_ref": "local:measure"}])}, "adopt")
                claim, = result["claim_revisions"]
                reg = claim["maturity"]["registration"]
                expected_scope = FROZEN if has_opening else scope
                self.assertEqual(reg["method_pin"]["measurement_spec"]["applicability"], expected_scope)
                self.assertFalse(v6_prospective.opening_pending(s.conn, s.run_id))
                job = s.settle(result["aliases"]["local:measure"])
                self.assertEqual(s.rows("SELECT status FROM jobs"), [{"status": "SUCCEEDED"}])
                output = ArtifactService(s.conn).read_json(job["result_ref"])
                self.assertEqual(output["missed_present"], int(deformed))
                settled, = s.brief(s.next_actor("run"))["claims"]
                self.assertEqual(settled["maturity"]["direction"], direction)
                if has_opening:
                    coverage, = settled["maturity"]["material_coverage"]
                    self.assertEqual(coverage["qualification"], "prospective")
                    self.assertLess(reg["event_id"], coverage["endpoint_first_available_event_id"])
                else:
                    self.assertEqual(settled["maturity"]["results"], [])

    def test_multiple_originals_with_one_common_attested_scope(self):
        s = self.study([FROZEN, FROZEN])
        staged = s.call(s.lead, "gx_stage_claim", self.claim(s.materials, TOKEN), "stage")
        stored = ArtifactService(s.conn).read_json(staged["claim_ref"])
        self.assertEqual(stored["claim"]["scope"], FROZEN)
        self.assertEqual({x["artifact_ref"] for x in stored["prediction_data"]}, set(s.materials))

    def test_explicit_exploratory_scope_warns_and_does_not_release_opening(self):
        for limit in ("另批座標的裝配體。", FROZEN + " Selected ligand-bound subpopulation only."):
            with self.subTest(scope=limit):
                s = self.study([FROZEN], opening=True)
                staged = s.call(s.lead, "gx_stage_claim", self.claim(s.materials, limit), "stage")
                feedback = staged["scope_feedback"]
                self.assertEqual(feedback["status"], "out_of_scientific_scope")
                self.assertEqual(feedback["field"], "scope")
                self.assertEqual(feedback["available_scopes"], [FROZEN])
                stored = ArtifactService(s.conn).read_json(staged["claim_ref"])
                self.assertEqual(stored["claim"]["scope"], limit)
                adopted = s.call(s.lead, "gx_decide_revision", {"decision": draft(
                    evidence=[*s.materials, staged["claim_ref"]])}, "adopt")
                claim, = adopted["claim_revisions"]
                self.assertEqual(claim["maturity"]["direction"], "undetermined")
                self.assertTrue(v6_prospective.opening_pending(s.conn, s.run_id))
                self.assertEqual(s.rows("SELECT count(*) AS n FROM jobs"), [{"n": 0}])

    def test_different_or_missing_material_scopes_cannot_be_inferred(self):
        for scopes in ((FROZEN, "Distinct assembly."), (FROZEN, None), (None,)):
            with self.subTest(scopes=scopes):
                s = self.study(scopes)
                for idx, scope in enumerate((None, TOKEN)):
                    with self.assertRaises(SchemaInvalid):
                        s.call(s.lead, "gx_stage_claim", self.claim(s.materials, scope), f"stage-{idx}")

    def test_acquisition_recorded_after_frozen_brief_does_not_supply_scope(self):
        s = self.study([None])
        target, = s.materials
        v6_goal.record_observation(s.conn, s.run_id, target, source="late",
            acquisition={"source": "late:same-target", "applicability": FROZEN})
        with self.assertRaises(SchemaInvalid):
            s.call(s.lead, "gx_stage_claim", self.claim([target]), "stage")

    def test_conflicting_acquisitions_for_one_original_cannot_supply_its_scope(self):
        s = self.study([FROZEN])
        target, = s.materials
        s.call(s.lead, "gx_decide_revision", {"decision": draft()}, "wait")
        v6_goal.record_observation(s.conn, s.run_id, target, source="independent-acquisition",
            acquisition={"source": "synthetic:conflicting-acquisition", "applicability": "Different assembly."})
        lead = s.next_actor("run")
        for idx, scope in enumerate((None, TOKEN)):
            with self.assertRaises(SchemaInvalid):
                s.call(lead, "gx_stage_claim", self.claim([target], scope), f"ambiguous-{idx}")

    def test_scope_is_still_required_without_a_typed_prediction(self):
        s = self.study([FROZEN])
        qualitative = self.claim(s.materials)
        del qualitative["prediction"]
        with self.assertRaises(SchemaInvalid):
            s.call(s.lead, "gx_stage_claim", qualitative, "qualitative")

    def test_historical_policy_requires_literal_scope(self):
        s = self.study([FROZEN], historical=v621_policy.V623)
        with self.assertRaises(SchemaInvalid):
            s.call(s.lead, "gx_stage_claim", self.claim(s.materials), "omitted")
        staged = s.call(s.lead, "gx_stage_claim", self.claim(s.materials, FROZEN), "exact")
        self.assertNotIn("scope_feedback", staged)
        self.assertEqual(ArtifactService(s.conn).read_json(staged["claim_ref"])["claim"]["scope"], FROZEN)
