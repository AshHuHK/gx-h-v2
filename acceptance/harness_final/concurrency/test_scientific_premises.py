"""Scientific premises at the SQLite graph CAS, exercised through ordinary Research Goals."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from acceptance.harness_refactor.modules.research_study import ResearchStudy
from acceptance.harness_refactor.probes.test_probes import CAP, start
from acceptance.v622.test_close import close_decision
from acceptance.v62a.interleavings_fixture import PRODUCT, branch, draft
from gx_core import budgets, contracts
from gx_core.errors import StaleRevision
from gx_core.p0 import capability_views, harness_policy, v6_decisions, v6_native
from gx_core.p0.roots import Roots
from gx_core.persistence import store


class ScientificPremises(unittest.TestCase):
    def worker_session(self, s, method):
        args = s.open_args("calibration", inputs=s.materials, caps=[method],
                           limits={"api_calls":2,"worker_tasks":2,"tokens":1000,
                                   "job_submissions":1,"cpu_seconds":15})
        args.update(module_id="ad_hoc", expected_delivery="Check the instrument recording.")
        commissioned = s.call(s.lead,"gx_open_module",args,"commission")
        peer = ResearchStudy(s.directory,reopen=True)
        peer.run_id,peer.materials = s.run_id,s.materials
        peer.conn = self.enterContext(closing(store.open_store(s.state/"gx.db")))
        peer.roots = Roots(peer.conn,run_id=s.run_id,cwd=s.state,token_budget=10)
        peer.roots.client = s.roots.client
        return peer,peer.bind(commissioned["task_id"])

    def between_prepare_and_commit(self, study, action):
        prepare = v6_decisions.prepare
        calls = []

        def interleave(conn, **identity):
            self.assertFalse(conn.in_transaction)
            plan = prepare(conn,**identity)
            if conn is study.conn and not calls:
                calls.append(True)
                action()
            return plan
        return patch.object(v6_decisions,"prepare",side_effect=interleave)

    def archive(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        return self.enterContext(ResearchStudy(folder,spec_updates={
            "research_effort":"deep","capabilities":["fixture-grid@1"],"research_modules":["ad_hoc@1"]}))

    def test_rebased_prediction_keeps_registered_material_endpoint_and_both_outcome_actions(self):
        study = start(self,with_metric=True,delegated=True)
        peer,worker = self.worker_session(study,CAP)
        original = study.materials[0]
        prediction = {"blind_id":"gauge-validation-1", "expected":"The held-out mean exceeds 5 mV.",
                      "on_expected":"Compare the second station using the same averaging window.",
                      "on_alternative":"Examine the calibration history before comparing stations."}
        method = {"capability":CAP, "parameters":{}, "output_name":"readings.json","metric_id":"mean","unit":"mV"}
        route = branch("calibrated")
        route.update(hypothesis="The station's mean exceeds 5 mV.",alternative="Its mean is at most 5 mV.",
                     discriminating_observation="A held-out reading averaged in mV.",stop_condition="Record the first comparison.")
        decision = draft(route,evidence=[original],watches=[{"event":"evidence_added","subject_ref":"run"}])
        decision["comparison_plan"] = {"branch":route["branch"], "material_role":"readings","reference_refs":[original],
                                       "method":method, "prediction":prediction}
        child = []
        def child_commits():
            child.append(peer.submit_node(worker,"independent",capability=CAP))
        with self.between_prepare_and_commit(study,child_commits):
            accepted = study.call(study.lead,v6_native.DECIDE,{"decision":decision},"register")
        self.assertEqual(accepted["base"],child[0]["result_head"])
        frozen = accepted["comparison"]
        self.assertEqual((frozen["prediction"],frozen["method"],frozen["reference_refs"]),(prediction,method,[original]))
        self.assertTrue(frozen["measurement_spec_sha256"])
        stored, = study.rows("SELECT request_json FROM v6_decisions WHERE decision_ref=?",accepted["decision_ref"])
        self.assertEqual(json.loads(stored["request_json"])["original_decision"],decision)
        self.assertEqual(accepted,study.call(study.lead,v6_native.DECIDE,{"decision":decision},"register"))
        self.assertEqual(len(study.rows("SELECT node_id FROM v6_nodes")),1)
        self.assertEqual(study.rows("SELECT job_id FROM jobs"),[])

    def test_changed_funding_blocks_original_branch_without_overwriting_worker(self):
        study = self.archive()
        peer,worker = self.worker_session(study,"fixture-grid@1")
        budget, = peer.conn.execute("SELECT budget_ref FROM runs WHERE run_id=?",(study.run_id,)).fetchone()
        def change_funding():
            peer.submit_node(worker,"archive",capability="fixture-grid@1",parameters={"operation":"same"})
            budgets.reserve(peer.conn,budget_ref=budget,subject_kind="job",subject_id="committed-calibration",
                            amounts={"cpu_seconds":1},actor="instrument")
        with self.between_prepare_and_commit(study,change_funding):
            with self.assertRaisesRegex(StaleRevision,"Funding changed"):
                study.call(study.lead,v6_native.DECIDE,{"decision":draft(branch("calibration"))},"commit-funding")
        self.assertEqual(study.head()["version"],1)
        self.assertEqual(len(study.rows("SELECT 1 FROM v6_nodes")),1)
        self.assertEqual(study.rows("SELECT 1 FROM v6_decisions WHERE task_id=?",study.lead.task_id),[])

    def test_token_only_accounting_does_not_supersede_cpu_only_precommit(self):
        study = self.archive()
        cap = "fixture-grid@1"
        peer,worker = self.worker_session(study,cap)
        budget, = peer.conn.execute("SELECT budget_ref FROM runs WHERE run_id=?",(study.run_id,)).fetchone()
        def update_independent_work():
            peer.submit_node(worker,"archive",capability=cap,parameters={"operation":"same"})
            budgets.reserve(peer.conn,budget_ref=budget,subject_kind="native_turn",subject_id="observer-token",
                            amounts={"tokens":1},actor="observer")
        route = branch("measurement")
        self.assertEqual(set(route["budget_limits"]),{"job_submissions","cpu_seconds"})
        with self.between_prepare_and_commit(study,update_independent_work):
            receipt = study.call(study.lead,v6_native.DECIDE,{"decision":draft(route)},"open-cpu-branch")
        self.assertEqual(receipt["result_head"]["version"],2)
        self.assertEqual(study.rows("SELECT count(*) AS n FROM budget_reservations WHERE subject_id='observer-token'"),
                         [{"n":1}])
        self.assertEqual(len(study.rows("SELECT 1 FROM v6_nodes")),1)

    def test_disabled_selected_method_blocks_graph_retry(self):
        study = self.archive()
        cap = "fixture-grid@1"
        peer,worker = self.worker_session(study,cap)
        def withdraw_method():
            peer.submit_node(worker,"archive",capability=cap,parameters={"operation":"same"})
            capability_views.change(peer.conn,study.run_id,disable=cap,expected_revision=0,request_id="withdraw")
        route = branch("independent")
        node = study.node(study.lead,"compare",capability=cap,branch_id=route["branch"],parameters={"operation":"same"})
        with self.between_prepare_and_commit(study,withdraw_method):
            with self.assertRaisesRegex(StaleRevision,"approved materials, scope or decision premises changed"):
                study.call(study.lead,v6_native.DECIDE,{"decision":draft(route,node)},"commit-withdrawn-method")
        self.assertNotIn(cap,capability_views.current(study.conn,study.run_id)["active"])
        self.assertEqual(len(study.rows("SELECT 1 FROM v6_nodes")),1)
        self.assertEqual(study.head()["version"],1)

    def test_completed_report_invalidates_pending_scientific_closure(self):
        study = self.archive()
        peer,worker = self.worker_session(study,"fixture-grid@1")
        source, = study.materials
        peer.call(worker,"gx_read_artifact",{"ref":source,"operation_key":"read"},"read")
        report = []
        def publish_report():
            report.append(peer.report(worker,source,text="The archival date is only a month, so exact timing is unresolved."))
        with self.between_prepare_and_commit(study,publish_report):
            with self.assertRaises(StaleRevision) as error:
                study.call(study.lead,v6_native.DECIDE,{"decision":close_decision([source])},"close-before-review")
        self.assertEqual(error.exception.details["reason"],"unseen_module_report")
        self.assertEqual(study.rows("SELECT report_ref FROM v62_module_reports"),[{"report_ref":report[0]["report_ref"]}])
        self.assertEqual(study.rows("SELECT status FROM v6_goals"),[{"status":"OPEN"}])
        self.assertEqual(study.rows("SELECT 1 FROM v6_decisions"),[])

    def test_original_harness2_goal_cold_opens_and_fences_same_write(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        release = folder/"baseline"
        release.mkdir()
        revisions = ("4b6aa93ad5bf638d06bed7004c26c27d030a59ea","6ec604d176d307c27b3c4f7d3e7ee02e37325bce")
        baseline = next((rev for rev in revisions if subprocess.run(
            ["git","merge-base","--is-ancestor",rev,"HEAD"],cwd=PRODUCT,capture_output=True).returncode == 0),None)
        if baseline is None:
            self.skipTest("This packaged checkout has no released ancestor available for historical cold admission")
        with tempfile.TemporaryFile() as archive:
            subprocess.run(["git","archive",baseline,"gx-h","bin"],cwd=PRODUCT,stdout=archive,check=True)
            archive.seek(0)
            with tarfile.open(fileobj=archive) as files:
                files.extractall(release,filter="data")
        directory = folder/"legacy"
        directory.mkdir()
        source = directory/"archive.txt"
        source.write_text("Field station records provide only the month.")
        config = directory/"goal.json"
        config.write_text(json.dumps({"research_effort":"deep","research_modules":["ad_hoc@1"],
            "capabilities":["fixture-grid@1"],"input_files":[{"path":str(source),"role":"material"}]}))
        cmd = [sys.executable,"-B",str(release/"bin/gx-p0"),"research","--state",str(directory/"state"),
               "--goal","What is the archive time resolution?","--goal-config",str(config),"--run-key","legacy",
               "--timeout","0.001"]
        result = subprocess.run(cmd,cwd=release,env={**os.environ,"PYTHONPATH":str(release/"gx-h"),"GX_CODEX_OFFLINE":"1"},
                                capture_output=True,text=True,timeout=45)
        self.assertEqual(result.returncode,75,result.stderr+result.stdout)
        for name in ("gx_core","contracts","config"):
            shutil.copytree(PRODUCT/"gx-h"/name,release/"gx-h"/name,dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
        resumed = subprocess.run(cmd,cwd=release,env={**os.environ,"PYTHONPATH":str(release/"gx-h"),"GX_CODEX_OFFLINE":"1"},
                                 capture_output=True,text=True,timeout=45)
        self.assertEqual(resumed.returncode,75,resumed.stderr+resumed.stdout)
        self.assertEqual(json.loads(result.stdout)["run_id"],json.loads(resumed.stdout)["run_id"])
        self.enterContext(patch.object(contracts,"HARNESS_ROOT",release/"gx-h"))
        with ResearchStudy(directory,reopen=True) as study:
            self.assertEqual(study.run_id,json.loads(result.stdout)["run_id"])
            self.assertEqual(harness_policy.version(study.conn,study.run_id),"gx.harness/2")
            lead = study.next_actor("run")
            peer,worker = self.worker_session(study,"fixture-grid@1")
            def child_commits():
                peer.submit_node(worker,"archive",capability="fixture-grid@1",parameters={"operation":"same"})
            with self.between_prepare_and_commit(study,child_commits):
                with self.assertRaisesRegex(StaleRevision,"graph moved during preparation"):
                    study.call(lead,v6_native.DECIDE,{"decision":draft(branch("parallel"))},"old-contract")
            self.assertEqual(study.head()["version"],1)
            self.assertEqual(len(study.rows("SELECT 1 FROM v6_nodes")),1)


if __name__ == "__main__":
    unittest.main()
