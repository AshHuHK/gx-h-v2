"""Private PI evidence, confirmed reading, and the wider audience of a released report."""
from pathlib import Path
import tempfile
import unittest

from acceptance.harness_refactor.late_material.test_directed_delivery import researcher, route
from acceptance.harness_refactor.modules.research_study import ResearchStudy
from acceptance.v62a.interleavings_fixture import draft
from gx_core.errors import ScopeDenied
from gx_core.p0 import v62_modules


class PublicationScope(unittest.TestCase):
    def test_private_evidence_rejection_explains_audience_and_offers_a_real_pi_question(self):
        with tempfile.TemporaryDirectory() as directory, ResearchStudy(Path(directory), spec_updates={
            "research_effort":"medium", "research_modules":["ad_hoc@1"], "capabilities":[],
        }) as s:
            assigned, _ = researcher(s, "receiver")
            _, independent = researcher(s, "independent")
            first = s.materials[0]
            later, _ = route(s, assigned["scope"], "signed-later", {"name":"late-note.txt",
                            "text":"The second dated observation was recorded at the west gauge."})
            v62_modules.advance(s.conn, s.run_id)
            reader = s.next_actor(assigned["scope"])
            s.call(reader, "gx_read_artifact", {"ref":first}, "read-first")
            s.call(reader, "gx_read_artifact", {"ref":later}, "read-later")
            # The official researcher submitted both raw refs, then retried with only finding-level refs.
            for key, listed in (("cited-originals", [first,later]), ("cited-findings", [])):
                with self.assertRaises(ScopeDenied) as denied:
                    s.report(reader, later, key=key, artifact_refs=listed, findings=[
                        {"text":"The first archive has a dated record.", "source_refs":[first]},
                        {"text":"The second archive has a dated record.", "source_refs":[later]}])
                self.assertEqual(denied.exception.details.get("reason"), "branch_private_report_source")
                self.assertEqual(denied.exception.details["artifact_ref"], later)
                self.assertIn("PI", str(denied.exception))
            located = s.call(reader, "gx_read_source", {"source_ref":later, "segment_id":"p1"}, "locate")
            extract = s.call(reader, "gx_extract_evidence", {
                "source_ref":later, "segment_id":"p1", "receipt_id":located["receipt_id"],
                "quote":located["text"], "claim":"The later record identifies a gauge.",
                "direction":"supports", "applicability":"This record only.",
                "confidence_basis":"This sentence names the gauge in the dated record."}, "extract")
            with self.assertRaises(ScopeDenied) as derived:
                s.report(reader, extract["artifact_ref"], "restricted-extract")
            self.assertEqual(derived.exception.details, {"reason":"branch_private_report_source",
                                                         "artifact_ref":extract["artifact_ref"]})
            # Do not expose whether a guessed reference exists, its audience, or another reader's receipts.
            with self.assertRaises(ScopeDenied) as guessed:
                s.report(independent, later, "not-my-evidence")
            self.assertNotIn("reason", guessed.exception.details)
            self.assertNotIn(later, str(guessed.exception))
            # A shareable public-only finding remains publishable by its actual reader.
            s.call(independent, "gx_read_artifact", {"ref":first}, "shared-source")
            visible = s.report(independent, first, "first-record", "The first archive contains a record.")
            self.assertEqual(visible["source_refs"], [first])
            question = {"question":"Can you provide an original authorized for the audience of the released report?",
                        "blocking":True, "choices":["Provide a shareable copy","Keep the item private"]}
            decision = dict(draft(), pi_questions=[question])
            asked = s.call(reader, "gx_decide_revision", {"decision":decision}, "request-shareable")
            self.assertEqual(asked["pi_questions"][0]["scope"], assigned["scope"])
            self.assertEqual(asked["pi_questions"][0]["question"], question["question"])
