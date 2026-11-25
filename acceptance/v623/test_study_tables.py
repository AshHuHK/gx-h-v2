"""Round-trip the table export using only synthetic coordinate products."""
import csv
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from acceptance.v623.study.tables import export
from acceptance.v623.test_block_method import FITTER, module, synthetic_cif


class StudyTables(unittest.TestCase):
    def test_partial_intervals_units_hashes_controls_and_aggregate(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        engine = module("block_influence")
        fitter = engine.load_fitter(FITTER)
        paths = []
        for mask in ("common_backbone", "common_ca"):
            product = engine.measure(synthetic_cif(size=105, missing=True), mask, fitter)
            path = root / f"{mask}.json"
            path.write_text(json.dumps(product) + "\n")
            paths.append(path)
        output = root / "export"
        receipt = export(paths, output)
        self.assertEqual(receipt["pair_intervals.csv"]["rows"], 72)
        self.assertEqual(receipt["deposit_summary.csv"]["rows"], 4)
        with (output / "pair_intervals.csv").open(encoding="utf-8", newline="") as stream:
            rows = [r for r in csv.DictReader(stream) if r["mask"] == "common_ca"]
        source_sha = sha256(paths[1].read_bytes()).hexdigest()
        for row in rows:
            b = product["blocks"][int(row["block_index"])]
            p, e = row["pair"], row["endpoint"]
            self.assertEqual(row["product_sha256"], source_sha)
            self.assertEqual(row["matched_keys_sha256"], product["matched_keys_sha256"])
            self.assertEqual(float(row["baseline"]), product["baseline"][p][e])
            self.assertEqual(json.loads(row["eligible"]), b["eligible"])
            if b["eligible"]:
                actual = b["pairs"][p]
                self.assertEqual(float(row["absolute_delta"]), actual["delta_" + e])
                self.assertEqual(float(row["threshold_margin"]), actual[e + "_assessment"]["margin"])
                self.assertEqual(float(row["control_max"]), actual[e + "_assessment"]["control_max"])
                self.assertEqual(int(row["valid_control_sets"]), 20)
                self.assertEqual(json.loads(row["median_concealed"]), b["median"][e]["concealed"])
            else:
                self.assertTrue(json.loads(row["eligibility_reasons"]))
                self.assertEqual(row["absolute_delta"], "")
                self.assertEqual(row["over_threshold"], "")
        with (output / "deposit_summary.csv").open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                self.assertEqual(row["unit"], "degree" if row["endpoint"] == "angle_deg" else "angstrom")
                self.assertEqual(int(row["eligible_pair_cells"]) + int(row["ineligible_blocks"]) * 3, 18)
        with self.assertRaises(FileExistsError):
            export(paths, output)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            export([paths[0], paths[0]], root/"duplicated")
        self.assertFalse((root/"duplicated").exists())


if __name__ == "__main__":
    unittest.main()
