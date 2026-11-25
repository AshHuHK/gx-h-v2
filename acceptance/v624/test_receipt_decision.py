"""An abbreviated native action and its adopted canonical form keep independent evidence."""
import json
import unittest

from acceptance.v62b.study import receipts


class DecisionReceipt(unittest.TestCase):
    raw = {"rationale": "Use the delivered reading.", "metric_refs": ["M1"], "ops": []}
    canonical = {**raw, "metric_refs": ["metric_long_identity"], "evidence_refs": ["report_1"]}

    def verify(self, request, args, receipt=None):
        row = {"request_json": json.dumps(request), "receipt_json": json.dumps(receipt or {})}
        return receipts.verified_native_decision(row, {"decision": args})

    def test_raw_and_normalized_are_bound_to_host_and_receipt(self):
        hashes = {"draft_sha256": receipts.digest(self.raw), "normalized_sha256": receipts.digest(self.canonical)}
        request = {"original_decision": self.raw, "decision": self.canonical, **hashes}
        self.assertEqual(self.verify(request, json.dumps(self.raw), hashes), self.raw)
        for changed in ({"original_decision": self.canonical}, {"decision": self.raw},
                        {"draft_sha256": receipts.digest(self.canonical)}, {"normalized_sha256": None}):
            with self.subTest(fields=list(changed)), self.assertRaises(ValueError):
                self.verify(request | changed, self.raw, hashes)
        tampered = request | {"decision": self.raw, "normalized_sha256": hashes["draft_sha256"]}
        with self.assertRaisesRegex(ValueError, "adoption receipt"):
            self.verify(tampered, self.raw, hashes)
        with self.assertRaises(ValueError):
            self.verify(request, self.canonical, hashes)

    def test_legacy_uses_single_hash_and_rejects_partial_upgrade(self):
        legacy = {"decision": self.raw, "draft_sha256": receipts.digest(self.raw)}
        self.assertEqual(self.verify(legacy, self.raw), self.raw)
        with self.assertRaises(ValueError):
            self.verify(legacy, self.canonical)
        with self.assertRaises(ValueError):
            self.verify(legacy, self.raw, {**legacy, "normalized_sha256": legacy["draft_sha256"]})
        for partial in ({"original_decision": self.raw}, {"normalized_sha256": legacy["draft_sha256"]}):
            with self.assertRaises(ValueError):
                self.verify(legacy | partial, self.raw)
