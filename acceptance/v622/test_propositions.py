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
        seen_by_lead = published["decision_delivery"]
        self.assertEqual((seen_by_lead["state"], seen_by_lead["brief_sha256"]), ("acknowledged", seen))
        self.assertTrue(set(refs) <= {o["artifact_ref"] for o in seen_by_lead["observations"]})
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

    def test_reworded_population_retains_prior_exposure_only_for_its_measured_member(self):
        f = self.flow
        first = f.reading("station-a", 7)
        lead, current, _ = f.delivered_claim()
        narrowed = "Mornings in the first week of the north station survey"
        rule = {**f.rule, "threshold": 5}
        revision = f.adopt(lead, "narrow", [first], "open", scope=narrowed, prediction=rule,
                           previous_claim_ref=current["claim_revision_id"])
        self.assertEqual([row["qualification"] for row in revision["maturity"]["material_coverage"]],
                         ["retrospective", "unmeasured"])
        rerun = f.observe("remeasured-first-week", {"schema": original.METRIC, "measurement": {"value": 8}},
                          data=f.gx.materials[:1], scope=narrowed)
        actor, current, _ = f.delivered_claim()
        new = f.adopt(actor, "same-material-and-scope", [rerun], "limited_support", scope=narrowed,
                      previous_claim_ref=current["claim_revision_id"])
        self.assertFalse(new["maturity"]["results"])
        self.assertIn(rerun, new["maturity"]["material_coverage"][0]["retrospective_refs"])
        f.observe("record-scope-change", {"note": "Clarify the first week."}, data=[])
        actor, previous, _ = f.delivered_claim()
        second = f.adopt(actor, "narrow-twice", [rerun], "open", scope="Week one, dawn hours only",
                         prediction=rule, previous_claim_ref=previous["claim_revision_id"])
        self.assertEqual([row["qualification"] for row in second["maturity"]["material_coverage"]],
                         ["retrospective", "unmeasured"])

    def test_single_counterexample_refutes_a_registered_universal(self):
        f = self.flow
        adverse = f.reading("first-negative", 7)
        actor, prior, _ = f.delivered_claim()
        result = f.adopt(actor, "refute-all", [adverse], "refuted", previous_claim_ref=prior["claim_revision_id"])
        self.assertEqual((result["publication"]["direction"], result["level"]), ("opposed", "refuted"))
        self.assertEqual(result["publication"]["unmeasured_data_refs"], f.gx.materials[1:])
        self.assertEqual(result["maturity"]["material_coverage"][0]["opposing_refs"], [adverse])

    def test_pooled_scalar_cannot_supply_per_material_results(self):
        f = self.flow
        avg = f.observe("pooled-average", {"schema": original.METRIC, "measurement": {"value": 19}}, data=f.gx.materials)
        actor, prior, _ = f.delivered_claim()
        result = f.adopt(actor, "require-individuals", [avg], "limited_support", previous_claim_ref=prior["claim_revision_id"])
        self.assertEqual(result["publication"]["direction"], "undetermined")
        self.assertEqual([row["qualification"] for row in result["maturity"]["material_coverage"]], ["unmeasured"] * 2)
        self.assertTrue(any(row["kind"] == "no_individual_material_attribution" for row in result["maturity"]["gaps"]))
        self.write_trace("pooled", published=result)

    def test_new_id_rerun_and_distinct_previously_unmeasured_endpoint(self):
        f = self.flow
        old = f.reading("north-air", 7)
        actor, _, _ = f.delivered_claim()
        # Deliberately omit previous_claim_ref. An alias cannot hide prior data.
        fresh_id = self.revision(actor, "rename-without-parent", [old], 5, None, f.gx.materials[:1])
        self.assertEqual(fresh_id["publication"]["direction"], "undetermined")
        repeat = f.reading("new-instrument-read", 9)
        actor = f.gx.next_actor("run")
        current = next(c for c in f.cli("status")["claims"] if c["hypothesis_id"] == fresh_id["hypothesis_id"])
        recalc = self.revision(actor, "same-data-new-reading", [repeat], 5, current, f.gx.materials[:1])
        self.assertEqual(recalc["maturity"]["material_coverage"][0]["qualification"], "retrospective")
        self.assertFalse(recalc["publication"]["supporting_refs"])
        f.observe("request-independent-endpoint", {"note":"The surface probe is ready"}, data=[])
        actor = f.gx.next_actor("run")
        other_rule = {**f.rule, "data_refs": f.gx.materials[:1], "observable": "/measurement/surface"}
        unseen = f.adopt(actor, "register-surface", [old], "open", statement="The surface temperature exceeds ten degrees.",
                         prediction=other_rule)
        self.assertFalse(unseen["maturity"]["preexisting_result"])
        surface = f.observe("surface", {"schema": original.METRIC, "measurement": {"surface": 15}}, data=f.gx.materials[:1])
        actor = f.gx.next_actor("run")
        current = next(c for c in f.cli("status")["claims"] if c["hypothesis_id"] == unseen["hypothesis_id"])
        proved = f.adopt(actor, "read-surface", [surface], "limited_support", statement=unseen["statement"],
                         previous_claim_ref=current["claim_revision_id"])
        self.assertEqual((proved["level"], proved["maturity"]["material_coverage"][0]["qualification"]),
                         ("limited_support", "prospective"))
        self.write_trace("renamed-and-fresh-endpoint", renamed=fresh_id, repeated=recalc, fresh=proved)


