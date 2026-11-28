"""Reproduce observed closing-field shapes on an ordinary Goal with a neutral instrument record."""
import json
from pathlib import Path
import tempfile
import unittest

from acceptance.harness_refactor.closing.test_closeout import Study
from gx_core.errors import SchemaInvalid, ScopeDenied


class FieldFormat(unittest.TestCase):
    def study(self, *, historical=False):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        cfg = {"historical_replay": True, "evidence_policy": "gx.evidence/6.2.3"} if historical else {}
        return self.enterContext(Study(folder, spec_updates=cfg))

    def call(self, s, op, key, **fields):
        decision = {"ops": [op] if op is not None else [], "rationale": "The instrument record is complete.",
                    **fields}
        return s.call(s.lead, "gx_decide_revision", {"decision": decision}, key)

    def test_sole_close_reuses_own_rationale_and_preserves_explicit_unknowns(self):
        s = self.study()
        op = {"op": "close_run", "claim_refs": [], "unresolved": ["Calibration at high temperature."]}
        result = self.call(s, op, "close", mode="resolve", stop_reason="SCIENTIFIC_COMPLETION")
        saved, = s.rows("SELECT request_json FROM v6_decisions")
        decision = json.loads(saved["request_json"])["decision"]
        self.assertEqual(decision["ops"][0], dict(op, reason=decision["rationale"]))
        self.assertEqual(decision["mode"], "amend")
        self.assertEqual(self.call(s, op, "close", mode="resolve", stop_reason="SCIENTIFIC_COMPLETION"), result)

    def test_invalid_pi_outcome_and_missing_unresolved_are_both_actionable(self):
        s = self.study()
        op = {"op":"close_run", "claim_refs":[]}
        items = [
            {"feedback_id": "feedback_x", "disposition": "confirm", "rationale": "The reading is unchanged."},
            {"feedback_id": "feedback_x", "outcome": "reject", "reason": "The reading is unchanged."},
            {"feedback_id": "feedback_x", "outcome": "incorporated", "reason": "The reading is unchanged."},
        ]
        for i, item in enumerate(items):
            with self.subTest(item=i), self.assertRaises(SchemaInvalid) as refused:
                self.call(s, op, f"invalid-{i}", disposition="STOP", affected_scope="run", pi_dispositions=[item])
            message = refused.exception.message
            for key in ("ops[0].unresolved", "pi_dispositions[0].outcome", "adopt", "maintain", "measure", "defer",
                        "omit top-level disposition and affected_scope"):
                self.assertIn(key, message)
        self.assertIsNone(s.head())
        self.assertEqual(s.rows("SELECT 1 FROM events WHERE kind='research.pi.disposition'"), [])

    def test_ordinary_wait_or_close_explains_precise_comparison_metadata(self):
        s = self.study()
        for op, label, mode, extra in ((None, "MAINTAIN", "resolve", {}), (
                {"op":"close_run", "reason":"Archive read.", "unresolved":[], "claim_refs":[]}, "STOP", "resolve",
                {"stop_reason":"SCIENTIFIC_COMPLETION"})):
            with self.subTest(label=label), self.assertRaises(SchemaInvalid) as refusal:
                self.call(s, op, label, mode=mode, disposition=label, affected_scope="run", **extra)
            self.assertEqual(refusal.exception.details["reason"], "comparison_action_ambiguous")
            for field in ("disposition", "affected_scope"):
                self.assertIn(field, refusal.exception.message)
            self.assertIsNone(s.head())

    def test_unresolved_is_scientific_and_existing_gap_ids_remain_scoped(self):
        s = self.study()
        op = {"op":"close_run", "claim_refs":[]}
        with self.assertRaises(SchemaInvalid) as gaps:
            self.call(s, op, "missing")
        self.assertEqual(gaps.exception.details["field"], "ops[0].unresolved")
        with self.assertRaises(ScopeDenied) as outside:
            self.call(s, dict(op, unresolved=[]), "scope", resolved_gaps=["invented"])
        self.assertIn("resolved_gaps", outside.exception.message)
        self.assertIsNone(s.head())

    def test_mode_conflict_names_both_legal_shapes(self):
        s = self.study()
        with self.assertRaises(SchemaInvalid) as wrong:
            self.call(s, None, "wait", mode="amend")
        self.assertEqual(wrong.exception.details["field"], "mode")
        self.assertIn("mode=resolve", wrong.exception.message)
        self.assertIn("mode=amend", wrong.exception.message)
        self.assertIsNone(s.head())

    def test_single_new_intent_can_be_unnamed_but_an_existing_reference_is_not_recast(self):
        s = self.study()
        with self.assertRaises(SchemaInvalid) as bad:
            self.call(s, {"op":"open_intent","intent":"intent_existing","question":"Which records are paired?"}, "invalid")
        self.assertIn("ops[0].intent", bad.exception.message)
        self.assertIn("local:", bad.exception.message)
        self.assertIsNone(s.head())
        receipt = self.call(s, {"op":"open_intent","question":"Which records are paired?"}, "single")
        self.assertIn("local:intent", receipt["aliases"])

    def test_historical_closure_still_requires_its_explicit_reason(self):
        s = self.study(historical=True)
        with self.assertRaises(SchemaInvalid) as kept:
            self.call(s, {"op":"close_run", "claim_refs":[], "unresolved":[]}, "old", mode="amend",
                      evidence_refs=[], reconsider_when=[{"event":"evidence_added", "subject_ref":"run"}])
        self.assertEqual(kept.exception.details["field"], "ops[0].reason")
