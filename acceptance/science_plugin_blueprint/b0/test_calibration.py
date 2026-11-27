import importlib.util
import json
from pathlib import Path
import tempfile
import sys
from unittest.mock import patch
import unittest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PKG = ROOT / "gx-h/plugins/installed/org.gx.archival-fold-calibration/1.0.0"
CONTROLS = ROOT / "acceptance/science_capabilities/structure_controls"


def module(name):
    spec = importlib.util.spec_from_file_location(name, PKG / "job-scripts" / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    # Plugin packages are content-pinned; imports during tests must leave their bytes intact.
    with patch.object(sys, "dont_write_bytecode", True):
        spec.loader.exec_module(loaded)
    return loaded


class OfflineCalibration(unittest.TestCase):
    def setUp(self):
        self.mapping = json.loads((HERE / "mapping.json").read_bytes())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def calculate(self):
        mapping = Path(self.temp.name) / "map.json"
        mapping.write_text(json.dumps(self.mapping))
        return module("score").score(
            CONTROLS / "native_prediction/published_top7_1qys_model.cif",
            CONTROLS / "materials/1QYS.cif", mapping)

    def test_known_archival_reading(self):
        r = self.calculate()
        self.assertEqual((r["matched_positions"], r["mapped_positions"]), (92, 92))
        self.assertAlmostEqual(r["calpha_rmsd_A"], 1.011876, places=6)

    def test_missing_coordinates_remain_in_denominator(self):
        self.mapping["pairs"][-1] = [92, 105]
        r = self.calculate()
        self.assertEqual((r["matched_positions"], r["mapped_positions"]), (91, 92))
        self.assertEqual(r["missing_pairs"], [
            {"pair": [92, 105], "prediction_missing": False, "reference_missing": True}])

    def test_ambiguous_mapping_fails(self):
        self.mapping["pairs"][-1][0] = 1
        with self.assertRaisesRegex(ValueError, "one-to-one"):
            self.calculate()

    def test_model_output_and_scientific_preregistration_are_distinct(self):
        with patch.object(sys, "path", [str(ROOT / "gx-h"), *sys.path]):
            from gx_core.p0.research_page import page
        result = json.loads((HERE / "result.json").read_bytes())
        self.assertEqual(result["predictions"], [])
        self.assertTrue(any(o.get("nature") == "model_prediction"
                            for j in result["jobs"] for o in j["outputs"]))
        self.assertIn("No prospective scientific prediction was registered.", page(result))
        result["presentation"] = {"language": "zh"}
        self.assertIn("本题未预先登记科学预测。", page(result))

    def test_nonarchived_input_denied_before_inference(self):
        import contextlib
        import io
        fake = Path(self.temp.name) / "empty.json"
        fake.write_text("{}")
        cmd = ["predict", "--specimen", str(fake), "--weight", str(fake), "--mount",
               self.temp.name, "--out", self.temp.name]
        with patch("sys.argv", cmd), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            module("predict").main()


if __name__ == "__main__":
    unittest.main()
