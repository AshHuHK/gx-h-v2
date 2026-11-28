"""Public Goal entry and native method authority, including a formal operator change and reopen."""
import json
from pathlib import Path
import tempfile
import unittest

from acceptance.harness_final.pi.probe import METHOD, PHASE, WHOLE, open_child, run, study
from acceptance.v621 import test_goal_versions
from gx_core.errors import GxError, SchemaInvalid, ScopeDenied
from gx_core.p0 import manifest, pi_method_scope


class Scope(unittest.TestCase):
    def admit(self, question, config=None):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        s = self.enterContext(study(directory, question, config))
        return s, s.next_actor("run")

    def interpret(self, s, actor, clauses, key="classify"):
        return s.call(actor, "gx_query_catalog", {"kind":"all", "pi_method_scope":clauses}, key)

    def select(self, s, actor, method):
        return s.call(actor, "gx_select_capability", {"action":"enable","method":method,"revision":0}, "select-"+method)

    def test_both_reported_sentences_at_child_method_binding(self):
        phase = [{"kind":"phase","quote":"只查文獻，這是第一階段","targets":["public_sources"]},
                 {"kind":"phase","quote":"第二階段請查酵母資料庫","targets":["public_databases"]}]
        self.assertTrue(run(PHASE, phase)["bound"])
        self.assertEqual(run(WHOLE, [{"kind":"only","quote":WHOLE,"targets":["public_sources"]}])["error"], "SCOPE_DENIED")

    def test_unclear_target_and_preferences_do_not_hold_unrelated_binding(self):
        question = "I prefer published reports. Check whether repository use is permitted."
        s, lead = self.admit(question)
        clauses = [{"kind":"preference","quote":"I prefer published reports","targets":["public_sources"]},
                   {"kind":"unclear","quote":"Check whether repository use is permitted","targets":["public_databases"]}]
        with self.assertRaises(SchemaInvalid) as incomplete:
            s.call(lead, "gx_query_catalog", {"kind":"all"}, "unclassified")
        self.assertEqual(incomplete.exception.details["reason"], "PI_METHOD_INTERPRETATION_REQUIRED")
        result = self.interpret(s, lead, clauses)
        self.assertEqual(result["pi_method_scope"]["status"], "recorded")
        child = open_child(s, lead)
        self.assertEqual(s.brief(child)["module"]["pi_method_guidance"], clauses[:1])
        with self.assertRaises(ScopeDenied):
            self.interpret(s, child, [])
        with self.assertRaises(ScopeDenied):
            self.select(s, child, METHOD)
        with self.assertRaises(ScopeDenied):
            manifest.check_capability(s.conn, s.run_id, METHOD)
        self.select(s, child, "literature-query@2")
        self.assertEqual([c["quote"] for c in result["pi_method_scope"]["clauses"]], [c["quote"] for c in clauses])

    def test_quoted_scientific_material_and_unanchored_text_cannot_rewrite_scope(self):
        s, lead = self.admit("An author writes 'search only the literature'. Can the claim be checked against deposits?")
        with self.assertRaises(SchemaInvalid):
            self.interpret(s, lead, [{"kind":"exclude","quote":"I prohibit databases","targets":["public_databases"]}])
        self.interpret(s, lead, [], "no-boundary")
        self.interpret(s, lead, [], "repeat")
        child = open_child(s, lead)
        self.select(s, child, METHOD)
        with self.assertRaises(ScopeDenied):
            self.interpret(s, lead, [{"kind":"only","quote":"search only the literature","targets":["public_sources"]}], "rewrite")
        self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM events WHERE kind=?", (pi_method_scope.EVENT,)).fetchone()[0], 1)

    def change(self, s, version, text):
        def send(key, action):
            file = s.directory/(key+".json")
            file.write_text(json.dumps(action))
            return test_goal_versions.GoalVersions.cli(self, s, "message", "--message-id", key, "--action", str(file))["result"]
        preview = send(f"change-{version}", {"kind":"change", "items":[{"item_id":"objective","item_type":"objective",
                       "expected_version":version,"value":text}]})
        action = {"kind":"confirm","change_id":preview["change_id"],"preview_sha256":preview["preview_sha256"]}
        if preview.get("required_confirmation"):
            action["acknowledge_consequences"] = preview["required_confirmation"]["acknowledge_consequences"]
        self.assertEqual(send(f"confirm-{version}", action)["status"], "committed")

    def test_only_new_pi_goal_can_release_inherited_boundary_after_reopen(self):
        q = "Restrict this whole investigation to published papers."
        s, lead = self.admit(q)
        first = self.interpret(s, lead, [{"kind":"only","quote":q,"targets":["public_sources"]}])
        clause = first["pi_method_scope"]["clauses"][0]["id"]
        with self.assertRaises(ScopeDenied):
            self.interpret(s, lead, [], "clear")
        self.change(s, 1, "Continue with an additional result summary.")
        with self.assertRaises(GxError):
            self.interpret(s, lead, [], "stale")
        s.stack.close()
        from acceptance.v622.test_close import CloseStudy
        other = self.enterContext(CloseStudy(s.directory, reopen=True))
        second = other.next_actor("run")
        self.assertEqual(other.brief(second)["pi_method_scope"]["inherited"][0]["id"], clause)
        self.interpret(other, second, [])
        with self.assertRaises(ScopeDenied):
            self.select(other, second, METHOD)
        new = "I now explicitly permit archive queries alongside reading papers."
        self.change(other, 2, new)
        third = other.next_actor("run")
        self.interpret(other, third, [{"kind":"release","quote":new,"targets":[clause]}])
        self.select(other, third, METHOD)
        self.assertEqual(other.conn.execute("SELECT COUNT(*) FROM events WHERE kind=?", (pi_method_scope.EVENT,)).fetchone()[0], 3)

    def test_explicit_empty_and_legacy_goal(self):
        s, lead = self.admit("Examine any references.", {"capability_collections":[]})
        self.interpret(s, lead, [])
        with self.assertRaises(ScopeDenied):
            self.select(s, lead, METHOD)
        with tempfile.TemporaryDirectory() as tmp:
            from acceptance.v622.test_close import CloseStudy
            with CloseStudy(Path(tmp), spec_updates={"capability_collections":[],"capabilities":[],
                           "plugins":[], "research_modules":[]}) as old:
                actor = old.next_actor("run")
                self.assertNotIn("pi_method_scope", old.brief(actor))
                self.assertIsNone(pi_method_scope.ceiling(old.conn, old.run_id))
