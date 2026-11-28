"""The official dynamic tool schema must reflect what the decision owner can infer."""
import unittest

from gx_core.p0 import harness_policy, v621_policy, v6_native


def actions(packet):
    tools = v6_native.tool_specs([], packet)
    decision = next(t for t in tools if t["name"] == "gx_decide_revision")
    options = decision["inputSchema"]["properties"]["decision"]["properties"]["ops"]["items"]["oneOf"]
    return {row["properties"]["op"]["const"]: row for row in options}


class NativeContract(unittest.TestCase):
    def test_candidate_projects_inference_without_mutating_predecessor(self):
        original = {"scope": "run", "evidence_policy": v621_policy.VERSION,
                    "research_evidence_version": "gx.research.v6.2b.1", "capabilities": []}
        self.assertIn("inputs", actions(original)["add_node"]["required"])
        current = {**original, "harness_policy": harness_policy.VERSION}
        self.assertNotIn("inputs", actions(current)["add_node"]["required"])
        self.assertIn("bind_intent", actions(current))
        self.assertIn("core_question", actions(current)["close_run"]["properties"])
        self.assertNotIn("bind_intent", actions(original))
        self.assertIn("inputs", actions(original)["add_node"]["required"])
