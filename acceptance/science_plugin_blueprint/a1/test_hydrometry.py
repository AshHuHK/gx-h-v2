"""Independent hand and exhaustive interval controls for the two external author packages."""
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def load(package):
    source = ROOT / "examples/plugins" / package / "worker.py"
    spec = importlib.util.spec_from_file_location(package.replace("-", "_"), source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


paired = load("hydro-concurrent")
totals = load("hydro-marginal")


class TestHydrometry(unittest.TestCase):
    def test_stepwise_overlap_against_independent_minute_sums(self):
        raw = (HERE / "positive/material.json").read_bytes()
        data = json.loads(raw)
        rows = data["intervals"]
        observed = []
        for minute in range(*data["window_minutes"]):
            at_time = {r["channel"]:r for r in rows if r["valid"] and r["start"] <= minute < r["end"]}
            if len(at_time) == 2:
                observed.append((at_time["West"], at_time["East"]))
        west = 60 * sum(w["q_L_s"] for w, e in observed)
        east = 60 * sum(e["q_L_s"] for w, e in observed)
        radius = 60 * sum(w["err_L_s"] + e["err_L_s"] for w, e in observed)
        result = paired.measure(raw)
        for key, value in {"left_L":west, "right_L":east, "common_minutes":len(observed),
                           "difference_low_L":west-east-radius, "difference_high_L":west-east+radius}.items():
            self.assertAlmostEqual(result[key], value)
        self.assertLess(result["common_minutes"], 120)
        self.assertGreater(result["difference_low_L"], 0)
        margins = totals.measure(raw)
        self.assertNotEqual(margins["left_L"] - margins["right_L"], result["difference_L"])

    def test_zero_coverage_preserves_unknown_and_overlapping_rows_rejected(self):
        doc = json.loads((HERE / "positive/material.json").read_bytes())
        doc["intervals"] = [r for r in doc["intervals"] if r["channel"] == "East"]
        empty = paired.measure(json.dumps(doc).encode())
        self.assertEqual(empty["common_minutes"], 0)
        self.assertEqual(empty["support"], "no_shared_observations")
        self.assertIsNone(empty["right_mean_L_s"])
        doc["intervals"].append(doc["intervals"][0])
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            paired.measure(json.dumps(doc).encode())

    def test_equal_marginals_allow_opposite_paired_signs(self):
        raw = (HERE / "negative/material.json").read_bytes()
        marginal = totals.measure(raw)
        self.assertEqual((marginal["left_L"], marginal["right_L"]), (9000, 8100))
        self.assertNotIn("difference_L", marginal)
        with self.assertRaisesRegex(ValueError,"time-resolved"):
            paired.measure(raw)
        # Left observes 0–75, right 45–120, so overlap is 45–75.
        # Both reconstructions reproduce the exact published station totals.
        for left_shared, right_shared, sign in [(6000, 2700, 1), (3000, 5400, -1)]:
            west_only, east_only = 9000-left_shared, 8100-right_shared
            self.assertEqual(west_only+left_shared, marginal["left_L"])
            self.assertEqual(east_only+right_shared, marginal["right_L"])
            self.assertGreater(sign*(left_shared-right_shared), 0)


if __name__ == "__main__":
    unittest.main()
