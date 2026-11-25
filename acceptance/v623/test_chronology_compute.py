"""E2/E4: actual CPU output becomes available after the raw coordinates; replay is stable."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
import random
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from acceptance.v621 import test_claim_publication as claims
from acceptance.v622 import test_measurement as measurement
from acceptance.v62a.interleavings_fixture import PRODUCT, Scenario, branch, draft
from gx_core.p0 import v6_goal, v6_jobs, v62_maturity, v622_measurement, v623_chronology
from gx_core.persistence import store


class ComputedAvailability(unittest.TestCase):
    def test_cpu_endpoint_registration_scope_and_cold_parallel_reconciliation(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        s = self.enterContext(Scenario(directory / "goal", spec_updates={"research_effort": "deep",
            "research_modules": [], "plugins": ["org.gx.protein@0.3.0"], "capabilities": [measurement.METHOD]}))
        raw = s.materials[0]
        acq = json.loads((PRODUCT / "examples/v621-coordinate-analysis/2RE9.json").read_text())
        v6_goal.record_observation(s.conn, s.run_id, raw, source="coordinate-archive",
                                   acquisition=dict(acq, applicability=measurement.SCOPE))
        def prediction(mask):
            return {"target": "analysis", "method": {"capability": measurement.METHOD, "output": "geometry.json",
                    "parameters": {"mask": mask}, "metric_id": "coverage", "unit": "1", "input_role": "structure"},
                    "observable": "/metrics/coverage", "measurement_schema": "gx.deposited_chain_geometry/2",
                    "operator": "gt", "threshold": 0.6, "data_refs": [raw]}
        def stage(actor, label, mask, scope, ref):
            return s.call(actor, "gx_stage_claim", dict(claims.BASE, statement=label, scope=scope,
                    level="open", evidence_refs=[ref], prediction=prediction(mask)), "stage-" + label)["claim_ref"]
        first = stage(s.lead, "First preregistration", "ordered_backbone", measurement.SCOPE, raw)
        node = s.node(s.lead, "read", raw, branch_id="local:measures", capability=measurement.METHOD,
                      parameters={"mask": "ordered_backbone"})
        accepted = s.call(s.lead, "gx_decide_revision", {"decision": draft(dict(branch("measures"), budget_limits={"job_submissions": 2, "cpu_seconds": 120}), node, evidence=[raw, first],
            watches=[{"event": "job_settled", "subject_ref": "local:read"}])}, "plan")
        reg = accepted["claim_revisions"][0]["maturity"]["registration"]
        self.assertFalse(reg["known_at_registration"][reg["data"][0]["sha256"]])
        self.assertEqual(reg["chronology_scope"], "same_store")
        result = s.settle(accepted["aliases"]["local:read"])
        # Reconcile through separate connections after an actual detached Job completed.
        db = s.state / "gx.db"
        def reconcile(_):
            with closing(store.open_store(db)) as cold:
                v6_jobs.advance(cold, s.run_id)
                return cold.execute("SELECT count(*) FROM jobs WHERE run_id=?", (s.run_id,)).fetchone()[0]
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(reconcile, range(2))), [1, 1])
        actor = s.next_actor("run")
        prior = next(c for c in s.brief(actor)["claims"] if c["statement"] == "First preregistration")
        cov, = prior["maturity"]["material_coverage"]
        self.assertEqual(cov["qualification"], "prospective")
        self.assertLess(cov["raw_material_received_event_id"], reg["event_id"])
        self.assertLess(reg["event_id"], cov["endpoint_first_available_event_id"])
        measured, = prior["maturity"]["results"]
        self.assertEqual(measured["first_event_id"], cov["endpoint_first_available_event_id"])
        self.assertEqual(measured["measurement"]["measurement_spec_sha256"], reg["measurement_spec_sha256"])
        ref = result["result_ref"]
        if ref not in s.brief(actor)["evidence_refs"]:
            s.call(actor, "gx_decide_revision", {"decision": draft(watches=[{"event":"job_settled","subject_ref":ref}])}, "next")
            actor = s.next_actor("run")
        # The same endpoint is already available, even with another label. A genuinely new mask is unmeasured.
        current = stage(actor, "Another proposition", "ordered_backbone", measurement.SCOPE + ".", raw)
        alternate = stage(actor, "A different selection", "common_ca", measurement.SCOPE, raw)
        adopted = s.call(actor, "gx_decide_revision", {"decision": draft(evidence=[raw, current, alternate])}, "compare")
        publications = {c["statement"]: c for c in adopted["claim_revisions"]}
        historical = publications["Another proposition"]["publication"]
        self.assertEqual(historical["retrospective_data_refs"], [raw])
        self.assertEqual(historical["unmeasured_data_refs"], [])
        self.assertEqual(historical["material_coverage"][0]["endpoint_first_available_event_id"], cov["endpoint_first_available_event_id"])
        alternate = publications["A different selection"]["publication"]
        self.assertEqual(alternate["unmeasured_data_refs"], [raw])
        self.assertIsNone(alternate["material_coverage"][0]["endpoint_first_available_event_id"])
        with closing(store.open_store(db)) as cold:
            retrieved = v62_maturity.recompute(cold, s.run_id, prior["claim_revision_id"])
            self.assertEqual(retrieved["material_coverage"], [cov])
        seed = 6230716
        rng = random.Random(seed)
        sequence = []
        cursor = s.conn.execute("SELECT MAX(event_id) FROM events").fetchone()[0]
        for i in range(12):
            tested_mask = rng.choice(("ordered_backbone", "common_ca"))
            pred = dict(prediction(tested_mask), observable=rng.choice(("/metrics/coverage", "/coverage_fraction")),
                        threshold=rng.choice((0.3, 0.6, 0.8)))
            fields = list(pred["method"].items())
            rng.shuffle(fields)
            pred["method"] = dict(fields)
            scope = rng.choice((measurement.SCOPE, "公開座標檔內的沉積鏈。", measurement.SCOPE + "."))
            thru = rng.choice((reg["event_id"], cov["endpoint_first_available_event_id"], cursor))
            pin = v622_measurement.pin(s.conn, s.run_id, pred, applicability=scope)
            probe = dict(reg, method_pin=pin, prediction=pred)
            variant = dict(scope=scope, prediction=pred, as_of=thru)
            with self.subTest(seed=seed, minimal_input=variant):
                t = v623_chronology.availability(s.conn, probe, thru)[cov["sha256"]]
                expected = cov["endpoint_first_available_event_id"] if tested_mask == "ordered_backbone" and (
                           thru >= cov["endpoint_first_available_event_id"]) else None
                self.assertEqual(t["endpoint_first_available_event_id"], expected)
            sequence.append(variant)
        if path := os.environ.get("GX_V623_E4_JOB_RECEIPT"):
            Path(path).write_text(json.dumps(dict(run_id=s.run_id, original_registration=reg, job=result, original=prior,
                changed_scope=historical, alternative=alternate, jobs_after_reconcile=1, cold_coverage=retrieved["material_coverage"],
                generated_seed=seed, generated_variants=sequence, external_paid_calls=0), indent=2) + "\n")
