"""Source integrity at the raw native-to-acceptance boundary."""
import json
from pathlib import Path
import tempfile
import unittest

import native_witness
import project_window as project
import verify as v


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.arch = project.Archive(self.root)

    def test_unread_source_and_wrong_artifact_never_establish_delivery(self):
        val = {"angle":1.25}
        brief = "GX research brief\n" + json.dumps({"observations":[{"artifact_ref":"a", "observation":val},
                                                             {"artifact_ref":"b","observation":{"angle":0.75}}]})
        native = {"reads":{},"brief":brief}
        self.assertTrue(native_witness.seen(native,"a",val))
        self.assertFalse(native_witness.seen(native,"b",val))
        self.assertFalse(native_witness.seen(native,"a",{"angle":0.75}))

    def test_tampered_cost_sidecar_fails_even_when_unscored_by_kit(self):
        sidecar = self.arch.put({"reserved":{"tokens":800},"confirmed":None})
        bundle = {"runs":[self.arch.put({"resource_ledger":sidecar})]}
        v.verify_objects(self.root,bundle)
        (self.root/sidecar["file"]).write_bytes(v.stable({"reserved":{"tokens":0},"confirmed":0}))
        with self.assertRaisesRegex(ValueError,"source digest"):
            v.verify_objects(self.root,bundle)

    def test_fresh_claim_requires_actual_order_scope_receipt_and_bytes(self):
        put = self.arch.put
        claim = {"evidence_refs":["e"],"level":"limited_support"}
        ref = "claim-8"
        call = {"threadId":"thread","turnId":"turn","callId":"call","tool":"gx_stage_claim","arguments":claim}
        answer = {"claim_ref":ref,"sha256":v.digest(claim)}
        receipt = {"thread_id":"thread","turn_id":"turn","call_id":"call",
                   "request_sha":v.digest(["gx_stage_claim",claim]),"result_json":json.dumps(answer)}
        frame = {"type":"function_call_output","call_id":"call",
                 "output":[{"type":"input_text","text":json.dumps(answer)}]}
        product = {"ref":ref,"call":put(call),"receipt":put(receipt),"output":put(frame),
                   "line":8,"bytes":put(claim)}
        turn = {"turn_id":"turn","thread_id":"thread","decision_line":10,"same_turn_products":[product]}
        self.assertEqual(v.same_turn_products(self.root,turn),{ref})
        for edit in ({"line":11},{"bytes":put({})},{"receipt":put({**receipt,"turn_id":"other"})},
                     {"output":put({**frame,"output":[]})}):
            wrong = {**turn,"same_turn_products":[{**product,**edit}]}
            with self.assertRaises(ValueError):
                v.same_turn_products(self.root,wrong)

    def test_numerical_pointer_comes_from_raw_request(self):
        p = project.pairs({"parameters":{"target":"A/C"},"not_a_target":"geometry"})
        self.assertEqual((p["chain_pairs"],p["pair_pointers"]),(["A/C"],["/parameters/target"]))
