"""Exercise policy selection and replay labelling at the published CLI and native boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import Scenario, draft
from gx_core.p0 import v621_policy, v62_native

ROOT = Path(__file__).resolve().parents[2]


class GoalPolicy(unittest.TestCase):
    def test_formal_admission_downgrade_rejected_and_replay_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = base / "input.json"
            state = base / "state"
            command = [sys.executable, "-B", str(ROOT / "bin/gx-p0"), "start", "--state", str(state),
                       "--goal", "What do the supplied observations establish?", "--run-key", "historical",
                       "--goal-config", str(config)]
            env = {**os.environ, "GX_CODEX_OFFLINE": "1"}
            for version in (v621_policy.LEGACY, v621_policy.PREVIOUS):
                config.write_text(json.dumps({"evidence_policy": version, "research_effort": "quick"}))
                denied = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn("historical replay", denied.stderr)
            replay = subprocess.run([*command, "--historical-replay"], env=env, capture_output=True,
                                    check=True, text=True, timeout=20)
            receipt = json.loads(replay.stdout)
            self.assertEqual((receipt["evidence_policy"], receipt["study_class"]),
                             (v621_policy.PREVIOUS, "historical_replay"))
            recovered = subprocess.check_output([sys.executable, "-B", str(ROOT / "bin/gx-p0"), "status",
                                               "--state", str(state), "--run-id", receipt["run_id"]], env=env)
            self.assertEqual(json.loads(recovered)["study_class"], "historical_replay")

    def test_historical_publication_is_labelled_at_adoption(self):
        with tempfile.TemporaryDirectory() as directory, Scenario(
                Path(directory)/"replay", spec_updates={"historical_replay": True,
                "evidence_policy": v621_policy.PREVIOUS, "research_effort": "deep"}) as s:
            claim = {"statement": "The available evidence permits further investigation.", "criteria": "Inspect the archive.",
                     "scope": "This deposit", "limitations": "The observation is pending.", "level": "open",
                     "rationale": "There is a specific unresolved question.", "evidence_refs": [s.materials[0]]}
            proposal = s.call(s.lead, "gx_stage_claim", claim, "stage")
            decision = draft(evidence=[s.materials[0], proposal["claim_ref"]])
            s.call(s.lead, "gx_decide_revision", {"decision": decision}, "publish")
            row = s.conn.execute("SELECT document_json FROM hypothesis_revisions").fetchone()
            self.assertEqual(json.loads(row[0])["study_class"], "historical_replay")

    def test_current_and_historical_native_forms_are_compatible(self):
        for version in (v621_policy.VERSION, v621_policy.PREVIOUS, v621_policy.LEGACY):
            form = v62_native.evidence_tool("gx_submit_module_report", version)["inputSchema"]["properties"]
            self.assertEqual("gap_criteria" in form, version in v621_policy.GOVERNED)
            self.assertEqual("qualitative_notes" in form, version == v621_policy.VERSION)
            self.assertEqual("metric_refs" in form, version == v621_policy.VERSION)
            props = v62_native.evidence_tool("gx_stage_claim", version)["inputSchema"]["properties"]
            self.assertEqual("parameters" in props["prediction"]["properties"]["method"]["properties"],
                             version == v621_policy.VERSION)