class QuickPublication(unittest.TestCase):
    def test_current_quick_exploration_cannot_issue_empirical_support(self):
        from tempfile import TemporaryDirectory
        from acceptance.v62a.interleavings_fixture import Scenario, draft
        with TemporaryDirectory() as directory, Scenario(Path(directory) / "quick", spec_updates={
                "research_effort": "quick", "capabilities": [], "research_modules": [], "plugins": []}) as gx:
            raw = gx.materials[0]
            proposal = gx.call(gx.lead, "gx_stage_claim", {**original.BASE,
                               "level": "limited_support", "evidence_refs": [raw]}, "quick-proposal")
            receipt = gx.call(gx.lead, "gx_decide_revision", {"decision":
                              draft(evidence=[raw, proposal["claim_ref"]])}, "quick-decision")
            claim, = receipt["claim_revisions"]
            self.assertEqual((claim["level"], claim["science"]["level"], claim["lead_interpretation"]["status"]),
                             ("insufficient", "insufficient", "UNVERIFIED"))
            self.assertEqual(claim["publication"]["admissible_levels"], ["open", "insufficient"])
            self.assertEqual(v6_claims.read_revision(gx.conn, gx.run_id, claim["claim_revision_id"]), claim)



class DerivedMaterialChronology(unittest.TestCase):
    endpoint = "public.archive.instrument/1"

    def start(self, **policy):
        from acceptance.v62c import materials
        from acceptance.v623.test_source_revisions import RevisionIdentity
        from gx_core.p0 import v621_policy

        self.fixture = RevisionIdentity()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.start(**policy)
        self.flow = original.ClaimPublication()
        self.flow.gx, self.flow.folder = self.fixture.state, self.fixture.work
        self.assertEqual(v621_policy.pinned(self.flow.gx.conn, self.flow.gx.run_id),
                         policy.get("evidence_policy", v621_policy.VERSION))
        self.parent, self.independent = self.flow.gx.materials
        self.metadata = materials.parent_acquisition()
        self.scope = self.metadata["applicability"]
        code, receipt = self.fixture.command("observe", "archive-original", self.fixture.src,
                                             self.metadata, existing_ref=self.parent)
        self.assertEqual(code, 0, self.fixture.out[-1]["error"])
        self.parent_pin = {k: receipt[k] for k in ("artifact_ref", "sha256")}
        self.prior = self.reading("instrument-before", 7, self.parent)
        self.custody = self.fixture.work / "custody"
        self.slots = materials.prepare(self.custody)

    def reading(self, label, value, material):
        return self.flow.observe(label, {"schema": self.endpoint, "measurement": {"value": value}},
                                 data=[material], scope=self.scope)

    def assign(self, code):
        slot = self.slots[code]
        child = self.custody / code
        descriptor = {**self.metadata, "derived_from": [self.parent_pin],
                      "processing": ["coordinate-record withholding/" + slot["transform_sha256"]],
                      "transformation": json.loads((child / "transform.json").read_bytes()), "sha256": slot["sha256"]}
        rc, assigned = self.fixture.command("assign-material", "source-derivative-" + code, child / "specimen.cif", descriptor)
        self.assertEqual(rc, 0, self.fixture.out[-1]["error"])
        return assigned["artifact_ref"]

    def register(self, inputs):
        prediction = {"data_refs": inputs, "observable": "/measurement/value", "operator": "gt",
                      "threshold": 5, "measurement_schema": self.endpoint, "target": "experimental_observation"}
        return self.flow.adopt(self.flow.gx.next_actor("run"), "register-on-renamed-source", [self.prior], "open",
                               scope=self.scope, prediction=prediction, statement="The instrument reading exceeds five.")

    def interpret(self, refs):
        actor, current, _ = self.flow.delivered_claim()
        return self.flow.adopt(actor, "interpret-child", refs, "limited_support", scope=self.scope,
                               statement=current["statement"], previous_claim_ref=current["claim_revision_id"])

    def test_previously_observed_endpoint_on_source_cannot_validate_again_on_its_derivative(self):
        self.start(evidence_policy="gx.evidence/6.2.2", historical_replay=True)
        derivative = self.assign("P4")
        registration = self.register([derivative])
        item, = registration["maturity"]["material_coverage"]
        self.assertTrue(item["known_at_registration"])
        self.assertEqual(item["qualification"], "retrospective")
        later = self.reading("instrument-after", 8, derivative)
        retried = self.interpret([later])
        self.assertEqual(retried["level"], "insufficient")
        self.assertIn(later, retried["maturity"]["material_coverage"][0]["retrospective_refs"])

    def test_external_instrument_requires_its_own_endpoint_equivalence(self):
        self.start()
        derivative, sibling = self.assign("P4"), self.assign("K7")
        registered = self.register([self.parent, derivative, sibling, self.independent])
        rows = registered["publication"]["material_coverage"]
        self.assertEqual([r["qualification"] for r in rows],
                         ["retrospective", "timing_unknown", "timing_unknown", "unmeasured"])
        self.assertEqual([r["known_at_registration"] for r in rows], [True, False, False, False])
        for item in rows[1:3]:
            self.assertIsNone(item["endpoint_first_available_event_id"])
            self.assertEqual(item["material_identity_uncertainty"], ["unverified_endpoint_transformation"])
        # A reading on one child gives that child an availability time. Its relation to
        # the parent's instrument value is still unknown, and its sibling has no reading.
        later = self.reading("instrument-after", 8, derivative)
        fresh = self.reading("separate-deposition", 9, self.independent)
        result = self.interpret([later, fresh])
        self.assertEqual((result["level"], result["publication"]["direction"]), ("insufficient", "undetermined"))
        parent, child, adjacent, independent = result["publication"]["material_coverage"]
        self.assertEqual(parent["qualification"], "retrospective")
        self.assertEqual(child["qualification"], "timing_unknown")
        self.assertLess(child["registration_event_id"], child["endpoint_first_available_event_id"])
        self.assertEqual(child["uncertain_refs"], [later])
        self.assertEqual(adjacent["qualification"], "timing_unknown")
        self.assertIsNone(adjacent["endpoint_first_available_event_id"])
        self.assertFalse(adjacent["uncertain_refs"])
        self.assertFalse(child["supporting_refs"] or adjacent["supporting_refs"])
        self.assertEqual((independent["qualification"], independent["supporting_refs"]), ("prospective", [fresh]))
