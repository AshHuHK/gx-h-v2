"""Reports read the frozen registration, each reassessment and the original Job outputs."""
from pathlib import Path
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import branch, draft
from acceptance.v622.test_close import CloseStudy
from acceptance.science_execution.study import test_mapped_method as mapped
from gx_core.p0 import research_evidence, v62_maturity, v621_policy
from tests import test_v62_maturity as maturity_fixture


class ResearchEvidence(unittest.TestCase):
    def test_adopted_rules_and_each_later_assessment_remain_separate(self):
        f = maturity_fixture.ProspectiveClaims()
        f.setUp()
        self.addCleanup(f.doCleanups)
        staged = f.stage()
        self.assertEqual(research_evidence.evidence(f.db, f.run)["predictions"], [])
        adopted = f.adopt(staged)
        original = f.observe(12.5, 0)
        v62_maturity.reassess(f.db, f.run)
        f.observe(8, 1)
        v62_maturity.reassess(f.db, f.run)
        data = research_evidence.evidence(f.db, f.run)
        item, = data["predictions"]
        self.assertEqual(item["registration"]["event_id"], adopted["maturity"]["registration"]["event_id"])
        self.assertTrue(item["registration"]["utc"])
        self.assertEqual(item["scope"], f.scope)
        self.assertEqual(item["prediction"]["threshold"], 10)
        self.assertEqual([r["direction"] for r in item["assessments"]], ["undetermined", "supported", "mixed"])
        self.assertEqual(item["assessments"][1]["results"][0]["value"], 12.5)
        self.assertEqual(item["assessments"][1]["results"][0]["artifact_ref"], original["artifact_ref"])
        self.assertEqual(research_evidence.evidence(f.db, f.run), data)
        report = "\n".join(research_evidence.evidence_lines(data))
        for expected in (f.scope, original["artifact_ref"], "12.5", "mixed"):
            self.assertIn(expected, report)

    def test_production_before_adoption_remains_exploratory(self):
        f = maturity_fixture.ProspectiveClaims()
        f.setUp()
        self.addCleanup(f.doCleanups)
        staged = f.stage()
        known = f.observe(5, 0)
        f.adopt(staged)
        v62_maturity.reassess(f.db, f.run)
        item, = research_evidence.evidence(f.db, f.run)["predictions"]
        self.assertEqual(item["classification"], "contains_retrospective_evidence")
        assessment = item["assessments"][-1]
        gap = next(g for g in assessment["gaps"] if g["kind"] == "already_available")
        self.assertEqual(gap["artifact_ref"], known["artifact_ref"])
        self.assertEqual(assessment["results"], [])

    def test_one_hypothesis_retains_each_registration_and_its_own_assessments(self):
        f = maturity_fixture.ProspectiveClaims()
        f.setUp()
        self.addCleanup(f.doCleanups)
        original = f.adopt(f.stage(prediction={**f.prediction, "data_refs": f.plans[:1]}))
        first_reading = f.observe(12, 0)
        first, = v62_maturity.reassess(f.db, f.run)
        f.allow(first["claim_revision_id"])
        rule = {**f.prediction, "data_refs": f.plans[1:2], "threshold": 15}
        revised = f.adopt(f.stage(key="change-transect", previous_claim_ref=first["claim_revision_id"],
                                  statement="The temperature at the second transect exceeds fifteen degrees.",
                                  criteria="Temperature above 15 Celsius at the second transect.", prediction=rule))
        second_reading = f.observe(14, 1)
        second, = v62_maturity.reassess(f.db, f.run)
        f.allow(second["claim_revision_id"])
        posthoc = f.adopt(f.stage(key="change-after-reading", previous_claim_ref=second["claim_revision_id"],
                                  prediction={**rule, "threshold": 13}))

        data = research_evidence.evidence(f.db, f.run)
        item, = data["predictions"]
        revision, late = item["revisions"]
        self.assertEqual([v["registration"]["claim_revision_id"] for v in (item, revision, late)],
                         [c["claim_revision_id"] for c in (original, revised, posthoc)])
        self.assertEqual([v["prediction"]["threshold"] for v in (item, revision, late)], [10, 15, 13])
        self.assertEqual([v["classification"] for v in (item, revision, late)],
                         ["prospectively_tested", "prospectively_tested", "contains_retrospective_evidence"])
        for version, assessment, observation, direction in ((item, first, first_reading, "supported"),
                                                            (revision, second, second_reading, "opposed")):
            self.assertEqual([a["direction"] for a in version["assessments"]], ["undetermined", direction])
            self.assertEqual(version["assessments"][-1]["claim_revision_id"], assessment["claim_revision_id"])
            result, = version["assessments"][-1]["results"]
            self.assertEqual(result["artifact_ref"], observation["artifact_ref"])
            self.assertEqual(result["lead_delivery_event_ids"], [])
            self.assertEqual(result["denominators"], {})
            self.assertIn("unit", result)
        self.assertTrue(late["assessments"][0]["preexisting_result"])
        report = "\n".join(research_evidence.evidence_lines(data))
        self.assertEqual(report.count("\n### "), 1)
        self.assertEqual(report.count("\n#### Revised registration"), 2)
        self.assertIn(second_reading["artifact_ref"], report)

    def test_named_output_has_original_sha_parameters_readings_and_delivery_status(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        raw, mapping = mapped.example()
        source, mapfile = work / "specimen.cif", work / "map.json"
        source.write_bytes(raw)
        mapfile.write_bytes(mapping)
        grant = {"research_effort": "deep", "evidence_policy": v621_policy.VERSION, "research_modules": [], "plugins": [mapped.PACKAGE],
                 "capabilities": [mapped.METHOD], "input_files": [
                     {"role": "structure", "path": str(source)}, {"role": "mapping", "path": str(mapfile)}]}
        with CloseStudy(work / "goal", spec_updates=grant) as s:
            params = {"interval": "21-40", "endpoint": "rmsd_A", "mask": "common_backbone"}
            op = s.node(s.lead, "primary", s.materials[0], branch_id="local:measure",
                        capability=mapped.METHOD, parameters=params)
            op["inputs"] = s.materials
            rule = dict(branch("measure"), budget_limits={"cpu_seconds": 120, "job_submissions": 1})
            accepted = s.call(s.lead, "gx_decide_revision", {"decision": draft(rule, op)}, "launch")
            done = s.settle(accepted["aliases"]["local:primary"])
            data = research_evidence.evidence(s.conn, s.run_id)
            job, = data["jobs"]
            self.assertEqual(job["job_id"], done["job_id"])
            self.assertEqual(job["parameters"], params)
            self.assertEqual(job["inputs"][0]["artifact_ref"], s.materials[0])
            self.assertTrue(job["inputs"][0]["sha256"])
            output = next(o for o in job["outputs"] if o["output_name"] == "segment.json")
            self.assertEqual(output["artifact_ref"], done["result_ref"])
            self.assertEqual(output["summary"]["selected_residues"], 150)
            self.assertTrue(output["metrics"])
            self.assertEqual(output["lead_delivery_event_ids"], [])
            s.next_actor("run")
            actual = research_evidence.evidence(s.conn, s.run_id)
            refreshed = next(o for o in actual["jobs"][0]["outputs"] if o["artifact_ref"] == done["result_ref"])
            self.assertTrue(refreshed["lead_delivery_event_ids"])
            self.assertTrue(any(m["lead_delivery_event_ids"] for m in refreshed["metrics"]))
            self.assertIn("21-40", "\n".join(research_evidence.evidence_lines(data)))


if __name__ == "__main__":
    unittest.main()
