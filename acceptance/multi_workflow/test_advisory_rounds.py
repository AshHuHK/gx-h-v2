"""Normal native Lead callbacks after an unproductive round on deposited PCNA."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.multi_workflow.test_pcna_native import INTERFACE, MATERIALS, PLUGIN
from acceptance.v62a.interleavings_fixture import PRODUCT, branch, draft
from acceptance.v622.test_close import SurveyedStudy, actual_packet, close_decision
from gx_core.artifacts import ArtifactService
from gx_core.errors import GxError, ScopeDenied
from gx_core.p0 import cli, v6_goal, v62_catalog, v62_rounds


class RoundStudy(unittest.TestCase):
    def setUp(self):
        if output := os.environ.get("GX_MULTITURN_ROUNDS_TRACE"):
            self.work = Path(output) / self._testMethodName
            self.work.mkdir(parents=True, exist_ok=False)
        else:
            self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        folder, digest = MATERIALS["1VYM"]
        coordinate = PRODUCT / "examples/pcna" / folder / "1VYM.cif"
        settings = {
            "project": "advisory-pcna-qc", "research_effort": "deep",
            "plugins": [PLUGIN], "research_modules": ["data_inventory@1"],
            "module_policy": {"no_progress_rounds": 1, "max_rounds": 2},
            "capabilities": [INTERFACE], "input_files": [{"role": "structure",
                "path": str(coordinate), "acquisition": str(coordinate.with_suffix(".json")), "sha256": digest}],
        }
        grant = self.work / "grant.json"
        grant.write_text(json.dumps(settings))
        with patch.object(cli, "run_until_stopped", side_effect=lambda conn, args, rid, timeout: cli.snapshot(conn, rid)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["research", "--state", str(self.work / "state"), "--run-key", "pcna-stagnation",
                "--goal", "Assess whether the first deposited PCNA pair contains a measurable interface.",
                "--goal-config", str(grant)]), 0)
        self.s = self.enterContext(SurveyedStudy(self.work, reopen=True))
        self.s.next_actor("run")
        self.source, = self.s.materials
        worker = self.s.open("initial-inventory", inputs=[self.source], caps=[])
        actor = self.s.bind(worker["task_id"])
        self.route_spec = dict(branch("pair"), hypothesis="The deposited pair forms an interface.",
                               alternative="The subunits are separated in this deposited structure.",
                               method_family=INTERFACE,
                               discriminating_observation="Count distinct residue pairs in atomic contact.",
                               budget_limits={"job_submissions": 1, "cpu_seconds": 400})
        initial = self.decide("original-route", draft(self.route_spec, evidence=[self.source]))
        self.route = initial["aliases"]["local:pair"]
        self.s.call(actor, "gx_read_artifact", {"ref": self.source, "operation_key": "read"}, "read")
        self.report = self.s.report(actor, self.source)["report_ref"]
        self.s.next_actor("run")
        self.progress = self.s.brief(self.s.lead)["research_progress"]
        self.assertEqual(self.progress["consecutive_no_progress"], 1)

    def decide(self, key, decision):
        return self.s.call(self.s.lead, "gx_decide_revision", {"decision": decision}, key)


class AdvisoryRounds(RoundStudy):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.progress["phase"], "reconsider")
        packet = actual_packet(self.s, self.s.lead)["packet"]
        self.assertIn("competing explanations", packet["research_progress"]["guidance"])
        self.assertEqual(set(self.progress["decision_options"]), {"explore", "wait", "enter_convergence", "close_run"})

    def test_existing_route_executes_discriminating_measurement(self):
        op = self.s.node(self.s.lead, "contact-count", self.source, branch_id=self.route, capability=INTERFACE,
                         parameters={"pair": "12", "block_index": -1})
        op["question"] = "Are there any residue pairs in contact across the first deposited interface?"
        decision = draft(op, evidence=[self.report, self.source], watches=[{"event": "job_settled",
                                                                           "subject_ref": "local:contact-count"}])
        decision["rationale"] = "A positive count would establish contact; zero would leave this interface unsupported."
        adopted = self.decide("measure-the-same-route", decision)
        result = self.s.settle(adopted["aliases"]["local:contact-count"])
        self.assertEqual((result["status"], result["branch_id"]), ("SUCCEEDED", self.route))
        artifact = ArtifactService(self.s.conn).read_json(result["result_ref"])
        self.assertEqual(artifact["n_contact_residue_pairs"], 33)

    def test_delegate_again_then_wait_without_duplicate_jobs_and_honor_round_limit(self):
        with self.assertRaises(ScopeDenied):
            self.s.open("outside-grant", round=3, inputs=[self.source], caps=[])
        passes = []
        guard = v62_rounds.require_exploration
        def record_guard(conn, run_id):
            passes.append(conn.in_transaction)
            return guard(conn, run_id)
        with patch.object(v62_rounds, "require_exploration", record_guard):
            opened = self.s.open("new-inquiry", round=2, inputs=[self.source], caps=[])
        self.assertEqual(passes, [False, True])
        self.assertEqual(self.s.open("new-inquiry", round=2, inputs=[self.source], caps=[]), opened)
        self.decide("wait-for-assessment", draft(watches=[{"event": "module_report", "subject_ref": "run"}]))
        self.assertIsNone(v6_goal.ensure_task(self.s.conn, self.s.run_id))
        self.assertFalse(self.s.rows("SELECT job_id FROM jobs"))

    def test_measurement_outside_branch_resources_creates_no_job(self):
        tight = dict(self.route_spec, branch="local:smaller", budget_limits={"job_submissions": 1, "cpu_seconds": 1})
        node = self.s.node(self.s.lead, "too-expensive", self.source, branch_id=tight["branch"], capability=INTERFACE,
                           parameters={"pair": "12", "block_index": -1})
        self.decide("outside-pi-resources", draft(tight, node, evidence=[self.source, self.report]))
        self.s.advance()
        self.assertFalse(self.s.rows("SELECT job_id FROM jobs"))
        self.assertFalse(self.s.rows("SELECT reservation_ref FROM budget_reservations WHERE subject_kind='job'"))
        wait, = self.s.rows("SELECT wait_json FROM v6_node_waits JOIN v6_nodes USING(node_id) WHERE status='PENDING'")
        gap, = json.loads(wait["wait_json"])["details"]["shortfalls"]
        self.assertEqual((gap["key"], gap["available"], gap["short_by"]),
                         ("cpu_seconds", 1, gap["requested"] - 1))
        self.assertGreater(gap["short_by"], 0)

    def test_wait_for_new_evidence_without_outstanding_jobs(self):
        self.decide("await-new-source", draft(evidence=[self.report]))
        self.assertIsNone(v6_goal.ensure_task(self.s.conn, self.s.run_id))
        self.assertFalse(self.s.rows("SELECT job_id FROM jobs"))

    def test_explicit_convergence_remains_binding_after_restart(self):
        decide = draft(evidence=[self.report])
        decide["convergence"] = {"spec_version": "interface-qc/1", "candidates": ["deposited-interface"]}
        receipt = self.decide("enter-convergence", decide)
        self.s.stack.close()
        with SurveyedStudy(self.work, reopen=True) as resumed:
            progress = v62_rounds.brief(resumed.conn, resumed.run_id, None)["research_progress"]
            self.assertEqual(progress["convergence"], receipt["convergence"])
            self.assertEqual(progress["phase"], "converging")
            with self.assertRaises(ScopeDenied):
                v62_rounds.require_exploration(resumed.conn, resumed.run_id)
            note = ArtifactService(resumed.conn).publish_bytes(
                namespace=resumed.run_id + "/notes", rel_path="assess.txt", data=b"Assess the selected candidate.",
                kind="operator_note", run_id=resumed.run_id, actor="operator", origin="INGEST")
            v6_goal.record_observation(resumed.conn, resumed.run_id, note.artifact_ref, source="operator")
            lead = resumed.next_actor("run")
            with self.assertRaises(ScopeDenied):
                resumed.open("explore-after-convergence", round=2, inputs=[self.source], caps=[])
            with self.assertRaises(ScopeDenied):
                resumed.call(lead, "gx_decide_revision", {"decision": draft(branch("new-hypothesis"),
                    evidence=[note.artifact_ref])}, "reopen-exploration")
            self.assertEqual(len(resumed.rows("SELECT event_id FROM events WHERE kind='v62.convergence.entered'")), 1)

    def test_lead_can_close_with_cited_limitations(self):
        receipt = self.decide("stop", close_decision([self.report]))
        self.s.advance()
        goal, = self.s.rows("SELECT status,stop_decision_ref FROM v6_goals")
        self.assertEqual(goal, {"status": "CLOSED", "stop_decision_ref": receipt["decision_ref"]})
        with self.assertRaises(GxError):
            self.s.open("sealed", round=2, inputs=[self.source], caps=[])
        self.assertFalse(self.s.rows("SELECT job_id FROM jobs"))


class StoredFixedRounds(RoundStudy):
    def setUp(self):
        # Reproduce the catalog bytes written by pre-upgrade admission with the same evidence version.
        current_catalog = v62_catalog.for_start

        def old_catalog(spec):
            catalog = current_catalog(spec)
            catalog["policy"].pop("stagnation_policy")
            return catalog

        with patch.object(v62_catalog, "for_start", old_catalog):
            super().setUp()

    def test_unmarked_existing_current_run_keeps_fixed_policy(self):
        self.assertEqual(self.progress["phase"], "convergence_due")
        self.assertEqual(self.progress["decision_options"], ["enter_convergence", "close_run"])
        with self.assertRaises(ScopeDenied):
            self.s.open("forbidden-extra-round", round=2, inputs=[self.source], caps=[])
        with self.assertRaises(ScopeDenied):
            self.decide("wait-indefinitely", draft(evidence=[self.report]))
