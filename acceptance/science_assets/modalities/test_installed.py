"""Exercise the two scientific scripts through the normal approved-package loader and native Goal catalogue."""
from pathlib import Path
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import Scenario


class ArchivedAnalysis(unittest.TestCase):
    def test_fresh_archived_goal_displays_each_approved_script_and_its_ordered_ports(self):
        policy = {"evidence_policy":"gx.evidence/6.2.4", "research_effort":"deep", "research_modules":[],
                  "capability_collections":["archived_structure"], "input_files":[]}
        with tempfile.TemporaryDirectory() as directory, Scenario(Path(directory), spec_updates=policy) as case:
            for index, (ref, inputs, filename) in enumerate([
                ("archival-peptide-coverage@1", ["structure"], "peptide-qc.json"),
                ("archival-fab-coverage@1", ["numbering","structure"], "fab_coverage.json")
            ]):
                card = case.call(case.lead, "gx_query_catalog", {"kind":"method", "reference":ref}, "read-"+str(index))
                self.assertEqual(card["spec"]["inputs"], inputs)
                self.assertIn(filename, [out["name"] for out in card["spec"]["outputs"]])
                case.call(case.lead,"gx_select_capability",{"action":"enable","method":ref,"revision":index},
                          "choose-"+str(index))
            self.assertEqual(case.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
