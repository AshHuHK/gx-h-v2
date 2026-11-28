"""Normal Goal publication plus deterministic variations of a previously seen endpoint."""
import json
import os
from pathlib import Path
import unittest

from acceptance.v621 import test_claim_publication as original
from acceptance.v6.export import capture
from acceptance.v6.project_window import Capture


def observe(f, name, scope, raw, *, data=None, acquisition=None):
    path = f.folder / (name + ".json")
    metadata = f.folder / (name + "-acquisition.json")
    path.write_bytes(raw)
    identity = dict(data="thermometer-42/visit-01", acquisition="thermometer-log/01", sample="morning/01",
                    source="north-station/instrument-42", sampling="north-station/visit-01",
                    processing="calibrated Kelvin-to-Celsius reader/1", publication=[])
    metadata.write_text(json.dumps(dict(identity, applicability=scope, data_refs=f.gx.materials[:1] if data is None else data)
                                   | (acquisition or {})))
    return f.cli("observe", "--source", name, "--input", path, "--acquisition", metadata)["artifact_ref"]


class KnownEndpoint(unittest.TestCase):
    def test_same_acquisition_new_hypothesis_scope_and_serialization(self):
        self.exercise(historical=False)

    def test_explicit_historical_replay_keeps_the_original_publication_and_contract(self):
        self.exercise(historical=True)

    def exercise(self, *, historical):
        f = original.ClaimPublication()
        self.addCleanup(f.doCleanups)
        if historical:
            from unittest.mock import patch
            from acceptance.v62a.interleavings_fixture import Scenario
            def replay(directory, *, spec_updates):
                return Scenario(directory, spec_updates={**spec_updates, "historical_replay": True,
                                "evidence_policy": "gx.evidence/6.2.2"})
            with patch.object(original, "Scenario", replay):
                f.start()
        else:
            f.start()
        first = observe(f, "original", original.SCOPE, b'{"schema":"public.thermometer.celsius/1","measurement":{"value":7}}')
        actor, previous, _ = f.delivered_claim()
        self.assertEqual(previous["maturity"]["direction"], "opposed")
        scope = original.SCOPE + "."
        statement = "The observed morning temperature exceeds five degrees Celsius."
        rule = dict(f.rule, threshold=5)
        if historical:
            from gx_core.errors import SchemaInvalid
            with self.assertRaises(SchemaInvalid):
                f.gx.call(actor, "gx_stage_claim", dict(original.BASE, level="open", evidence_refs=[first],
                          prediction=dict(rule, external_endpoint={"unit":"C"})), "attempt-new-policy-field")
        new = f.adopt(actor, "independent-proposal", [first], "open", statement=statement, scope=scope, prediction=rule)
        repacked = observe(f, "copied-acquisition", scope,
                           b'{ "measurement": {"value": 7.0}, "schema": "public.thermometer.celsius/1" }\n')
        actor = f.gx.next_actor("run")
        before = next(c for c in f.cli("status")["claims"] if c["hypothesis_id"] == new["hypothesis_id"])
        adopted = f.adopt(actor, "publish-after-resupply", [repacked], "limited_support", statement=statement,
                          scope=scope, previous_claim_ref=before["claim_revision_id"])
        out = f.folder / "export"
        capture(f.gx.state / "gx.db", f.gx.run_id, out)
        exported = Capture(out).claim(adopted["hypothesis_id"])
        self.assertEqual(exported["publication"], adopted["publication"])
        events = f.gx.rows("SELECT event_id,kind,subject_id,payload_json FROM events WHERE run_id=? "
                           "AND kind IN ('artifact.published','v6.observation.added','v6.claim.adopted') "
                           "ORDER BY event_id", f.gx.run_id)
        if historical:
            self.assertEqual(adopted["study_class"], "historical_replay")
            self.assertEqual(exported["level"], "limited_support")
            self.assertEqual(adopted["maturity"]["registration"]["policy"], "gx.claim-proposition/2")
            from acceptance.v623.test_chronology_supplied import case_receipt
            case_receipt("historical", policy="gx.evidence/6.2.2", original=previous, exported=exported)
            return
        if where := os.environ.get("GX_V623_E1_RECEIPT"):
            Path(where).write_text(json.dumps(dict(case="E1", policy=f.cli("status").get("evidence_policy"),
                    input_refs=[first, repacked], run_id=f.gx.run_id, evidence_events=events, original=previous,
                    new_registration=new, proposal=adopted, external=exported, external_paid_calls=0), indent=2)+"\n")
        self.assertEqual(adopted["level"], "insufficient", "re-encoding a known 7°C reading gave a new hypothesis support")
        p = adopted["publication"]
        self.assertEqual(p["retrospective_data_refs"], f.gx.materials[:1])
        self.assertEqual(p["unmeasured_data_refs"], [])
        self.assertEqual(p["missing_prospective_data_refs"], f.gx.materials[:1])
        coverage, = p["material_coverage"]
        self.assertLess(coverage["endpoint_first_available_event_id"], new["maturity"]["registration"]["event_id"])
        self.assertEqual(coverage["endpoint_first_available_event_id"],
                         previous["maturity"]["material_coverage"][0]["endpoint_first_available_event_id"])
        from gx_core.p0 import v623_chronology, v6_native
        root = f.gx.conn.execute("SELECT * FROM p0_roots WHERE lease_id=?", (actor.lease_id,)).fetchone()
        packet, received = v6_native.acknowledged_brief(f.gx.conn, root, actor.turn)
        delivered = v623_chronology.delivered_external(f.gx.conn, packet)
        self.assertTrue(any(m["artifact_ref"] == repacked and m["value"] == 7 for m in delivered))
        self.assertEqual(received["text_sha256"], adopted["decision_delivery"]["brief_sha256"])
        clipped = {**packet, "observations": [{k: v for k, v in item.items() if k != "observation"}
                                            for item in packet["observations"]]}
        self.assertEqual(v623_chronology.delivered_external(f.gx.conn, clipped), [])


