"""Challenge the B5 evidence checks against changed records; no network or model calls."""
import copy
import json
import os
from pathlib import Path
import unittest

from grade import score


HERE = Path(__file__).resolve().parent


class GradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = json.loads((HERE / "case.json").read_bytes())
        cls.original = json.loads(Path(os.environ.get("GX_B5_TEST_EVIDENCE", HERE / "scripted/evidence.json")).read_bytes())

    def setUp(self):
        self.data = copy.deepcopy(self.original)
        self.checks = lambda: score(self.data, self.case)["checks"]
        self.history, = [a["body"]["rounds"] for a in self.data["artifacts"].values() if a["kind"] == "research_report"]

    def test_complete_control(self):
        self.assertTrue(all(self.checks().values()))

    def test_receipt_mismatch(self):
        self.data["official_products"][0]["request_sha256"] = "0" * 64
        self.assertFalse(self.checks()["accepted_products_match_native_calls"])

    def test_method_labels_swapped(self):
        decision = self.data["artifacts"][self.history[0]["artifact_ref"]]["body"]
        pts = decision["next_grids"]["high"]["candidates"]
        pts[0]["candidate_id"], pts[1]["candidate_id"] = pts[1]["candidate_id"], pts[0]["candidate_id"]
        self.assertFalse(self.checks()["alternative_method_choices"])
        self.assertFalse(self.checks()["decision_executed"])

    def test_source_supplied_only_after_review(self):
        self.data["messages"][0]["created_at"] = "2090-01-01T00:00:00Z"
        self.assertFalse(self.checks()["source_follow-up-controller-catalog"])

    def test_planner_rubber_stamps_contrary_score(self):
        decision = self.data["artifacts"][self.history[0]["artifact_ref"]]["body"]
        sid = next(s["source_id"] for s in self.data["sources"] if s["text"].startswith("Archived"))
        next(r for r in decision["source_decisions"] if r["source_id"] == sid)["disposition"] = "accepted"
        self.assertFalse(self.checks()["source_earlier-score-export"])

    def test_settlement_cites_other_path(self):
        settlements = [s for s in self.data["settlements"] if s["round_index"] == 0 and s["kind"] == "valid_scientific_outcome"]
        first, other = sorted(settlements, key=lambda s: s["path_id"])
        first["eval_id"] = other["eval_id"]
        self.assertFalse(self.checks()["reviewed_finding_chain_r0_" + first["path_id"]])

    def test_wrong_raw_score(self):
        evidence = self.data["artifacts"][self.history[1]["evidence_ref"]]["body"]
        raw = self.data["artifacts"][evidence["routes"][0]["observations_ref"]]["body"]
        raw["observations"][0]["score"] -= 1
        self.assertFalse(self.checks()["observation_chain_r1_high"])
        self.assertFalse(self.checks()["held_out_measured"])

    def test_self_review(self):
        reviewer = self.data["decisions"][0]["reviewer_submission_id"]
        entry = next(s for s in self.data["submissions"] if s["submission_id"] == reviewer)
        entry["principal"] = self.data["artifacts"][self.history[0]["proposal_ref"]]["body"]["author_principal"]
        self.assertFalse(self.checks()["independent_submission_r0"])


if __name__ == "__main__":
    unittest.main()
