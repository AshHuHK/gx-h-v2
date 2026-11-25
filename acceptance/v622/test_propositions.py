"""Exercise prospective and retrospective revisions through native Goal publication."""
import json
import os
from pathlib import Path
import unittest

from acceptance.v621 import test_claim_publication as original
from gx_core.p0 import v6_claims


class ProspectiveRevisions(unittest.TestCase):
    def setUp(self):
        self.flow = original.ClaimPublication()
        self.addCleanup(self.flow.doCleanups)
        self.flow.start(second_material=True)

    def revision(self, actor, key, refs, threshold, prior, materials=None, level="limited_support"):
        words = {5: "five", 9: "nine", 10: "ten"}
        rule = {**self.flow.rule, "threshold": threshold,
                "data_refs": self.flow.gx.materials if materials is None else materials}
        return self.flow.adopt(actor, key, refs, level,
                               statement="Each registered morning sample exceeds " + words[threshold] + " degrees.",
                               criteria="Every registered morning sample exceeds " + words[threshold] + " degrees.",
                               prediction=rule, **({"previous_claim_ref": prior["claim_revision_id"]} if prior else {}))

    def write_trace(self, case, **payload):
        root = os.environ.get("GX_V622_PROPOSITION_RECEIPTS")
        if root:
            flow = self.flow
            root = Path(root)
            root.mkdir(parents=True, exist_ok=True)
            result = {"case": case, "run_id": flow.gx.run_id, "paid_calls": 0,
                      "events": flow.gx.rows("SELECT event_id,kind,subject_id FROM events WHERE run_id=? "
                                            "AND kind IN ('v6.claim.adopted','v62.claim.reassessed','v6.observation.added') "
                                            "ORDER BY event_id", flow.gx.run_id),
                      **payload}
            (root / (case + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    def test_lower_threshold_after_two_measurements(self):
        f = self.flow
        refs = [f.observe("first", {"schema": original.METRIC, "measurement": {"value": 7}},
                          data=f.gx.materials[:1]),
                f.observe("second", {"schema": original.METRIC, "measurement": {"value": 8}},
                          data=f.gx.materials[1:])]
        lead, prior, seen = f.delivered_claim()
        self.assertEqual(prior["maturity"]["direction"], "opposed")
        self.assertEqual({r["value"] for r in prior["maturity"]["results"]}, {7, 8})
        published = self.revision(lead, "lower-after-read", refs, 5, prior)
        external, = f.cli("status")["claims"]
        self.assertEqual(external["claim_revision_id"], published["claim_revision_id"])
        self.write_trace("R3", registration=f.initial, original_verdict=prior, lead_brief_sha256=seen,
                         receipts=refs, published=published, external=external)
        self.assertIn(external["level"], {"open", "insufficient"})
        self.assertEqual(external["publication"]["direction"], "undetermined")
        self.assertEqual([row["qualification"] for row in published["maturity"]["material_coverage"]],
                         ["retrospective", "retrospective"])
        self.assertEqual(v6_claims.read_revision(f.gx.conn, f.gx.run_id, prior["claim_revision_id"]), prior)
        # A subsequent, distinct material can support the new rule after it is registered.
        third = f.observe("next-site", {"site": "new morning lot"}, data=[])
        lead = f.gx.next_actor("run")
        active = f.cli("status")["claims"][0]
        new_registration = self.revision(lead, "new-lot", [third], 5, active, [third], level="open")
        fresh = f.observe("third", {"schema": original.METRIC, "measurement": {"value": 6}}, data=[third])
        lead, current, _ = f.delivered_claim()
        supported = self.revision(lead, "read-new-lot", [fresh], 5, current, [third])
        self.assertEqual(supported["level"], "limited_support")
        self.assertEqual(supported["maturity"]["material_coverage"][0]["qualification"], "prospective")
        self.assertEqual(supported["maturity"]["registration"]["event_id"], new_registration["maturity"]["registration"]["event_id"])
        self.write_trace("R3-fresh", superseded=published, registration=new_registration, fresh=supported,
                         external=f.cli("status")["claims"][0])

    def test_partial_coverage_survives_threshold_revision(self):
        f = self.flow
        sample = f.reading("one-sample", 14)
        lead, prior, seen = f.delivered_claim()
        published = self.revision(lead, "lower-partial", [sample], 9, prior)
        external, = f.cli("status")["claims"]
        self.write_trace("R2", registration=f.initial, original_verdict=prior, lead_brief_sha256=seen,
                         receipts=[sample], published=published, external=external)
        self.assertIn(external["level"], {"open", "insufficient"})
        self.assertEqual([row["qualification"] for row in published["maturity"]["material_coverage"]],
                         ["retrospective", "unmeasured"])
