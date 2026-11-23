import copy
import json
import unittest

from adapters.local.p01_grid.adapter import smoke_request
from gx_core import schema
from gx_core.domain import ids
from gx_core.errors import SchemaInvalid
from gx_core.p0 import grid_planner
from workflows.science_grid_v3.products import DECISION_SCHEMA


def packet(rounds=2, sources=()):
    return {"role": "planner", "input": {"output_kind": "science_decision", "max_rounds": rounds,
            "supplement_sources": list(sources), "product_contract": {"digest": "frozen", "schema": DECISION_SCHEMA}}}


class PlannerTransportTests(unittest.TestCase):
    def test_only_eligible_new_planners_get_a_distinct_and_reproducible_presentation(self):
        control = packet(rounds=1)
        for sample in (control, {**packet(), "role": "evidence_reviewer"}):
            before = copy.deepcopy(sample)
            grid_planner.bind(sample)
            self.assertEqual(sample, before)
            self.assertIsNone(grid_planner.assigned({"role": sample["role"], "brief": "GX task brief\n" + json.dumps(sample)}))
        for sample in (packet(), packet(rounds=1, sources=[{}])):
            original = sample["input"]
            before = copy.deepcopy(original)
            grid_planner.bind(sample)
            self.assertEqual(original, before)
            brief = "GX task brief\n" + json.dumps(sample, separators=(",", ":"))
            assigned = grid_planner.assigned({"role": "planner", "brief": brief})
            self.assertEqual(sample["input"]["product_contract"]["digest"], assigned["workflow_contract_digest"])
            self.assertEqual(assigned["digest"], ids.sha256_of_obj({k: v for k, v in assigned.items() if k != "digest"}))
            self.assertNotEqual(assigned["digest"], assigned["workflow_contract_digest"])
            self.assertEqual(assigned, sample[grid_planner.FIELD])
            props = assigned["schema"]["properties"]
            self.assertEqual("properties" in props["source_decisions"]["items"], bool(original["supplement_sources"]))

    def test_existing_method_envelope_and_escaped_repair_pointer_for_a_path_or_pivot(self):
        sample = packet(sources=[{}])
        grid_planner.bind(sample)
        assigned = sample[grid_planner.FIELD]
        grid = json.loads(smoke_request()["inputs"]["grid"]["text"])
        grid["delay_seconds"] = 0
        props = assigned["schema"]["properties"]
        self.assertFalse(schema.validate(grid, props["next_grids"]["additionalProperties"]))
        self.assertEqual(props["pivots"]["items"]["properties"]["grid"], props["next_grids"]["additionalProperties"])
        grid_planner.validate_grids({"next_grids": {"a~/b": grid}, "pivots": [{"grid": grid}],
                                     "source_decisions": [{"evidence_refs": ["unverified-citation"]}]}, assigned)
        with self.assertRaises(SchemaInvalid) as candidate:
            grid_planner.validate_grids({"next_grids": {"a": {**grid, "candidates": [{"x": "3"}]}}}, assigned)
        self.assertEqual(candidate.exception.details["pointer"], "/next_grids/a/candidates/0")
        for invalid in (grid["candidates"], {**grid, "method_version": 1}, {**grid, "candidates": []}, None):
            for body, pointer in (({"next_grids": {"a~/b": invalid}}, "/next_grids/a~0~1b"),
                                  ({"pivots": [{"grid": invalid}]}, "/pivots/0/grid")):
                with self.subTest(pointer=pointer, invalid=invalid), self.assertRaises(SchemaInvalid) as failed:
                    grid_planner.validate_grids(body, assigned)
                self.assertEqual(failed.exception.details, {"category": "product_structure",
                                 "contract_digest": assigned["digest"], "pointer": pointer})
