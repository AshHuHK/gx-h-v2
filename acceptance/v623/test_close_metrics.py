"""D2. Exercise a real work result, bounded module projection and native closing decision."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from acceptance.v6.export import capture
from acceptance.v6.project_window import Capture
from acceptance.v621.test_verified_metrics import METHOD, SOURCE_METADATA
from acceptance.v62a.interleavings_fixture import draft
from acceptance.v622.test_close import CloseStudy, actual_packet, close_decision
from acceptance.v622 import test_reports as report_fixture
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import GxError, StaleRevision
from gx_core.p0 import v6_decisions, v6_goal, v6_jobs, v6_native


def trace(name, receipt):
    path = os.environ.get("GX_D2_TRACE")
    if path:
        Path(path).mkdir(parents=True, exist_ok=True)
        (Path(path) / (name + ".json")).write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class EndpointClose(unittest.TestCase):
    def measured(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with patch("acceptance.v622.test_reports.Scenario", CloseStudy):
            prepared = report_fixture.ReportEvidence.measured_output(
                self, directory, module_policy={"report_bytes": 512, "brief_bytes": 4096})
        return directory, prepared

    def report(self, s, assets, output, worker):
        selected = s.report(worker, output, key="measured", findings=[],
                            metric_refs=[{"artifact_ref": output, "metric_id": "coverage"}],
                            qualitative_notes=[{"text": "The analyst estimates ninety percent／九成。",
                                                "source_refs":[output]}])
        lead, _, _ = report_fixture.ReportEvidence.delivered_report(self, s, selected)
        report = assets.read_json(selected["report_ref"])
        return selected["report_ref"], report, lead, actual_packet(s, lead)

    def read_page(self, s, actor, ref, offset, key, *, acknowledge=True):
        args = {"ref": ref, "endpoint_offset": offset}
        page = s.call(actor, "gx_read_artifact", args, key)
        if acknowledge:
            # This is the successful host tool-completion envelope. A GX callback result
            # on its own cannot establish whether bytes reached the specified actor.
            s.roots.delivery.observe("item/completed", {"threadId": actor.thread, "turnId": actor.turn,
                "item": {"type": "dynamicToolCall", "tool": "gx_read_artifact", "id": key, "arguments": args,
                         "status": "completed", "success": True, "contentItems": [
                             {"type": "inputText", "text": ids.canonical_json(page)}]}})
        return page

    def measured_multiple(self, *, worker_reads=False, opening=None):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = {"research_effort": "deep", "research_modules": ["data_inventory@2"],
                  "plugins": ["org.gx.protein@0.3.0"], "capabilities": [METHOD],
                  "initial_job_limit": 6, "module_policy": {"report_bytes": 512, "brief_bytes": 4096}}
        s = self.enterContext(CloseStudy(directory / "run", spec_updates=config))
        inputs = s.materials[:2]
        for ref, accession in zip(inputs, ("2RE9", "2O0O"), strict=True):
            acquisition = json.loads(SOURCE_METADATA.with_name(accession + ".json").read_bytes())
            v6_goal.record_observation(s.conn, s.run_id, ref, source="archive-" + accession, acquisition=acquisition)
        grant = s.open_args("multi", inputs=inputs, caps=[METHOD],
                            limits={"job_submissions": 6, "cpu_seconds": 220, "api_calls": 8,
                                    "worker_tasks": 8, "tokens": 8000})
        module = s.call(s.lead, "gx_open_module", {**grant, "module_version": "2"}, "commission")
        actor = s.bind(module["task_id"])
        s.call(s.lead, v6_native.DECIDE, {"decision": opening or draft()}, "wait")
        ops = [s.node(actor, f"input-{i}-{mask}", input_ref=ref, capability=METHOD, parameters={"mask": mask})
               for i, ref in enumerate(inputs) for mask in ("common_ca", "common_backbone", "ordered_backbone")]
        plan = draft(*ops, watches=[{"event": "job_settled", "subject_ref": op["node"]} for op in ops])
        s.call(actor, v6_native.DECIDE, {"decision": plan}, "measure")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            v6_jobs.advance(s.conn, s.run_id)
            nodes = s.rows("SELECT node_id,job_id,result_ref,status FROM v6_nodes ORDER BY node_id")
            if all(n["status"] in v6_jobs.NODE_TERMINAL for n in nodes):
                break
            time.sleep(.03)
        self.assertEqual([n["status"] for n in nodes], ["SUCCEEDED"] * 6, nodes)
        s.advance()
        actor = s.next_actor(module["scope"])
        outputs = [n["result_ref"] for n in nodes]
        if opening:
            mask = opening["comparison_plan"]["method"]["parameters"]["mask"]
            # The matching measurements intentionally arrive last at publication.
            assets = ArtifactService(s.conn)
            outputs.sort(key=lambda ref: assets.read_json(ref)["mask"] == mask)
        if worker_reads:
            for i, ref in enumerate(outputs):
                page = self.read_page(s, actor, ref, 0, f"worker-page-{i}")
                self.assertEqual(len(page["verified_metrics"]), 1)
        selectors = [{"artifact_ref": ref, "metric_id": "coverage"} for ref in outputs]
        submitted = s.report(actor, outputs[0], key="all-readings", findings=[], artifact_refs=outputs,
                             metric_refs=selectors,
                             qualitative_notes=[{"text": "The selections differ.", "source_refs": outputs}])
        lead, _, _ = report_fixture.ReportEvidence.delivered_report(self, s, submitted)
        report = ArtifactService(s.conn).read_json(submitted["report_ref"])
        self.assertEqual(len(report["verified_metrics"]), 6)
        return directory, s, actor, lead, nodes, submitted["report_ref"], report

    def test_registered_mask_leads_in_a_tight_real_report(self):
        from acceptance.v623.test_decision_action import comparison_open
        from gx_core.p0 import v623_endpoints
        opening = comparison_open()
        _, s, _, lead, _, ref, report = self.measured_multiple(opening=opening)
        metrics = report["verified_metrics"]
        keys = {r["key"]:m for r,m in zip(v623_endpoints.compact_rows(metrics), metrics, strict=True)}
        packet = actual_packet(s, lead)
        delivered = next(r for r in packet["packet"]["module_reports"] if r["report_ref"] == ref)
        mask = opening["comparison_plan"]["method"]["parameters"]["mask"]
        order = [keys[r["key"]]["scope"]["mask"] for r in delivered["endpoint_rows"]]
        self.assertEqual([m["scope"]["mask"] for m in metrics][-2:], [mask, mask])
        self.assertTrue(delivered["pending_endpoints"])
        trace("registered-endpoint-priority", {"cpu_jobs":len(metrics), "report_limit_bytes":512,
              "delivered_utf8_bytes":len(ids.canonical_json(delivered).encode()),
              "report_masks":[m["scope"]["mask"] for m in metrics], "delivered_masks":order,
              "registered_mask":mask, "delivery_sha256":packet["text_sha256"], "pending":delivered["pending_endpoints"]})
        self.assertEqual(order[:2], [mask, mask], f"delivered masks {order} from six settled Jobs")
        self.assertEqual(delivered["endpoint_total"], 6)

    def test_registered_endpoint_precedes_newer_direct_job_with_same_name_and_unit(self):
        from acceptance.v623.test_decision_action import comparison_open
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with CloseStudy(directory/"direct", spec_updates={"research_effort":"deep", "research_modules":[],
                "module_policy":{"brief_bytes":4096}, "initial_job_limit":2, "capabilities":[METHOD],
                "plugins":["org.gx.protein@0.3.0"]}) as s:
            opening = comparison_open()
            opening["ops"][0]["budget_limits"] = {"job_submissions":2,"cpu_seconds":120}
            ops = [s.node(s.lead, alias, s.materials[0], branch_id="local:route", capability=METHOD,
                          parameters={"mask":mask}) for alias,mask in (("primary","common_ca"),("later","ordered_backbone"))]
            ops[1]["depends_on"] = ["local:primary"]
            opening["ops"].extend(ops)
            opening["reconsider_when"] = [{"event":"job_settled","subject_ref":"local:later"}]
            admitted = s.call(s.lead, v6_native.DECIDE, {"decision":opening}, "competing-jobs")
            deadline = time.monotonic()+35
            while time.monotonic()<deadline:
                v6_jobs.advance(s.conn,s.run_id)
                nodes = s.rows("SELECT node_id,result_ref,status FROM v6_nodes")
                if len(nodes)==2 and all(n["status"] in v6_jobs.NODE_TERMINAL for n in nodes):
                    break
                time.sleep(.03)
            self.assertEqual([n["status"] for n in nodes], ["SUCCEEDED"]*2)
            expected = {n["result_ref"]:alias for alias,node in admitted["aliases"].items()
                        for n in nodes if n["node_id"]==node}
            lead = s.next_actor("run")
            packet = actual_packet(s,lead)["packet"]
            self.assertIn("full_brief_ref",packet)  # exercise the actual bounded transport
            order = [expected[o["artifact_ref"]] for o in packet["observations"] if o.get("endpoint_rows")]
            self.assertEqual(order,["local:primary","local:later"])

    def exported(self, directory, s, receipt):
        path = directory / "export"
        capture(s.state / "gx.db", s.run_id, path)
        return json.loads(Capture(path).decisions[receipt["decision_ref"]]["receipt_json"])

    def test_report_citation_alone_does_not_prove_use_of_its_measurement(self):
        directory, (s, assets, origin, node, output, worker) = self.measured()
        ref, report, lead, actual = self.report(s, assets, output, worker)
        decision = close_decision([ref])
        decision["rationale"] = "Close on the registered coverage reported by the module."
        try:
            reply = s.call(lead, v6_native.DECIDE, {"decision": decision}, "cites-report-only")
            refused = None
        except GxError as exc:
            reply = None
            refused = {"code": exc.code, "reason": exc.message, "details": exc.details}
        exported = self.exported(directory, s, reply) if reply else None
        readings = report["verified_metrics"]
        trace("report-only", {"run_id": s.run_id, "source": origin, "actual_job_id": node["job_id"],
              "output_ref": output, "report_ref": ref, "report_sha256": assets.describe(ref)["sha256"],
              "actual_lead_delivery": actual, "reported_readings": readings,
              "lead_read_calls": s.rows("SELECT call_id FROM p0_v6_native_calls WHERE thread_id=? AND json_extract(request_json,'$.tool')='gx_read_artifact'",
                                        lead.thread),
              "closing_request": decision, "adopted": reply, "exported": exported, "refused": refused,
              "provider_calls": 0, "transport": "RootDelivery with local scripted transport"})
        self.assertIsNone(reply, "A bare report reference was accepted as the declared basis for closure.")
        self.assertEqual(s.rows("SELECT status FROM v6_goals"), [{"status": "OPEN"}])
        self.assertEqual(s.rows("SELECT job_id FROM jobs"), [{"job_id": node["job_id"]}])

    def test_single_display_is_received_before_a_cited_close(self):
        directory, (s, assets, _, node, output, worker) = self.measured()
        ref, report, lead, packet = self.report(s, assets, output, worker)
        reading, = report["verified_metrics"]
        self.assertEqual(reading["display"], "49/164 (0.299)")
        preview, = [r for r in packet["packet"]["module_reports"] if r["report_ref"] == ref]
        self.assertIn("49/164", json.dumps(preview["endpoint_rows"], ensure_ascii=False))
        self.assertLessEqual(len(ids.canonical_json(preview).encode()), 512)
        decision = close_decision([ref])
        decision["metric_refs"] = [reading["metric_ref"]]
        prior = s.rows("SELECT job_id,reservation_ref FROM jobs")
        closed = s.call(lead, v6_native.DECIDE, {"decision": decision}, "cites-measurement")
        context = closed["close_context"]
        self.assertEqual(context["used_metric_refs"], [reading["metric_ref"]])
        self.assertIn(reading["metric_ref"], context["received_metric_refs"])
        self.assertNotIn(reading["metric_ref"], context["unread_metric_refs"])
        self.assertEqual(closed, self.exported(directory, s, closed))
        self.assertEqual(s.rows("SELECT job_id,reservation_ref FROM jobs"), prior)
        trace("delivered-use", {"run_id": s.run_id, "job_id": node["job_id"], "output_ref": output,
              "report": ref, "actual_delivery": packet, "used": closed, "external_paid_calls": 0})

    def test_deferred_endpoints_need_successful_lead_read_then_replay_same_close(self):
        directory, s, worker, lead, nodes, ref, report = self.measured_multiple(worker_reads=True)
        packet = actual_packet(s, lead)
        preview, = [r for r in packet["packet"]["module_reports"] if r["report_ref"] == ref]
        references = [reading["metric_ref"] for reading in report["verified_metrics"]]
        self.assertTrue(preview["pending_endpoints"])
        self.assertLess(len(preview.get("endpoint_rows", [])), len(references))
        decision = close_decision([ref])
        decision["metric_refs"] = references
        with self.assertRaises(GxError):
            s.call(lead, v6_native.DECIDE, {"decision": decision}, "cannot-use-worker-read")

        # Start with a real callback for every page but omit successful transport receipts.
        offsets, offset, all_refs = [], 0, set()
        while offset is not None:
            self.assertNotIn(offset, offsets)
            offsets.append(offset)
            page = self.read_page(s, lead, ref, offset, f"undelivered-page-{offset}", acknowledge=False)
            for item in page["endpoint_rows"]:
                matched, = [r for r in references if r.removeprefix("metric_").startswith(item["key"])]
                all_refs.add(matched)
            offset = page["next_endpoint_offset"]
        self.assertEqual(all_refs, set(references))
        self.assertGreaterEqual(len(offsets), 2)
        with self.assertRaises(GxError):
            s.call(lead, v6_native.DECIDE, {"decision": decision}, "cannot-use-lost-reply")

        pages = [self.read_page(s, lead, ref, i, f"read-page-{i}") for i in offsets]
        jobs = s.rows("SELECT job_id,reservation_ref FROM jobs ORDER BY job_id")
        s.__exit__(None, None, None)
        with CloseStudy(directory / "run", reopen=True) as resumed:
            recovered = resumed.next_actor("run")
            self.assertEqual((recovered.thread, recovered.turn), (lead.thread, lead.turn))
            # The Lead chooses only one reading, although every page was delivered.
            decision["metric_refs"] = [references[-1]]
            body = {"decision": decision}
            closed = resumed.call(recovered, v6_native.DECIDE, body, "after-page-delivery")
            self.assertEqual(resumed.call(recovered, v6_native.DECIDE, body, "after-page-delivery"), closed)
            context = closed["close_context"]
            self.assertEqual(set(context["received_metric_refs"]) & set(references), set(references))
            self.assertEqual(context["used_metric_refs"], decision["metric_refs"])
            self.assertFalse(set(context["unread_metric_refs"]) & set(references))
            self.assertEqual(jobs, resumed.rows("SELECT job_id,reservation_ref FROM jobs ORDER BY job_id"))
            self.assertEqual(closed, self.exported(directory, resumed, closed))
            trace("read-and-resume", {"run_id": resumed.run_id, "job_records": jobs, "report_ref": ref,
                  "worker": worker.thread, "lead": lead.thread, "actual_delivery": packet,
                  "confirmed_pages": pages, "close": closed, "replay_identical": True, "external_paid_calls": 0})

    def test_late_observation_then_partial_cited_close_preserves_other_unread_results(self):
        directory, s, _, lead, nodes, ref, report = self.measured_multiple()
        old = actual_packet(s, lead)
        preview, = [r for r in old["packet"]["module_reports"] if r["report_ref"] == ref]
        self.assertGreater(len(preview["endpoint_rows"]), 0)
        citation = ref + "#" + preview["endpoint_rows"][0]["key"]
        decision = close_decision([ref])
        decision.update(metric_refs=[citation], rationale="The conclusion concerns only the cited specimen and mask.")
        prepare = v6_decisions.prepare

        def interleave(*args, **kwargs):
            plan = prepare(*args, **kwargs)
            v6_goal.record_observation(s.conn, s.run_id, s.materials[-1], source="late-operator-note")
            return plan

        with patch.object(v6_decisions, "prepare", interleave), self.assertRaises(StaleRevision) as stale:
            s.call(lead, v6_native.DECIDE, {"decision": decision}, "freshness-race")
        self.assertEqual(stale.exception.details["reason"], "close_context_changed")
        successor = s.next_actor("run")
        current = actual_packet(s, successor)
        offered, = [r for r in current["packet"]["module_reports"] if r["report_ref"] == ref]
        row = offered["endpoint_rows"][0]
        decision["metric_refs"] = [ref + "#" + row["key"]]
        closed = s.call(successor, v6_native.DECIDE, {"decision": decision}, "scoped-conclusion")
        concrete, = [r["metric_ref"] for r in report["verified_metrics"]
                     if r["metric_ref"].removeprefix("metric_").startswith(row["key"])]
        endpoints = closed["endpoint_use"]
        refs = {m["metric_ref"] for m in report["verified_metrics"]}
        self.assertEqual(endpoints["used_metric_refs"], [concrete])
        self.assertTrue(set(endpoints["unread_metric_refs"]) & refs)
        self.assertEqual((set(endpoints["received_metric_refs"]) | set(endpoints["unread_metric_refs"])) & refs, refs)
        self.assertFalse(set(endpoints["received_metric_refs"]) & set(endpoints["unread_metric_refs"]))
        self.assertEqual(closed, self.exported(directory, s, closed))
        trace("late-and-partial", {"run_id": s.run_id, "jobs": nodes, "stale_decision": stale.exception.details,
                                  "actual_old_delivery": old, "actual_new_delivery": current,
                                  "adopted": closed, "external_paid_calls": 0})

    def test_explicit_early_stop_preserves_unread_endpoints(self):
        directory, s, _, lead, nodes, ref, report = self.measured_multiple()
        packet = actual_packet(s, lead)
        decision = close_decision([ref])
        decision.update(stop_reason="UNREAD_EVIDENCE", rationale="Stop before interpreting the deferred endpoints.")
        closed = s.call(lead, v6_native.DECIDE, {"decision": decision}, "stop-unread")
        context = closed["close_context"]
        expected = {m["metric_ref"] for m in report["verified_metrics"]}
        received = set(context["received_metric_refs"]) & expected
        self.assertLess(len(received), len(expected))
        self.assertEqual(set(context["unread_metric_refs"]) & expected, expected - received)
        self.assertEqual(context["used_metric_refs"], [])
        self.assertEqual(context["stop_reason"], "UNREAD_EVIDENCE")
        accepted_events = s.rows("SELECT kind,payload_json FROM events WHERE run_id=? AND ("
                                 "kind='v6.run.close_requested' OR event_id=?) ORDER BY event_id",
                                 s.run_id, closed["event_id"])
        self.assertEqual({e["kind"] for e in accepted_events}, {"v6.run.close_requested", "v6.decision.committed"})
        for event in accepted_events:
            payload = json.loads(event["payload_json"])
            self.assertEqual(payload["endpoint_use"], closed["endpoint_use"])
            self.assertEqual(payload["stop_reason"], "UNREAD_EVIDENCE")
        with self.assertRaises(GxError):
            self.read_page(s, lead, ref, 0, "read-after-close")
        self.assertEqual(closed, self.exported(directory, s, closed))
        trace("explicit-unread", {"run_id": s.run_id, "jobs": nodes, "actual_delivery": packet,
              "report": ref, "closed": closed, "reason": decision["stop_reason"], "external_paid_calls": 0})

    def test_superseded_goal_metric_reference_cannot_authorize_the_new_goal(self):
        from acceptance.v621.test_goal_versions import GoalVersions
        directory, (s, assets, _, _, output, worker) = self.measured()
        ref, report, old, old_packet = self.report(s, assets, output, worker)
        metric = report["verified_metrics"][0]["metric_ref"]
        self.assertEqual(old_packet["packet"]["goal_version"], 1)
        self.read_page(s, old, ref, 0, "old-goal-read")
        GoalVersions().change_goal(s)
        lead = s.next_actor("run")
        new_packet = actual_packet(s, lead)
        self.assertEqual(new_packet["packet"]["goal_version"], 2)
        self.assertNotEqual(old.thread, lead.thread)
        decision = close_decision()
        decision["metric_refs"] = [metric]
        with self.assertRaises(GxError):
            s.call(lead, v6_native.DECIDE, {"decision": decision}, "wrong-goal-basis")
        decision = close_decision()
        decision.update(stop_reason="INSUFFICIENT_DATA", rationale="Current scope has no reviewed measurement.")
        closed = s.call(lead, v6_native.DECIDE, {"decision": decision}, "no-current-evidence")
        self.assertNotIn(metric, closed["close_context"]["received_metric_refs"])
        self.assertEqual(closed["close_context"]["used_metric_refs"], [])
        self.assertEqual(closed, self.exported(directory, s, closed))
        trace("old-goal", {"run_id": s.run_id, "official_prior_goal_brief": old_packet,
              "official_new_goal_brief": new_packet, "closed": closed, "external_paid_calls": 0})


if __name__ == "__main__":
    unittest.main()
