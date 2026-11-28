"""Exercise the released method's input boundary without launching the prediction."""
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PKG = ROOT / "gx-h/plugins/installed/org.gx.natural-food-fold/1.0.0"
spec = importlib.util.spec_from_file_location("natural_food_forward", PKG / "job-scripts/predict.py")
predict = importlib.util.module_from_spec(spec)
spec.loader.exec_module(predict)


class NativeSpecimen(unittest.TestCase):
    def setUp(self):
        self.library = json.loads((PKG / "specimens.json").read_bytes())
        self.specimens = [json.loads((HERE / "materials" / (acc + ".json")).read_bytes())
                          for acc in self.library["specimens"]]

    def test_distinct_archived_inputs_produce_distinct_new_requests(self):
        requests = [predict.request_for(s, self.library) for s in self.specimens]
        self.assertEqual(len(requests), 2)
        for s, request in zip(self.specimens, requests, strict=True):
            protein = request["sequences"][0]["protein"]
            self.assertEqual(protein["sequence"], s["sequence"])
            self.assertEqual(protein["templates"], [])
            self.assertEqual(request["modelSeeds"], [1])
        self.assertNotEqual(requests[0]["sequences"], requests[1]["sequences"])

    def test_mismatched_or_unregistered_accessions_are_rejected(self):
        left, right = self.specimens
        for item in [dict(left, accession=right["accession"]), dict(left, accession="unknown"),
                     dict(left, sequence=left["sequence"][:-1])]:
            with self.assertRaisesRegex(ValueError, "registered"):
                predict.request_for(item, self.library)

    def test_no_supplied_templates_or_parameters(self):
        with self.assertRaisesRegex(ValueError, "accession and sequence"):
            predict.request_for(dict(self.specimens[0], templates=["reference"]), self.library)

if __name__ == "__main__":
    unittest.main()
