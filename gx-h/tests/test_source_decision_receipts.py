"""Decision citations for freshly returned source records in the owning turn."""
from pathlib import Path
import tempfile
import unittest

from gx_core.errors import ScopeDenied
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
