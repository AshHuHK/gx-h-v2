"""Decision citations for freshly returned source records in the owning turn."""
from pathlib import Path
import tempfile
import unittest

from gx_core.errors import ScopeDenied, StaleRevision
from gx_core.p0 import v6_decisions
from tests.test_v62_modules_publication import Scenario, draft


class SourceDecisionReceipts(unittest.TestCase):
    def test_owner_can_cite_new_source_record_without_publishing_it_to_other_threads(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        note = work / "source.txt"
        note.write_text("The north gauge rose during the second visit.")
        grant = {"research_effort":"deep","research_modules":["scholarly_investigation@1"],
                 "plugins":["org.gx.scholarly-sources@1.0.0"], "capabilities":[],
                 "evidence_policy":"gx.evidence/6.2.4","input_files":[{"path":str(note),"role":"literature"}]}
        s = self.enterContext(Scenario(work/"study",spec_updates=grant))
        source = s.materials[0]
        actors = []
        for label in ("a","b"):
            opening = dict(s.open_args(label, inputs=[source], caps=[]), module_id="scholarly_investigation",
                           focus="literature", expected_delivery="Assess the record and its interpretation.")
            actors.append(s.next_actor(s.call(s.lead,"gx_open_module",opening,label)["scope"]))
        a, b = actors
        segment = {"source_ref":source, "segment_id":"p1"}
        read = s.call(a,"gx_read_source",segment,"read")
        extract = s.call(a,"gx_extract_evidence",dict(segment, receipt_id=read["receipt_id"],
            quote=read["text"], claim=read["text"], direction="supports", applicability="Second visit only.",
            confidence_basis="The original names the visit and the gauge."),"extract")
        ref = extract["evidence_ref"]
        decision = {"decision":draft(evidence=[ref],watches=[{"event":"goal_changed","subject_ref":"run"}])}
        for actor in (b, s.lead):
            with self.assertRaises(ScopeDenied):
                s.call(actor,"gx_decide_revision",decision,"foreign-"+actor.task_id)
        with self.assertRaises(ScopeDenied):
            s.call(a, "gx_decide_revision", {"decision":draft(evidence=[read["receipt_id"]])},"raw-read-id")
        task = s.conn.execute("SELECT * FROM v6_decision_tasks WHERE task_id=?", (a.task_id,)).fetchone()
        for lease, thread, turn in ((a.lease_id,b.thread,a.turn),(b.lease_id,a.thread,a.turn),
                                    (a.lease_id,a.thread,"different-turn")):
            with self.assertRaises(ScopeDenied):
                v6_decisions._visible(s.conn,task,lease,thread,turn,decision["decision"])
        # Even where a call legitimately cites the paper extraction, that artifact
        # cannot serve as a claim revision, graph input or cross-scope grant.
        overlap = dict(decision["decision"], ops=[{"op":"close_run", "claim_refs":[ref]}])
        with self.assertRaises(ScopeDenied) as denied:
            v6_decisions._visible(s.conn, task, a.lease_id, a.thread, a.turn, overlap)
        self.assertEqual(denied.exception.details["field"], "ops[0].claim_refs[0]")
        materials = v6_decisions._visible(s.conn, task, a.lease_id, a.thread, a.turn, decision["decision"])
        self.assertNotIn(ref, materials)
        receipt = s.call(a,"gx_decide_revision",decision,"mine")
        self.assertTrue(receipt["decision_ref"])


class SourceReviewClose(unittest.TestCase):
    def setUp(self):
        from gx_core.p0 import v6_goal
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        note = work / "source.txt"
        note.write_text("The north gauge rose during the second visit.")
        s = self.enterContext(Scenario(work / "study", spec_updates={
            "research_effort": "deep", "research_modules": ["scholarly_investigation@1"],
            "plugins": ["org.gx.scholarly-sources@1.0.0"], "capabilities": [],
            "evidence_policy": "gx.evidence/6.2.4",
            "input_files": [{"path": str(note), "role": "literature"}]}))
        segment = {"source_ref": s.materials[0], "segment_id": "p1"}
        first = s.call(s.lead, "gx_read_source", segment, "read")
        extraction = s.call(s.lead, "gx_extract_evidence", dict(
            segment, receipt_id=first["receipt_id"], claim=first["text"], quote=first["text"],
            direction="supports", applicability="The north gauge only.",
            confidence_basis="The note names this gauge."), "extract")
        s.call(s.lead, "gx_decide_revision", {"decision": draft(evidence=[extraction["evidence_ref"]])}, "publish")
        v6_goal.record_observation(s.conn, s.run_id, segment["source_ref"], source="review-request")
        lead = s.next_actor("run")
        read = s.call(lead, "gx_read_source", segment, "reread")
        review = s.call(lead, "gx_review_evidence", {
            "evidence_ref": extraction["evidence_ref"], "receipt_id": read["receipt_id"],
            "quote_sha256_seen": extraction["quote_sha256"], "verdict": "SUPPORTED",
            "note": "The current record explicitly names the north gauge."}, "review")
        self.assertTrue(review["accepted"])
        self.s, self.review_ref, self.segment = s, review["review_ref"], segment
        self.review_event = s.conn.execute("SELECT event_id FROM events WHERE subject_id=? AND kind='v62.source.reviewed'",
                                           (self.review_ref,)).fetchone()[0]

    def close(self, refs):
        from acceptance.v622.test_close import close_decision
        return self.s.call(self.s.lead, "gx_decide_revision", {"decision": close_decision(refs)}, "close")

    def test_lead_closes_with_exact_review_just_delivered(self):
        result = self.close([self.review_ref])
        self.assertTrue(result["decision_ref"])
        self.assertEqual(len(self.s.rows("SELECT 1 FROM events WHERE kind='v6.run.close_requested'")), 1)

    def test_uncited_review_still_requires_fresh_brief(self):
        with self.assertRaises(StaleRevision) as stale:
            self.close([])
        self.assertEqual(stale.exception.details["event_id"], self.review_event)
        self.assertEqual(stale.exception.details["uncited_review_refs"], [self.review_ref])

    def test_new_observation_requires_refresh_even_with_cited_review(self):
        from gx_core.p0 import v6_goal
        s = self.s
        v6_goal.record_observation(s.conn, s.run_id, self.segment["source_ref"], source="new-reading")
        with self.assertRaises(StaleRevision) as stale:
            self.close([self.review_ref])
        self.assertEqual(stale.exception.details["reason"], "close_context_changed")
        self.assertNotIn("uncited_review_refs", stale.exception.details)
        self.assertEqual(len(s.rows("SELECT 1 FROM events WHERE kind='v6.decision.superseded'")), 1)

    def test_only_current_lead_and_unmixed_untargeted_reviews_qualify(self):
        from gx_core.p0 import research_dialogue, v62_rounds
        from gx_core.persistence import store
        s = self.s
        native = dict(run_id=s.run_id, task_id=s.lead.task_id, lease_id=s.lead.lease_id,
                      thread_id=s.lead.thread, turn_id=s.lead.turn)
        root, task, _, _ = v6_decisions.authority(s.conn, **native)
        def covered(*, actor=root, turn=s.lead.turn, **fields):
            return v6_decisions._own_close_reviews(s.conn, task, actor, turn, fields)
        self.assertEqual(covered(), {self.review_ref})
        self.assertFalse(covered(actor={**root, "thread_id": "another-scientist"}))
        self.assertFalse(covered(turn="later-turn-on-same-thread"))
        self.assertFalse(covered(convergence={"spec":"convergence"}))
        self.assertFalse(covered(resolved_gaps=[{"gap_ref":"pending"}]))
        # Regular convergence and non-closing decisions always see the review as late.
        self.assertEqual(v62_rounds.late_evidence(s.conn, s.run_id, task["event_cursor"]), self.review_event)
        original = s.conn.execute("SELECT payload_json FROM events WHERE event_id=?", (self.review_event,)).fetchone()
        review = store.loads(original, "payload_json")
        incoming = [
            ("v62.source.reviewed", {**review, "target_claim_ref": "hypothesis@1"}),
            ("v62.source.reviewed", {**review, "review_turn_id": "original-turn-before-redelivery"}),
            ("v62.source.reviewed", {k:v for k,v in review.items() if k != "review_turn_id"}),
            ("v62.source.reviewed", {**review, "artifact_ref":"different-review"}),
            ("v62.claim.reassessed", {"scope": "run"}),
            ("v62.source.extracted", {"scope": "run"}),
            (research_dialogue.FEEDBACK, {"scope": "run"}),
            ("job.created", {}),
        ]
        for kind, payload in incoming:
            with self.subTest(kind=kind, payload=payload), store.transaction(
                    s.conn, transaction_owner="SourceCloseTest", operation="interleaving", run_id=s.run_id):
                s.conn.execute("SAVEPOINT incoming")
                store.append_event(s.conn, run_id=s.run_id, kind=kind, actor="other-worker",
                    subject_kind="job" if kind=="job.created" else "artifact",
                    subject_id=payload.get("artifact_ref", self.review_ref), payload=payload)
                try:
                    self.assertFalse(covered())
                finally:
                    s.conn.execute("ROLLBACK TO incoming")
                    s.conn.execute("RELEASE incoming")
