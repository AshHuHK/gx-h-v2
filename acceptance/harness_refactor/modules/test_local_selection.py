"""Ordinary native callback controls with actual local CPU Job dispatch."""
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from acceptance.harness_refactor.modules.research_study import ResearchStudy
from gx_core.errors import DependencyUnmet, GxError, StaleRevision
from gx_core.p0 import capability_views, module_methods, v6_jobs

ROOT = Path(__file__).resolve().parents[3]
METHOD = "archival-peptide-coverage@1"


class LocalSelections(unittest.TestCase):
    def test_two_modules_choose_independently_and_job_records_its_provider(self):
        source = ROOT / "acceptance/science_assets/modalities/peptide/materials/1UAO.cif"
        spec = {"research_effort": "medium",
                "research_modules": ["ad_hoc@1"], "evidence_policy": "gx.evidence/6.2.4",
                "input_files": [{"role": "original", "path": str(source)}], "plugins": [],
                "capabilities": [], "capability_collections": ["archived_structure"]}
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with ResearchStudy(tmp, spec_updates=spec) as study:
            actors = []
            for suffix in ("a", "b"):
                p = study.open_args(suffix, caps=[], inputs=study.materials)
                p.update(module_id="ad_hoc", module_version="1", focus="Inspect the coordinate archive",
                         expected_delivery="Describe the coverage of the original.", method_choices=[METHOD])
                response = study.call(study.lead, "gx_open_module", p, "open-" + suffix)
                actors.append(study.next_actor(response["scope"]))
            left, right = actors
            query = {"kind":"method", "reference":METHOD}
            self.assertEqual(study.call(left, "gx_query_catalog", query, "left-scope")["status"], "selectable")
            chosen = {"method": METHOD, "action": "enable", "revision": 0}
            first = study.call(left, "gx_select_capability", chosen, "choose")
            self.assertEqual(first["revision"], 1)
            mine = study.call(left, "gx_query_catalog", query, "left-selected")
            sibling = study.call(right, "gx_query_catalog", query, "right-scope")
            self.assertEqual((mine["status"], mine["capability_revision"]), ("available", 1))
            self.assertEqual((sibling["status"], sibling["capability_revision"]), ("selectable", 0))
            self.assertEqual(first, study.call(left, "gx_select_capability", chosen, "choose"))
            self.assertNotIn(METHOD, capability_views.current(study.conn, study.run_id)["active"])
            with self.assertRaises(GxError):
                study.submit_node(right, "other", capability=METHOD)
            with self.assertRaises(GxError):
                study.call(left, "gx_select_capability", {**chosen, "method":"out-of-scope@1"}, "bad")
            disabled = study.call(left, "gx_select_capability",
                                  {"action":"disable", "method":METHOD, "revision":1}, "release-choice")
            self.assertEqual(disabled["active"], {})
            with self.assertRaises(StaleRevision):
                study.call(left, "gx_select_capability", chosen, "stale")
            selected = study.call(left, "gx_select_capability", {**chosen, "revision":2}, "choose-again")
            self.assertNotEqual(selected["event_id"], first["event_id"])
            study.submit_node(left, "own", capability=METHOD)
            node, = study.rows("SELECT node_id FROM v6_nodes WHERE run_id=?", study.run_id)
            v6_jobs.advance(study.conn, study.run_id)
            admitted = study.rows("SELECT job_id FROM v6_nodes WHERE node_id=?", node["node_id"])[0]["job_id"]
            self.assertTrue(admitted)
            choice = module_methods.options(study.conn, study.run_id, left.scope)[METHOD]
            capability_views.change(study.conn, study.run_id, enable=METHOD, provider=choice["provider"],
                                    expected_revision=0, request_id="operator-on")
            capability_views.change(study.conn, study.run_id, disable=METHOD,
                                    expected_revision=1, request_id="operator-off")
            with self.assertRaises(DependencyUnmet):
                study.call(right, "gx_select_capability", chosen, "now-withdrawn")
            run = study.run_id
        # A fresh Driver process settles the already-admitted Job from its frozen local method event.
        state = tmp / "state"
        command = [sys.executable, "-B", str(ROOT / "bin/gx-p0"), "resume", "--state",
                   str(state), "--run-id", run, "--timeout", "1"]
        deadline = time.monotonic() + 20
        with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.fail(f"Node {node['node_id']} for Job {admitted} did not settle after resume")
                restarted = subprocess.run(command, env={**os.environ, "GX_CODEX_OFFLINE": "1"},
                                           text=True, capture_output=True, timeout=remaining)
                self.assertIn(restarted.returncode, (0, 75), restarted.stderr)
                status, = db.execute("SELECT status FROM v6_nodes WHERE node_id=?",
                                     (node["node_id"],)).fetchone()
                if status in v6_jobs.NODE_TERMINAL:
                    break
        with ResearchStudy(tmp, reopen=True) as study:
            result, = study.rows("SELECT * FROM v6_nodes WHERE node_id=?", node["node_id"])
            self.assertEqual(result["status"], "SUCCEEDED")
            proof = json.loads(study.conn.execute(
                "SELECT admission_json FROM p0_execution_manifests WHERE job_id=?", (result["job_id"],)).fetchone()[0])
            self.assertEqual(result["job_id"], admitted)
            self.assertEqual(proof["module_capability"]["scope"], left.scope)
            self.assertEqual(proof["module_capability"]["event_id"], selected["event_id"])
            self.assertEqual(proof["module_capability"]["plugin"]["id"], "org.gx.archival-molecule-qc")
            self.assertNotIn(METHOD, capability_views.current(study.conn, study.run_id)["active"])
            if destination := os.environ.get("GX_HARNESS_MODULE_RECEIPT"):
                artifact = study.rows("SELECT sha256 FROM artifacts WHERE artifact_ref=?", result["result_ref"])[0]
                Path(destination).write_text(json.dumps({
                    "model_transport":"scripted native callback", "cpu_execution":"real",
                    "run_id":study.run_id, "job_id":admitted, "node_id":node["node_id"],
                    "left_scope":left.scope, "right_scope":right.scope, "selector_replay_same":True,
                    "sibling_selection_isolated":True, "run_method_active_after":False,
                    "settled_by_fresh_resume":result["status"], "module_selection":proof["module_capability"],
                    "inputs":proof["inputs"], "result_ref":result["result_ref"], "result_sha256":artifact["sha256"]
                }, ensure_ascii=False, indent=2) + "\n")
