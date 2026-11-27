"""A scripted native transport, the normal Goal entry, two real detached CPU Jobs, and a new method view."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v62a import interleavings_fixture as fx
from gx_core import config
from gx_core.artifacts import ArtifactService
from gx_core.capabilities import manifest
from gx_core.errors import GxError
from gx_core.p0 import capability_views, cli, v6_goal, v62_outputs
from gx_core.plugins import development
from gx_core.plugins.catalog import package_digest
from gx_core.plugins.cli import _install

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/record-method-authoring"
BUILDER = "derive-record-coverage@1"
PARAMS = {"source_quote": "an asynchronous job API service to map the identifiers of one database to another",
          "cohort_rows": "candidates", "cohort_id": "accession", "evidence_rows": "pointers",
          "evidence_id": "entry", "evidence_value": "pmid"}


class DerivedMethod(unittest.TestCase):
    def test_authored_method_reaches_same_goal_job_and_cited_decision(self):
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        release = work / "release/gx-h"
        shutil.copytree(config.HARNESS_ROOT, release, ignore=shutil.ignore_patterns("var", "__pycache__", ".venv", "tests"))
        self.enterContext(patch.object(config, "HARNESS_ROOT", release))
        self.enterContext(patch.object(config, "CONFIG_DIR", release / "config"))
        self.enterContext(patch.object(manifest, "HARNESS_ROOT", release))
        pkg = ROOT / "gx-h/plugins/installed/org.gx.record-method-builder/1.0.0"
        _install(pkg, package_digest(pkg), release / "plugins/installed", config.CONFIG_DIR / "workflow_packages.json")
        state = work / "study/state"
        grant = json.loads((EXAMPLE / "trehalose/goal.json").read_bytes())
        for item in grant["input_files"]:
            item["path"] = str((EXAMPLE / "trehalose" / item["path"]).resolve())
        grant.pop("budget_profile", None)
        grant["max_parallel_roots"] = 5
        grant.update(workspace_budget={"job_submissions":10,"api_calls":10,"tokens":100000,"cpu_seconds":240,
                                      "worker_tasks":24,"gpu_jobs":0,"gpu_minutes":0,"wall_hours":1})
        grant["run_budget"] = grant["workspace_budget"]
        cfg = work / "grant.json"
        cfg.write_text(json.dumps(grant))
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, cli.main(["start", "--state", str(state), "--run-key", "develop-record-method",
                        "--goal", (EXAMPLE / "trehalose/question.md").read_text(), "--goal-config", str(cfg)]), output.getvalue())
        s = self.enterContext(fx.Scenario(state.parent, reopen=True))
        actor = s.next_actor("run")
        # Use material roles; presentation order is independent of argv order.
        files = json.loads(s.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (s.run_id,)).fetchone()[0])["files"]
        inputs = {f["role"]: f["artifact_ref"] for f in files}
        route = fx.branch("records")
        route.update(budget_limits={"job_submissions": 2, "cpu_seconds": 60})
        node = s.node(actor, "derive", inputs["method_source"], branch_id="local:records",
                      capability=BUILDER, parameters=PARAMS)
        adopted = s.call(actor, "gx_decide_revision", {"decision": fx.draft(route, node,
                    evidence=[inputs["method_source"]], watches=[{"event":"job_settled","subject_ref":"local:derive"}])}, "develop")
        first = s.settle(adopted["aliases"]["local:derive"])
        spec_ref = v62_outputs.published_outputs(s.conn, s.run_id, first["job_id"])[0]["artifact_ref"]
        actor = s.next_actor("run")
        install_args = {"build_ref": spec_ref, "expected_revision": 0}
        installed = s.call(actor, "gx_install_record_method", install_args, "install")
        self.assertEqual(installed, s.call(actor, "gx_install_record_method", install_args, "install"))
        method = installed["capability_version"]
        self.assertEqual(1, capability_views.current(s.conn, s.run_id)["revision"])
        self.assertEqual(method, development.skills(s.conn, s.run_id)[0]["reference"])
        # Freeze a new brief after the view changed, and commission the developed method there.
        s.call(actor, "gx_decide_revision", {"decision": fx.draft(evidence=[spec_ref])}, "read-build")
        actor = s.next_actor("run")
        measured = s.node(actor, "audit", inputs["cohort"], branch_id=adopted["aliases"]["local:records"], capability=method)
        measured["inputs"].append(inputs["evidence"])
        dispatched = s.call(actor, "gx_decide_revision", {"decision": fx.draft(measured, evidence=[inputs["cohort"]],
                     watches=[{"event":"job_settled","subject_ref":"local:audit"}])}, "audit")
        job = s.settle(dispatched["aliases"]["local:audit"])
        published = v62_outputs.published_outputs(s.conn, s.run_id, job["job_id"])[0]["artifact_ref"]
        result = ArtifactService(s.conn).read_json(published)
        self.assertEqual((result["numerator"], result["denominator"], result["matched_without_value"]), (2, 3, ["P35172"]))
        actor = s.next_actor("run")
        read = s.call(actor, "gx_read_artifact", {"ref": published}, "read-coverage")
        self.assertEqual(read["denominator"], 3)
        decision = fx.draft(evidence=[published])
        decision["rationale"] = "Prioritize the two entries with paper pointers for source review; the other entry remains unclassified."
        accepted = s.call(actor, "gx_decide_revision", {"decision": decision}, "cite-readout")
        proof = dict(run_id=s.run_id, provider="scripted_native_transport", model_calls=0, source=installed["source"],
                     method=method, plugin=installed["plugin"], builder_job=first["job_id"], build_ref=spec_ref,
                     execution_job=job["job_id"], measurement_ref=published, reading=result,
                     cited_decision_ref=accepted["decision_ref"], calibration=installed["calibration"])
        if path := os.environ.get("GX_RECORD_METHOD_RECEIPT"):
            Path(path).write_text(json.dumps(proof, indent=2, ensure_ascii=False) + "\n")
