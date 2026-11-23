"""The science_grid@3 reviewer contract against synthetic, frozen Job artifacts."""
import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
import unittest

from gx_core.orchestration.engine import module_sources
from workflows.science_grid_v3 import contract as grid


PIN = module_sources(grid.SEMANTIC_VALIDATORS)[grid.__name__]


def digest(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


class Packet:
    def __init__(self):
        self.refs = {}
        self.proposal = {"method": "fixture-grid@1", "threshold": -1.0, "paths": []}
        self.evidence = {"proposal_ref": "proposal", "routes": []}
        self.artifacts = {}
        for path, x, expected in [("A", 1, True), ("B", 0, True), ("C", 4, False)]:
            input_ref, obs_ref, eval_ref = ("input-" + path, "obs-" + path, "eval-" + path)
            method = {"method_id": "fixture-grid", "method_version": "1", "synthetic": True}
            sample = {"candidate_id": path + "-sample", "x": x}
            score = -(x - 2) ** 2
            self.proposal["paths"].append({"id": path, "prediction": "The stated finite-sample cutoff outcome.",
                                            "expected_reaches_threshold": expected, "input_ref": input_ref})
            self.evidence["routes"].append({"path_id": path, "job_id": "job-" + path, "status": "SUCCEEDED",
                "outcome_kind": "valid_scientific_outcome", "input_ref": input_ref, "observations_ref": obs_ref,
                "evaluation_ref": eval_ref, "eval_id": "scoring-" + path})
            self.artifacts[input_ref] = {**method, "candidates": [sample.copy()]}
            input_sha = digest(self.artifacts[input_ref])
            self.artifacts[obs_ref] = {**method, "input_sha256": input_sha,
                                       "observations": [{**sample, "score": score}]}
            self.artifacts[eval_ref] = {"input_ref": obs_ref, "input_sha256": digest(self.artifacts[obs_ref]),
                "attempt_id": "scoring-" + path, "observations": 1, "max_score": score, "threshold": -1.0,
                "count_at_or_above": int(score >= -1.0)}

    def freeze(self, **kwargs):
        args = {"artifact_digests": {key: digest(doc) for key, doc in self.artifacts.items()},
                "validator_digest": PIN, "requires_interpretation": False}
        return grid.ProductContract.freeze("proposal", self.proposal, {"evidence": self.evidence},
                                           self.artifacts, **{**args, **kwargs})

    @staticmethod
    def product():
        return {"findings_by_path": {key: {"reason": f"Interpretation for {key}.", "risk": "Finite samples."}
                                     for key in ("A", "B", "C")}}

    def refreeze_observations(self, path):
        self.artifacts["eval-" + path]["input_sha256"] = digest(self.artifacts["obs-" + path])


class GridProductContract(unittest.TestCase):
    def setUp(self):
        self.packet = Packet()

    def test_positive_negative_boundary_and_negative_expectation(self):
        frozen = self.packet.freeze()
        self.assertFalse(frozen.requires_review)
        body = frozen.evaluate()
        self.assertEqual(body["assessment_mode"], "deterministic")
        self.assertEqual([f["reaches_threshold"] for f in body["findings"]], [True, False, False])
        self.assertEqual([f["supported"] for f in body["findings"]], [True, False, True])
        self.assertEqual([f["measurement_valid"] for f in body["findings"]], [True] * 3)
        self.assertIn("1 of 1", body["findings"][0]["reason"])
        frozen.validate_expanded(body)
        self.packet.proposal["paths"][0]["expected_reaches_threshold"] = False
        self.assertIs(self.packet.freeze().evaluate()["findings"][0]["supported"], False)

    def test_same_concrete_contract_drives_schema_brief_and_repair(self):
        frozen = self.packet.freeze()
        valid = self.packet.product()
        shape = frozen.schema()["properties"]["findings_by_path"]
        self.assertEqual(set(shape["required"]), valid["findings_by_path"].keys())
        self.assertIs(shape["additionalProperties"], False)
        self.assertEqual(shape["properties"]["A"]["required"], ["reason", "risk"])
        for key in shape["required"]:
            self.assertIn(json.dumps(key), frozen.brief())
        for mutation in (
            lambda p: p["findings_by_path"].pop("B"),
            lambda p: p["findings_by_path"].update(extra=p["findings_by_path"]["A"]),
            lambda p: p.update(proposal_ref="proposal"),
            lambda p: p["findings_by_path"]["A"].update(path_id="A"),
            lambda p: p["findings_by_path"]["A"].update(evaluation_ref="eval-A"),
            lambda p: p["findings_by_path"]["A"].update(supported=True),
            lambda p: p["findings_by_path"]["C"].update(measurement_valid=True),
            lambda p: p["findings_by_path"]["A"].update(reason="   "),
            lambda p: p["findings_by_path"]["A"].update(risk="x" * (grid.MAX_TEXT + 1)),
        ):
            bad = copy.deepcopy(valid)
            mutation(bad)
            with self.subTest(bad=list(bad)):
                with self.assertRaises(grid.StructuralError) as failure:
                    grid.validate_model_schema(bad, frozen.schema(), frozen.digest)
                self.assertEqual(failure.exception.contract_digest, frozen.digest)
                self.assertTrue(failure.exception.pointer.startswith("/"))
        frozen.validate_structure(valid)
        expanded = frozen.evaluate(valid)
        first = expanded["findings"][0]
        self.assertEqual((first["path_id"], first["evaluation_ref"], first["reason"]),
                         ("A", "eval-A", valid["findings_by_path"]["A"]["reason"]))
        frozen.validate_expanded(expanded)

    def test_frozen_facts_survive_caller_mutation_and_pin_validator_bytes(self):
        frozen = self.packet.freeze()
        self.assertEqual(len(PIN), 64)
        self.assertTrue(all(fn.__module__ == grid.__name__ for fn in grid.SEMANTIC_VALIDATORS))
        self.assertEqual(frozen.digest, self.packet.freeze().digest)
        self.assertNotEqual(frozen.digest, self.packet.freeze(validator_digest="f" * 64).digest)
        self.packet.artifacts["obs-A"]["observations"].clear()
        self.packet.proposal["paths"][0]["prediction"] = "Changed."
        self.assertTrue(frozen.evaluate()["findings"][0]["supported"])
        with self.assertRaises(FrozenInstanceError):
            frozen.paths[0].expected = False
        with self.assertRaises(FrozenInstanceError):
            frozen.paths[0].measurement.valid = False

    def test_provenance_errors_are_distinct_from_model_repair(self):
        for mutation in (
            lambda p: p.evidence.update(proposal_ref="foreign"),
            lambda p: p.evidence["routes"][0].update(input_ref="input-B"),
            lambda p: p.evidence["routes"][0].update(job_id="job-B"),
            lambda p: p.evidence["routes"][0].update(path_id="B"),
            lambda p: p.evidence["routes"][0].update(status="FAILED"),
            lambda p: p.evidence["routes"][0].update(observations_ref="obs-B"),
            lambda p: p.artifacts["eval-A"].update(input_ref="obs-C"),
            lambda p: p.artifacts["eval-A"].update(input_sha256="0" * 64),
            lambda p: p.artifacts["eval-A"].update(attempt_id="scoring-C"),
            lambda p: p.artifacts["eval-A"].update(threshold=0),
            lambda p: p.artifacts["obs-A"].update(input_sha256="wrong-grid"),
        ):
            packet = Packet()
            mutation(packet)
            with self.subTest(mutation=mutation):
                with self.assertRaises(grid.SemanticError) as failed:
                    packet.freeze()
                self.assertEqual(failed.exception.finding.category, "provenance")
                self.assertFalse(isinstance(failed.exception, grid.StructuralError))
        issues = grid.validate_path_coverage(("A", "B"), ("A", "A"))
        self.assertIsInstance(issues[0], grid.ValidationFinding)

    def test_failed_job_or_evaluation_is_neither_support_nor_refutation(self):
        for kind, status in (("measurement_failure", "SUCCEEDED"), ("infrastructure_failure", "FAILED"),
                             ("measurement_failure", "FAILED"), ("historical_only", "SUCCEEDED")):
            with self.subTest(kind=kind, status=status):
                self.packet.evidence["routes"][0].update(outcome_kind=kind, status=status,
                                                       evaluation_ref=None, eval_id=None)
                frozen = self.packet.freeze()
                self.assertTrue(frozen.requires_review)
                with self.assertRaises(grid.SemanticError):
                    frozen.evaluate()
                path = frozen.evaluate(self.packet.product())["findings"][0]
                self.assertIs(path["measurement_valid"], False)
                self.assertIsNone(path["supported"])
                self.assertIsNone(path["reaches_threshold"])

    def test_summary_must_agree_with_complete_finite_original_sample(self):
        for mutation in (
            lambda p: p.artifacts["obs-A"]["observations"].clear(),
            lambda p: p.artifacts["obs-A"]["observations"][0].update(candidate_id="wrong"),
            lambda p: p.artifacts["obs-A"]["observations"][0].update(x=0),
            lambda p: p.artifacts["obs-A"]["observations"][0].update(x=True),
            lambda p: p.artifacts["obs-A"]["observations"][0].update(score=float("nan")),
            lambda p: p.artifacts["obs-A"]["observations"].append(dict(p.artifacts["obs-A"]["observations"][0])),
            lambda p: p.artifacts["eval-A"].update(observations=0),
            lambda p: p.artifacts["eval-A"].update(count_at_or_above=True),
            lambda p: p.artifacts["eval-A"].update(count_at_or_above=0),
            lambda p: p.artifacts["eval-A"].update(max_score=float("inf")),
        ):
            packet = Packet()
            mutation(packet)
            packet.refreeze_observations("A")
            frozen = packet.freeze()
            with self.subTest(mutation=mutation):
                self.assertTrue(frozen.requires_review)
                path = frozen.evaluate(packet.product())["findings"][0]
                self.assertIs(path["measurement_valid"], False)
                self.assertIsNone(path["supported"])

    def test_scope_supplements_and_live_advisory_values_require_actual_judgment(self):
        frozen = self.packet.freeze(supplement_sources=[{"source_id": "operator", "artifact_ref": "operator-ref"}],
                                    intent_items={"language": {"item_id": "language", "version": 3}})
        self.assertTrue(frozen.requires_review)
        product = self.packet.product()
        with self.assertRaises(grid.StructuralError):
            frozen.evaluate(product)
        product.update(source_reviews_by_id={"operator": {"disposition": "pending", "reason": "Control unresolved.",
            "compared_paths": ["A", "B"], "qualifies_paths": ["A"]}}, intent_reasons_by_id={"language": "Applied."})
        result = frozen.evaluate(product)
        self.assertEqual(result["assessment_mode"], "independent_review")
        review = result["source_reviews"][0]
        self.assertEqual(review["artifact_ref"], "operator-ref")
        self.assertEqual(review["evidence_refs"], ["evidence", "obs-A", "eval-A", "obs-B", "eval-B"])
        self.assertIs(result["findings"][0]["reaches_threshold"], True)
        self.assertIsNone(result["findings"][0]["supported"])
        self.assertIs(result["findings"][1]["supported"], False)
        self.assertEqual(result["findings"][0]["qualifying_source_ids"], ["operator"])
        self.assertEqual(result["intent_responses"], [{"item_id": "language", "version": 3, "reason": "Applied."}])
        frozen.validate_expanded(result)
        product["source_reviews_by_id"]["operator"]["qualifies_paths"] = ["C"]
        with self.assertRaises(grid.SemanticError) as invalid:
            frozen.evaluate(product)
        self.assertEqual(invalid.exception.finding.category, "scientific_judgment")
        product["source_reviews_by_id"]["operator"].update(disposition="rejected", qualifies_paths=["A"])
        with self.assertRaises(grid.SemanticError):
            frozen.evaluate(product)
        product["source_reviews_by_id"]["operator"].update(qualifies_paths=[], compared_paths=["foreign"])
        with self.assertRaises(grid.StructuralError):
            frozen.evaluate(product)

    def test_mechanistic_uncertainty_can_remain_open_without_supplement(self):
        frozen = self.packet.freeze(requires_interpretation=True)
        self.assertTrue(frozen.requires_review)
        with self.assertRaises(grid.SemanticError):
            frozen.evaluate()
        model = self.packet.product()
        model["findings_by_path"]["A"]["unresolved_question"] = "Could either mechanism explain the same score?"
        body = frozen.evaluate(model)
        self.assertIsNone(body["findings"][0]["supported"])
        self.assertTrue(body["findings"][0]["reaches_threshold"])
        frozen.validate_expanded(body)

    def test_edited_or_malformed_derived_fields_fail_independent_validation(self):
        frozen = self.packet.freeze()
        for mutation in (lambda b: b["findings"][0].update(supported=1),
                         lambda b: b["findings"][0].update(measurement_valid=False),
                         lambda b: b["findings"][0].update(evaluation_ref="eval-B"),
                         lambda b: b.update(contract_digest="different"),
                         lambda b: b["findings"].append(copy.deepcopy(b["findings"][0])),
                         lambda b: b.update(assessment_mode="other")):
            body = frozen.evaluate(self.packet.product())
            mutation(body)
            with self.subTest(mutation=mutation), self.assertRaises(grid.SemanticError):
                frozen.validate_expanded(body)

    def test_oversize_or_deferred_valid_job_stays_ungraded(self):
        for bounds in ({"artifact_sizes": {"obs-A": grid.MAX_INLINE_BYTES + 1}}, {"deferred_refs": {"obs-A"}}):
            frozen = self.packet.freeze(**bounds)
            self.assertTrue(frozen.requires_review)
            first = frozen.evaluate(self.packet.product())["findings"][0]
            self.assertIsNone(first["measurement_valid"])
            self.assertIsNone(first["supported"])
            self.assertEqual(first["outcome_kind"], "valid_scientific_outcome")
            self.assertIn("Job-backed", first["measurement_note"])
        self.packet.artifacts["obs-A"]["observations"] *= grid.MAX_INLINE_POINTS + 1
        self.packet.refreeze_observations("A")
        frozen = self.packet.freeze()
        self.assertIsNone(frozen.evaluate(self.packet.product())["findings"][0]["supported"])
