"""Verify a normal archive Job can be selected from a question without a PI query file."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v622.test_close import SurveyedStudy
from acceptance.v62a.interleavings_fixture import draft
from gx_core.artifacts import ArtifactService
from gx_core.errors import GxError
from gx_core.p0 import cli, manifest
from tests.test_v6_decisions import branch

METHOD = "yeast-archive-lookup@2"
PROVIDER = "org.gx.yeast-enzyme-archive@1.1.0"


class LiteralArchive(unittest.TestCase):
    def test_real_job_without_query_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            grant = {"project": "literal-archive-control", "research_effort": "medium",
                     "research_modules": [], "plugins": [PROVIDER], "capabilities": [METHOD]}
            (work/"inputs.json").write_text(json.dumps(grant))
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output), patch.object(
                    cli, "run_until_stopped", side_effect=lambda conn, _, run, timeout: cli.snapshot(conn, run)):
                code = cli.main(["research", "--state", str(work/"state"), "--run-key", "literal",
                                 "--goal", "What records support the archived sugar annotation?",
                                 "--goal-config", str(work/"inputs.json")])
            self.assertEqual(code, 0, output.getvalue())
            with SurveyedStudy(work, reopen=True) as study:
                lead = study.next_actor("run")
                fund = branch("archive", jobs=1)
                fund["budget_limits"]["cpu_seconds"] = 60

                def decision(taxon, substrate):
                    node = {"op": "add_node", "node": "local:lookup", "branch": fund["branch"],
                            "capability": METHOD, "inputs": [], "parameters": {"taxid": taxon, "substrate": substrate},
                            "depends_on": [], "question": "What does the source archive record?"}
                    watch = {"event":"job_settled", "subject_ref": node["node"]}
                    return {"decision": draft(fund, node, watches=[watch])}

                for tax, sugar in ((1, "maltose"), (559292, "out-of-panel")):
                    with self.assertRaises((GxError, ValueError)):
                        study.call(lead, "gx_decide_revision", decision(tax, sugar), f"bad-{tax}")
                reply = study.call(lead, "gx_decide_revision", decision(559292, "lactose"), "inside")
                settled = study.settle(reply["aliases"]["local:lookup"])
                completion = manifest.read_manifest(study.conn, settled["job_id"])["completion"]
                self.assertEqual(len(completion["outputs"]), 1)
                result = ArtifactService(study.conn).read_json(completion["outputs"][0]["artifact_ref"])
                self.assertEqual(result["counts"]["exact_accessions"], 0)
                self.assertEqual(result["query"]["taxid"], 559292)
                self.assertEqual(result["query"]["substrate"], "lactose")
                self.assertEqual(len(result["source_digests"]), 4)

    def test_missing_snapshot_fails_without_scientific_output(self):
        import subprocess
        import sys

        plugins = Path(__file__).resolve().parents[3]/"gx-h/plugins/installed/org.gx.yeast-enzyme-archive"
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)/"output"
            output.mkdir()
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(plugins/"1.1.0/job-scripts/lookup.py"),
                 "--reader", str(plugins/"1.0.0/archive.py"),
                 "--mount-member", str(Path(temp)/"missing/a/b/records"),
                 "--taxid", "559292", "--substrate", "lactose", "--out", str(output)],
                text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("FileNotFoundError", result.stderr)
            self.assertFalse((output/"parent-records.json").exists())
