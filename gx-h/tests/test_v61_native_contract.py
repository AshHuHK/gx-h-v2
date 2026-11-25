"""The formal Goal tool catalog declares precisely the authenticated native callbacks."""
import unittest

from gx_core import api, contracts, registry
from gx_core.errors import SchemaInvalid
from gx_core.p0 import v6_claims, v6_delegation, v6_graph, v6_native


class GoalToolContract(unittest.TestCase):
    def test_scientific_callbacks_registered_and_private_to_the_native_transport(self):
        implementations = {
            "gx_decide_revision": v6_native.decision_tool(),
            "gx_stage_claim": v6_claims.tool_spec(),
            "gx_delegate_branch": v6_delegation.TOOL,
        }
        doctor = api.doctor()
        self.assertEqual(doctor["registry"]["compatibility_only"]["native_goal_tools"], [])
        self.assertEqual(set(doctor["registry"]["published"]["native_goal_tools"]), implementations.keys())
        for name, spec in implementations.items():
            with self.subTest(name=name):
                self.assertEqual(contracts.native_goal_tool_spec(name), spec)
                self.assertIs(registry.native_goal_handler_for(name), v6_native.call)
                with self.assertRaises(api.UnknownTool):
                    api.describe_tool(name)
        self.assertIsNone(registry.native_goal_handler_for("gx_reevaluate_geometry"))

    def test_decision_operations_and_claim_levels_are_distinct_from_future_measured_watches(self):
        shape = v6_graph.decision_schema()["properties"]
        ops = shape["ops"]["items"]["oneOf"]
        self.assertEqual({op["properties"]["op"]["const"] for op in ops},
                         {"open_branch", "add_node", "stop_branch", "cancel_node", "close_run"})
        self.assertEqual(set(v6_claims.LEVELS),
                         {"open", "limited_support", "contested", "insufficient", "mixed", "refuted"})
        self.assertNotIn("chain_pairs", v6_claims.REQUIRED)
        with self.assertRaises(SchemaInvalid):
            v6_graph.validate_body({"mode": "resolve", "rationale": "Await measurements.",
                                    "evidence_refs": [], "ops": [],
                                    "reconsider_when": [{"event": "watch_measure", "subject_ref": "sensor"}]})


if __name__ == "__main__":
    unittest.main()
