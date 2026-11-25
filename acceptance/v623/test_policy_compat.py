"""Pin the actual prior host vocabularies while allowing new research to cite new endpoints."""
import hashlib
import json
from pathlib import Path
import unittest

from gx_core.errors import SchemaInvalid
from gx_core.p0 import v621_policy, v62_native, v6_native


def wire_digest(document):
    wire = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(wire).hexdigest()


class PinnedEvidencePolicy(unittest.TestCase):
    def test_old_wire_contracts_match_pre_update_checkout(self):
        prior = Path(__file__).parent / "evidence/baseline/legacy-menus.json"
        for version, tools in json.loads(prior.read_bytes())["tools"].items():
            for name, digest in tools.items():
                with self.subTest(policy=version, tool=name):
                    form = (v6_native._legacy_goal_tools(version)[0] if name == "quick_decision" else
                            v62_native.evidence_tool(name, version))
                    self.assertEqual(wire_digest(form), digest)

    def test_only_current_research_is_admitted_without_replay_class(self):
        self.assertEqual(v621_policy.admit({}), v621_policy.VERSION)
        for version in (v621_policy.LEGACY, v621_policy.PREVIOUS, v621_policy.V622, v621_policy.V623):
            with self.subTest(version=version):
                with self.assertRaises(SchemaInvalid):
                    v621_policy.admit({"evidence_policy": version})
                self.assertEqual(v621_policy.admit({"historical_replay": True, "evidence_policy": version}), version)
        with self.assertRaises(SchemaInvalid):
            v621_policy.admit({"historical_replay": True})

    def test_new_lead_can_use_structured_disposition_and_typed_external_observation(self):
        version = v621_policy.VERSION
        decision = v62_native.evidence_tool("gx_decide_revision", version)
        fields = decision["inputSchema"]["properties"]["decision"]["properties"]
        self.assertEqual(set(fields["disposition"]["enum"]), {"CHANGE", "MAINTAIN", "STOP"})
        self.assertTrue({"comparison_plan", "comparison_id", "metric_refs", "affected_scope", "stop_reason"} <= fields.keys())
        claim = v62_native.evidence_tool("gx_stage_claim", version)
        prediction = claim["inputSchema"]["properties"]["prediction"]["properties"]
        self.assertEqual(set(prediction["external_endpoint"]["properties"]), {"unit", "instrument", "selection"})


if __name__ == "__main__":
    unittest.main()
