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
