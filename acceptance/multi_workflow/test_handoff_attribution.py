"""Attribute a follow-up Job only to report content received on the matching material."""
import json
from pathlib import Path
import tempfile
import unittest

from acceptance.multi_workflow.investigation_handoff import open_investigation
from acceptance.v621.test_verified_metrics import METHOD, SOURCE_METADATA
from acceptance.v622.test_close import CloseStudy, actual_packet
from acceptance.v62a.interleavings_fixture import branch, draft
from gx_core.domain import ids
from gx_core.p0 import research_routes, v6_goal


class HandoffAttribution(unittest.TestCase):
    def setUp(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory())) / "run"
        config = {"research_modules": ["investigation@1"], "plugins": ["org.gx.investigation@1.0.0",
                  "org.gx.protein@0.3.0"], "capabilities": [METHOD]}
        study = self.enterContext(CloseStudy(work, spec_updates=config))
        self.study = study
        self.target, self.other = study.materials[:2]
        target = self.target
        v6_goal.record_observation(study.conn, study.run_id, target, source="public-archive",
                                   acquisition=json.loads(SOURCE_METADATA.read_bytes()))
        origin = open_investigation(study, "inventory", [target], "Measure ordered-backbone coverage.", 1)
        actor = study.bind(origin["task_id"])
        study.call(study.lead, "gx_decide_revision", {"decision": draft()}, "wait")
        queued = study.submit_node(actor, "ordered", target, capability=METHOD, parameters={"mask": "ordered_backbone"})
        output = study.settle(queued["aliases"]["local:ordered"])["result_ref"]
        self.source_output = output
        reporter = study.next_actor(origin["scope"])
        self.report = study.report(reporter, output, findings=[],
                                   metric_refs=[{"artifact_ref": output, "metric_id": "coverage"}])["report_ref"]
        lead = study.next_actor("run")
        if self.report not in actual_packet(study, lead)["packet"]["evidence_refs"]:
            study.call(lead, "gx_decide_revision", {"decision": draft()}, "wait-for-report")
            study.next_actor("run")
        child = open_investigation(study, "followup", [self.report, target, self.other],
                                   "Check whether the measured coverage depends on the selected atoms.", 2)
        self.receiver = study.bind(child["task_id"])

    def follow_up(self, originals, evidence):
        study = self.study
        ops = [study.node(self.receiver, name, material, capability=METHOD, parameters={"mask": "common_backbone"})
               for name, material in originals]
        receipt = study.call(self.receiver, "gx_decide_revision", {"decision": draft(*ops, evidence=evidence)}, "compare")
        jobs = [study.settle(receipt["aliases"][op["node"]])["job_id"] for op in ops]
        routes = research_routes.routes(study.conn, study.run_id, study.state)
        handoff, = [h for h in routes["handoffs"] if h.get("decision_ref") == receipt["decision_ref"]]
        return handoff, jobs

    def test_unread_report_handle_does_not_explain_successful_jobs(self):
        handoff, _ = self.follow_up([("control", self.target), ("elsewhere", self.other)], [self.report])
        self.assertFalse(handoff["received_content"])
        self.assertEqual((handoff["status"], handoff["effects"], handoff["real_model_handoff"]), ("CITED", [], False))

    def receive_report(self, actor):
        study = self.study
        request = {"ref": self.report, "endpoint_offset": 0}
        page = study.call(actor, "gx_read_artifact", request, "read-coverage")
        study.roots.delivery.observe("item/completed", {"threadId": actor.thread, "turnId": actor.turn, "item": {
            "type": "dynamicToolCall", "tool": "gx_read_artifact", "id": "read-coverage", "arguments": request,
            "status": "completed", "success": True, "contentItems": [{"type": "inputText", "text": ids.canonical_json(page)}]}})

    def test_received_report_links_only_its_matching_job_within_one_decision(self):
        self.receive_report(self.receiver)
        handoff, jobs = self.follow_up([("control", self.target), ("elsewhere", self.other)],
                                       [self.report, self.source_output])
        self.assertTrue(handoff["received_content"])
        self.assertEqual(handoff["status"], "ENACTED")
        self.assertEqual([e["job_id"] for e in handoff["effects"] if e["kind"] == "MEASUREMENT_COMPLETED"], jobs[:1])


    def test_stop_only_attributes_the_branch_on_the_reported_original(self):
        study = self.study
        ops = []
        for name, material in (("match", self.target), ("other", self.other)):
            opening = branch(name)
            opening["budget_limits"] = {"job_submissions": 1, "cpu_seconds": 65}
            job = study.node(study.lead, name + "-job", material, capability=METHOD,
                             parameters={"mask": "common_ca"}, branch_id=opening["branch"])
            ops.extend([opening, job])
        wait = [{"event": "job_settled", "subject_ref": "local:other-job"}]
        accepted = study.call(study.lead, "gx_decide_revision", {"decision": draft(*ops, watches=wait)}, "open")
        refs = accepted["aliases"]
        for name in ("match-job", "other-job"):
            study.settle(refs["local:" + name])
        lead = study.next_actor("run")
        self.receive_report(lead)
        stops = [{"op": "stop_branch", "branch": refs["local:" + name], "reason": "Measurement complete."}
                 for name in ("match", "other")]
        closed = study.call(lead, "gx_decide_revision", {"decision": draft(*stops, evidence=[self.report])}, "stop")
        study.advance()
        handoff, = [h for h in research_routes.routes(study.conn, study.run_id, study.state)["handoffs"]
                    if h.get("decision_ref") == closed["decision_ref"]]
        self.assertEqual(handoff["status"], "ENACTED")
        self.assertEqual([e["subject"] for e in handoff["effects"]], [refs["local:match"]])