class GeneratedEndpointVariants(unittest.TestCase):
    def test_seeded_known_acquisition_is_invariant_under_json_and_scope_variants(self):
        import random
        from gx_core.p0 import v623_chronology
        from acceptance.v623.test_chronology_supplied import case_receipt
        from acceptance.v62a.interleavings_fixture import draft
        seed = 6230715
        rng = random.Random(seed)
        corpus = [original.SCOPE, original.SCOPE + ".", "North station — morning samples.",
                  "北站早晨採集的樣本", "北站的晨間樣本。"]
        f = original.ClaimPublication()
        self.addCleanup(f.doCleanups)
        f.start()
        raw = b'{"schema":"public.thermometer.celsius/1","measurement":{"value":7}}'
        first = observe(f, "acquisition-one", original.SCOPE, raw)
        actor, prior, _ = f.delivered_claim()
        frozen = prior["maturity"]["registration"]
        availability = prior["maturity"]["material_coverage"][0]["endpoint_first_available_event_id"]
        pin, = frozen["data"]
        proposals = []
        for i, scope in enumerate(corpus):
            payload = dict(original.BASE, statement="Hypothesis " + chr(ord("A") + i),
                           scope=scope, level="open", evidence_refs=[first], prediction=dict(f.rule, threshold=5))
            proposals.append(f.gx.call(actor, "gx_stage_claim", payload, "candidate-" + str(i))["claim_ref"])
        adopted = f.gx.call(actor, "gx_decide_revision", {"decision": draft(evidence=[first, *proposals])}, "adopt-variants")
        for claim in adopted["claim_revisions"]:
            check, = claim["publication"]["material_coverage"]
            self.assertEqual((check["qualification"], check["endpoint_first_available_event_id"]), ("retrospective", availability))
        variants = []
        for i in range(12):
            scope = rng.choice(corpus)
            body = [("schema", original.METRIC), ("measurement", {"value": rng.choice((7, 7.0))}),
                    ("attachment", {"copy": i})]
            rng.shuffle(body)
            raw = json.dumps(dict(body), indent=rng.choice((None, 1, 3)), ensure_ascii=bool(i % 2)).encode()
            ref = observe(f, "variation-" + str(i), scope, raw)
            event = f.gx.conn.execute("SELECT MAX(event_id) FROM events").fetchone()[0]
            variants.append({"ordinal": i, "scope": scope, "input": raw.decode(), "ref": ref, "as_of": event})
            with self.subTest(seed=seed, minimal_input=variants[-1]):
                self.assertEqual(v623_chronology.availability(f.gx.conn, frozen, event)[pin["sha256"]]
                                 ["endpoint_first_available_event_id"], availability)
        case_receipt("generated", seed=seed, corpus=corpus, earliest=availability, original_ref=first, variants=variants,
                     scope_variants=len(proposals), replayed_serializations=len(variants))
