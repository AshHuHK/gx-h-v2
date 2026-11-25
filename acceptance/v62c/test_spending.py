"""Prove why a successful offline boundary probe still blocks enrollment."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


class PaidBoundary(unittest.TestCase):
    def test_paid_check_fails_closed_with_measured_root_and_module_overshoot(self):
        run = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("spending.py")),
                              "--require-paid-ready"], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 2, run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["paid_gate"], "BLOCKED")
        self.assertEqual(report["paid_provider_calls"], 0)
        self.assertEqual(report["private_prior_denominator"], "0/2")
        self.assertIsNone(report["official_provider_tokens"])
        self.assertIsNone(report["official_provider_amount"])
        self.assertEqual({r["charged_role"] for r in report["scripted_cases"]}, {"lead", "module"})
        for row in report["scripted_cases"]:
            with self.subTest(architecture=row["architecture"]):
                self.assertEqual(row["turn_grant"], row["held_at_dispatch"])
                self.assertEqual(row["prior_scripted_spend"] + row["turn_grant"], row["slot_limit"])
                self.assertEqual(row["settled_subtree_tokens"],
                                 row["prior_scripted_spend"] + row["final_scripted_usage"])
                self.assertEqual((row["slot_overshoot"], row["settlement_count"]), (200, 1))
                self.assertIsNone(row["provider_maximum_overshoot_tokens"])
                self.assertEqual(row["in_flight_admission_refusal"], "HOLD_ACTIVE")
                self.assertEqual(row["after_settlement_admission_refusal"], "BUDGET_EXCEEDED")
        self.assertEqual(report["illustrative_batch_if_three_other_slots_spend_their_cap"], 4_000_200)


if __name__ == "__main__":
    unittest.main()
