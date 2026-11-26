"""A5. The PI can read the adopted conclusion after closure and change an open research question."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import GOAL, PRODUCT, draft
from acceptance.v622.test_close import CloseStudy, close_decision
from gx_core.p0 import conversation_science, research_report
from gx_core.persistence import store


def replace_question():
    return {"kind": "change", "items": [{"item_id": "objective", "item_type": "objective",
                                          "expected_version": 1, "value": "How should we replicate this result?"}]}


def confirm(preview):
    action = {"kind": "confirm", "change_id": preview["change_id"], "preview_sha256": preview["preview_sha256"]}
    if preview.get("required_confirmation"):
        action["acknowledge_consequences"] = preview["required_confirmation"]["acknowledge_consequences"]
    return action


class SealedIntent(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "study"

    def cli(self, verb, *args, error=None):
        ret = subprocess.run([sys.executable, "-B", str(PRODUCT/"bin/gx-p0"), verb,
                              "--state", str(self.work/"state"), *args],
                             env={**os.environ, "GX_CODEX_OFFLINE": "1"},
                             text=True, capture_output=True, timeout=30)
        if error:
            self.assertNotEqual(ret.returncode, 0)
            self.assertIn(error, ret.stderr)
            return
        self.assertEqual(ret.returncode, 0, ret.stderr)
        return json.loads(ret.stdout)

    def send(self, study, mid, action, **kwargs):
        path = self.work/(mid + ".json")
        path.write_text(json.dumps(action))
        result = self.cli("message", "--run-id", study.run_id, "--message-id", mid,
                          "--action", str(path), **kwargs)
        return result and result["result"]

    def warm(self):
        outcome = self.cli("research", "--run-key", "v62a-local-admission", "--goal", GOAL,
                           "--goal-config", str(self.work/"grant.json"))
        self.assertEqual(outcome["invocation"]["added"], {"jobs": 0, "host_turns": 0})
        return Path(outcome["report"]).read_bytes(), json.loads(Path(outcome["result"]).read_bytes())

    def test_open_preview_cannot_cross_closing_or_closed_and_sealed_report_still_exports(self):
        with CloseStudy(self.work) as s:
            action = confirm(self.send(s, "open-preview", replace_question()))
            adopted = s.call(s.lead, "gx_decide_revision", {"decision": close_decision()}, "close")
            self.send(s, "closing-confirm", action, error="CLOSING")
            s.advance()
            self.send(s, "closed-confirm", action, error="CLOSED")
            self.send(s, "late-change", replace_question(), error="CLOSED")
            self.assertEqual(s.rows("SELECT status,goal_version,stop_decision_ref FROM v6_goals"),
                             [{"status":"CLOSED", "goal_version":1, "stop_decision_ref":adopted["decision_ref"]}])
            self.assertEqual(s.rows("SELECT version FROM p0_intent_versions WHERE item_id='objective'"), [{"version":1}])
        _, result = self.warm()
        self.assertEqual((result["goal"], result["goal_version"]), (GOAL, 1))
        self.assertEqual(result["final_decision"]["decision_ref"], adopted["decision_ref"])

    def test_legacy_misaligned_header_recovers_original_science_and_routes_without_writes(self):
        with CloseStudy(self.work) as s:
            opened = s.open("inventory", round=1, inputs=[s.materials[-1]], caps=[])
            actor = s.bind(opened["task_id"])
            s.call(actor, "gx_read_artifact", {"ref": s.materials[-1], "operation_key": "read"}, "read")
            s.call(s.lead, "gx_decide_revision", {"decision": draft()}, "wait")
            report = s.report(actor, s.materials[-1])
            lead = s.next_actor("run")
            adopted = s.call(lead, "gx_decide_revision",
                             {"decision": close_decision([report["report_ref"]])}, "close")
            s.advance()
        original_page, original = self.warm()
        self.assertTrue(original["routes"]["modules"])
        with CloseStudy(self.work, reopen=True) as s:
            # A post-study note is useful for follow-up, but the closing Lead never saw it.
            note = self.send(s, "later-source", {"kind":"supplement", "text":"Follow-up should test a different sample."})
            self.assertEqual(note["status"], "supplement_registered")
            # Reproduce just the persisted header from the old CLOSED edit, leaving the original close untouched.
            with store.transaction(s.conn):
                s.conn.execute("UPDATE v6_goals SET goal_version=2,goal='Later wording' WHERE run_id=?", (s.run_id,))
        page, recovered = self.warm()
        self.assertEqual(page, original_page)
        self.assertEqual(recovered, original)
        self.assertEqual(recovered["closure"]["decision_ref"], adopted["decision_ref"])
        with CloseStudy(self.work, reopen=True) as s:
            self.assertEqual(s.rows("SELECT goal,goal_version FROM v6_goals"), [{"goal":"Later wording", "goal_version":2}])
            view = conversation_science.project(s.conn, s.run_id)
            self.assertEqual((view["goal"], view["goal_version"], view["stop_decision_ref"]),
                             (GOAL, 1, adopted["decision_ref"]))
            self.assertEqual(conversation_science.latest_receipt(s.conn, s.run_id)["decision_ref"], adopted["decision_ref"])
            # A dangling operation still has no authorized conclusion to export.
            with store.transaction(s.conn):
                s.conn.execute("UPDATE v6_goals SET stop_op_digest='missing' WHERE run_id=?", (s.run_id,))
            with self.assertRaisesRegex(ValueError, "Missing enacted close"):
                research_report.completed_close(s.conn, s.run_id)

    def test_open_preview_of_rule_is_bound_to_original_objective_as_well_as_rule_version(self):
        with CloseStudy(self.work) as s:
            item = {"item_id":"method-constraint", "item_type":"method_ban", "expected_version":0,
                    "value":{"forbidden_capabilities":["unavailable-method"]}}
            self.send(s, "limit-method", {"kind":"change", "items":[item]})
            rule = self.send(s, "preview-rule", {"kind":"change", "items":[{**item, "value":None, "expected_version":1}]})
            replacement = self.send(s, "preview-objective", replace_question())
            self.send(s, "approve-question", confirm(replacement))
            self.send(s, "approve-old-rule", confirm(rule), error="objective changed")
            self.assertEqual(s.rows("SELECT version FROM p0_intent_versions WHERE item_id='method-constraint'"),
                             [{"version":1}])
