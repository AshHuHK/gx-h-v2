"""Pin historical host declarations and preserve exact native retry receipts on reopen."""
import json
from pathlib import Path
import tempfile
import unittest

from acceptance.v622.test_close import CloseStudy, close_decision
from acceptance.v623.test_policy_compat import wire_digest
from gx_core import contracts
from gx_core.errors import SchemaInvalid, ScopeDenied
from gx_core.p0 import v621_policy, v62_native, v6_native
from gx_core.persistence import store


class NativeDecisionWire(unittest.TestCase):
    def test_f9231023_historical_wire_stays_pinned(self):
        # Captured from the unmodified V6.2.3 release, including the quick projection.
        expected = {
            "gx_decide_revision": "b868f19fbdaca69a2b1722ab45ba74f8498ccec825d31d42d8200f5850143541",
            "gx_stage_claim": "7aa0eacff015d4dad31e1753a31852c6b638fc45f74b78fbdcb3cdd4e46776dd",
            "gx_submit_module_report": "3f816a50dc313133dc59a1194e39081a46a852ad0459bdd0e0b21c4707229278",
            "quick_decision": "b510366223254ebf66d08b2e2033293d2d14099ed3bc91fe697a85c03c94befd",
        }
        for tool, digest in expected.items():
            with self.subTest(tool=tool):
                spec = (v6_native._legacy_goal_tools(v621_policy.V623)[0] if tool == "quick_decision" else
                        v62_native.evidence_tool(tool, v621_policy.V623))
                self.assertEqual(wire_digest(spec), digest)

    def test_refused_redeliveries_and_accepted_retry_survive_restart(self):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "retry"
        bad = {"private-field": "PRIVATE-RESEARCH-CONTENTS"}
        good = {"decision": close_decision()}
        with CloseStudy(path) as study:
            actor = study.lead
            for _ in range(2):
                with self.assertRaises(SchemaInvalid) as error:
                    study.call(actor, v6_native.DECIDE, bad, "retry-one")
                self.assertEqual(error.exception.details["field"], "decision")
            with self.assertRaises(ScopeDenied):
                study.call(actor, v6_native.DECIDE, good, "retry-one")
            self.assertIsNone(study.head())
            accepted = study.call(actor, v6_native.DECIDE, good, "corrected-two")
            self.assertEqual(study.call(actor, v6_native.DECIDE, good, "corrected-two"), accepted)
        with CloseStudy(path, reopen=True) as restored:
            with self.assertRaises(SchemaInvalid) as error:
                restored.call(actor, v6_native.DECIDE, bad, "retry-one")
            self.assertEqual(error.exception.details["reason"], "DECISION_OBJECT_REQUIRED")
            self.assertEqual(restored.call(actor, v6_native.DECIDE, good, "corrected-two"), accepted)
            rows = restored.rows("SELECT kind,payload_json FROM events WHERE kind LIKE 'v624.decision.%' ORDER BY event_id")
            self.assertEqual([r["kind"] for r in rows], ["v624.decision.rejected", "v624.decision.corrected"])
            self.assertNotIn("PRIVATE-RESEARCH-CONTENTS", json.dumps(rows))
            self.assertEqual(json.loads(rows[-1]["payload_json"])["decision_event"], accepted["event_id"])
            self.assertEqual(len(restored.rows("SELECT * FROM v6_decisions")), 1)
            self.assertEqual(len(restored.rows("SELECT * FROM p0_v6_native_calls")), 2)

    def test_older_receipt_and_native_request_bytes_survive_contract_resync(self):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "old"
        history = {"historical_replay": True, "evidence_policy": v621_policy.V623}
        old_sha = "cdb1d8e5704193422e9ec1f341b064d02ee71d440aea33dc5f840e3c0700f56f"
        with CloseStudy(path, spec_updates=history) as study:
            actor = study.lead
            args = {"decision": close_decision()}
            receipt = study.call(actor, v6_native.DECIDE, args, "original")
            before = tuple(study.conn.execute("SELECT request_json,receipt_json FROM v6_decisions").fetchone())
            study.conn.execute("UPDATE schema_meta SET value=? WHERE key='contract_sha256'", (old_sha,))
            self.assertEqual(study.rows("SELECT kind FROM events WHERE kind LIKE 'v624.decision.%'"), [])
        with CloseStudy(path, reopen=True) as restored:
            self.assertEqual(restored.call(actor, v6_native.DECIDE, args, "original"), receipt)
            after = tuple(restored.conn.execute("SELECT request_json,receipt_json FROM v6_decisions").fetchone())
            self.assertEqual(after, before)
            self.assertEqual(v621_policy.pinned(restored.conn, restored.run_id), v621_policy.V623)
            self.assertEqual(store.read_meta(restored.conn)["contract_sha256"], contracts.contract_digest()["__combined__"])
            resyncs = restored.rows("""SELECT payload_json FROM events WHERE kind='store.contract_resynced'
                    AND json_extract(payload_json,'$.from_sha256')=?""", old_sha)
            self.assertEqual(len(resyncs), 1)
