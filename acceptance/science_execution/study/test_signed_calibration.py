"""Replay the two historical control depositions and compare with the published full-precision fits."""
import csv
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
HARNESS = ROOT / 'gx-h'
INSTALLED = HARNESS / 'plugins/installed'
SIGNED = INSTALLED / 'org.gx.block-signed/1.0.0/methods/signed.py'
ENGINE = INSTALLED / 'org.gx.block-influence/1.0.0/methods/block_influence.py'
FITTER = HARNESS / 'adapters/scientific/tl1a_geometry/worker.py'
ARCHIVE = ROOT / 'acceptance/v623'


def reference_class(pair_changes, median_change, e, z):
    affected = [v for v in pair_changes if abs(v) >= e]
    if not affected:
        return "quiet"
    if abs(median_change) >= e:
        return "opposed" if any(v * median_change < 0 for v in affected) else "represented"
    return "near_zero" if abs(median_change) < z else "attenuated"


def calibration():
    with (ARCHIVE / 'evidence/study/tables/pair_intervals.csv').open() as stream:
        historical = list(csv.DictReader(stream))
    controls = json.loads((ARCHIVE / 'study/operator/reference_protocol.json').read_bytes())["inputs"]
    reports = []
    for control in controls:
        structure = ROOT / control["repository_path"]
        for mask in ("common_backbone", "common_ca"):
            for ep, e, z in (("angle_deg", Decimal("0.30"), Decimal("0.10")),
                             ("rmsd_A", Decimal("0.15"), Decimal("0.05"))):
                with tempfile.TemporaryDirectory() as temp:
                    out = Path(temp)
                    subprocess.run([sys.executable, "-I", "-B", str(SIGNED), "--structure", str(structure),
                                    "--engine", str(ENGINE), "--fitter", str(FITTER), "--mask", mask,
                                    "--endpoint", ep, "--effect-minimum", str(e), "--zero-limit", str(z),
                                    "--minimum-evaluable", "3", "--out", str(out)], check=True)
                    summary = json.loads((out / 'signed-summary.json').read_bytes())
                    details = json.loads((out / 'signed-blocks.json').read_bytes(), parse_float=Decimal)
                assert details["input_sha256"] == summary["input_sha256"] == control["sha256"]
                expected = [r for r in historical if (r["entry"], r["mask"], r["endpoint"]) ==
                            (control["entry"], mask, ep)]
                observed = {row["block_index"]: row for row in details["blocks"]}
                assert set(observed) == {int(r["block_index"]) for r in expected}
                checked = []
                for i, block in observed.items():
                    rows = [r for r in expected if int(r["block_index"]) == i]
                    assert len(rows) == 3
                    if rows[0]["eligible"] == "false":
                        assert block["classification"] == "ineligible" and block["reasons"]
                    else:
                        changes = []
                        for row in rows:
                            a, b = Decimal(row["baseline"]), Decimal(row["after_deletion"])
                            reported = block["pairs"][row["pair"]]
                            assert reported["before"] == a and reported["after"] == b
                            assert abs(reported["delta"] - (b-a)) < Decimal("1e-14")
                            changes.append(b-a)
                        med_a, med_b = (Decimal(rows[0][k]) for k in ("median_before", "median_after"))
                        assert block["median"]["before"] == med_a and block["median"]["after"] == med_b
                        assert abs(block["median"]["delta"] - (med_b-med_a)) < Decimal("1e-14")
                        assert block["classification"] == reference_class(changes, med_b-med_a, e, z)
                    checked.append({"interval": block["interval"], "classification": block["classification"]})
                hit_count = sum(r["classification"] in {"opposed", "near_zero", "attenuated"} for r in checked)
                n_measured = sum(r["classification"] != "ineligible" for r in checked)
                assert summary["blocks_missed_observed"] == hit_count and summary["blocks_evaluable"] == n_measured == 8
                assert summary["blocks_unmeasured"] == 0 and summary["missed_present"] == int(hit_count > 0)
                report = {"summary": summary, "intervals": checked}
                if mask == "common_backbone" and ep == "angle_deg":
                    example_index = 5 if control["entry"] == "2RE9" else 1
                    report["known_control_interval"] = observed[example_index]
                reports.append(report)
    return reports


class SignedHistoricalCalibration(unittest.TestCase):
    def test_two_depositions_two_masks_two_endpoints(self):
        reports = calibration()
        self.assertEqual(len(reports), 8)
        self.assertEqual(sum(r["summary"]["blocks_evaluable"] for r in reports), 64)
        self.assertEqual(sum(r["summary"]["blocks_total"] for r in reports), 76)


if __name__ == "__main__":
    unittest.main()
