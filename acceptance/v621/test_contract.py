"""Exercise published report bytes, evidence receipts and a mutable delivery cursor on the same Goal."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import Scenario, draft
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.p0 import v621_policy, v62_maturity


class ContractPublication(unittest.TestCase):
    def test_cold_report_replay_and_cursor_advance_after_rejected_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            study = Path(tmp) / "report"
            with Scenario(study) as s:
                src = s.materials[0]
                self.assertTrue(v621_policy.enabled(s.conn, s.run_id))
                actor = s.bind(s.open("inventory", inputs=[src])["task_id"])
                s.call(actor, "gx_read_artifact", {"ref": src, "operation_key": "source"}, "source")
                s.call(s.lead, "gx_decide_revision", {"decision": draft()}, "yield")
                receipt = s.report(actor, src)
                ref = receipt["report_ref"]
                evidence = s.conn.execute("SELECT event_id FROM events WHERE run_id=? AND "
                                          "kind='v62.module.tool_completed'", (s.run_id,)).fetchone()[0]
                original = ArtifactService(s.conn).describe(ref)["sha256"]
                for sql, param in (
                    ("UPDATE v62_module_reports SET receipt_json='{}' WHERE report_ref=?", ref),
                    ("DELETE FROM v62_module_reports WHERE report_ref=?", ref),
                    ("INSERT OR REPLACE INTO v62_module_reports SELECT * FROM v62_module_reports WHERE report_ref=?", ref),
                    ("UPDATE events SET payload_json='{}' WHERE event_id=?", evidence),
                    ("DELETE FROM events WHERE event_id=?", evidence),
                    ("INSERT OR REPLACE INTO events SELECT * FROM events WHERE event_id=?", evidence),
                ):
                    with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                        s.conn.execute(sql, (param,))
            with Scenario(study, reopen=True) as cold:
                self.assertEqual(receipt, cold.report(actor, src))
                lead = cold.next_actor("run")
                self.assertIn(ref, cold.brief(lead)["evidence_refs"])
                full = ArtifactService(cold.conn).read_json(ref)
                self.assertEqual(original, ids.sha256_of_obj(full))
                v62_maturity.reassess(cold.conn, cold.run_id)
                self.assertGreaterEqual(cold.conn.execute("SELECT event_cursor FROM v62_maturity_cursors "
                                                          "WHERE run_id=?", (cold.run_id,)).fetchone()[0], evidence)
                wakes, = cold.conn.execute("SELECT COUNT(*) FROM v62_module_wakes WHERE run_id=?", (cold.run_id,)).fetchone()
                self.assertEqual(wakes, 1)

    def test_old_and_new_goals_keep_separate_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Scenario(Path(tmp) / "legacy", spec_updates={"evidence_policy": v621_policy.LEGACY}) as old:
                self.assertFalse(v621_policy.enabled(old.conn, old.run_id))
            with Scenario(Path(tmp) / "new") as new:
                self.assertTrue(v621_policy.enabled(new.conn, new.run_id))
                inputs = json.loads(new.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?",
                                                     (new.run_id,)).fetchone()[0])
                self.assertEqual(v621_policy.VERSION, inputs["inputs"]["evidence_policy"])
