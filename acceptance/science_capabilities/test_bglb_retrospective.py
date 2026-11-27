"""Published-assay regressions and an ordinary GX CPU Job with deterministic Lead transport."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v62a.interleavings_fixture import PRODUCT, branch, draft
from acceptance.v622.test_close import CloseStudy
from gx_core.artifacts import ArtifactService
from gx_core.p0 import cli, manifest, v621_metrics

EXAMPLE = PRODUCT / "examples/bglb-retrospective"
SCRIPT = PRODUCT / "gx-h/plugins/installed/org.gx.published-kinetics/1.0.0/methods/retrospective.py"
spec = importlib.util.spec_from_file_location("published_bglb", SCRIPT)
method = importlib.util.module_from_spec(spec)
spec.loader.exec_module(method)


class BglBRetrospective(unittest.TestCase):
    def test_fixed_rows_and_site_split_without_S3_outcomes(self):
        kinetics = method.table_rows(EXAMPLE / "data/S1-table.docx", "kinetics")
        scores = method.table_rows(EXAMPLE / "data/S3-table.docx", "conservation")
        rows = method.records(kinetics, scores)
        by_label = {r["variant"]: r for r in rows}
        self.assertEqual(len(by_label), 101)
        self.assertEqual(by_label["BglB"]["kcat_over_KM"]["value"], 176000)
        self.assertEqual(by_label["Q19A"]["kcat"]["status"], "no_reported_fit")
        self.assertEqual(by_label["Q19C"]["kcat_over_KM"]["upper_bound"], 10)
        self.assertIsNone(by_label["Q19C"]["kcat_over_KM"]["value"])
        self.assertEqual(by_label["W34A"]["kcat_over_KM"]["status"], "no_quantifiable_soluble_yield")
        self.assertEqual(by_label["H119N"]["kcat"]["uncertainty"]["relation"], "<")
        held = {r["position"] for r in rows if r["partition"] == "holdout"}
        dev = {r["position"] for r in rows if r["partition"] == "development"}
        self.assertFalse(held & dev)
        replacement = [scores[0]] + [["hidden", *row[1:]] for row in reversed(scores[1:])]
        self.assertEqual(method.evaluate(rows), method.evaluate(method.records(kinetics, replacement)))
        self.assertIsNone(method.correlation([4, 4, 4], [0, 3, 8]))
        self.assertAlmostEqual(method.correlation([1, 2, 2, 4], [4, 2, 2, 1]), -1)

    def test_mismatched_material_and_no_score_are_not_numerical_results(self):
        with self.assertRaisesRegex(ValueError, "original published kinetics table"):
            method.table_rows(EXAMPLE / "data/S3-table.docx", "kinetics")
        kinetics = method.table_rows(EXAMPLE / "data/S1-table.docx", "kinetics")
        scores = method.table_rows(EXAMPLE / "data/S3-table.docx", "conservation")
        output = method.evaluate(method.records(kinetics, scores[:1]))
        self.assertEqual(output["holdout_exact"], 30)
        self.assertEqual(output["holdout_score_pairs"], 0)
        self.assertIsNone(output["holdout_spearman"])
        self.assertIsNone(output["paired_wt_median_absolute_log10_error"])

    def test_research_entry_runs_real_cpu_job_with_original_materials(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            with patch.object(cli, "run_until_stopped", side_effect=lambda conn, args, rid, timeout: cli.snapshot(conn, rid)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["research", "--state", str(work / "state"), "--run-key", "bglb-acceptance",
                                           "--goal-file", str(EXAMPLE / "question.md"), "--goal-config", str(EXAMPLE / "goal.json")]), 0)
            with CloseStudy(work, reopen=True) as s:
                s.next_actor("run")
                study = branch("historical")
                study["budget_limits"]["cpu_seconds"] = 60
                op = s.node(s.lead, "score", s.materials[0], branch_id="local:historical", capability=method.METHOD)
                op.update(inputs=list(reversed(s.materials)), question="Does the fixed conservation rank agree with held-out pNPG efficiency?")
                decision = draft(study, op, evidence=s.materials, watches=[{"event":"job_settled", "subject_ref":"local:score"}])
                adopted = s.call(s.lead, "gx_decide_revision", {"decision":decision}, "run-audit")
                result = s.settle(adopted["aliases"]["local:score"])
                receipt = manifest.read_manifest(s.conn, result["job_id"])["completion"]
                self.assertEqual((receipt["status"], receipt["exit_code"]), ("SUCCEEDED", 0))
                asset = ArtifactService(s.conn)
                outputs = {o["output_name"]: o for o in receipt["outputs"]}
                self.assertEqual(set(outputs), {"summary.json", "records.json"})
                for output in outputs.values():
                    self.assertEqual({item["role"]: item["sha256"] for item in output["source_inputs"]}, method.TABLES)
                summary = asset.read_json(outputs["summary.json"]["artifact_ref"])
                self.assertEqual((summary["holdout_count"], summary["holdout_exact"], summary["holdout_score_pairs"]), (41, 30, 14))
                self.assertAlmostEqual(summary["holdout_spearman"], 0.2596261197)
                self.assertEqual(summary["overall_efficiency_status"], {"quantified":78, "below_reported_bound":8, "no_quantifiable_soluble_yield":14})
                metrics = {m["metric_id"] for m in v621_metrics.from_output(s.conn, s.run_id, outputs["summary.json"]["artifact_ref"])}
                self.assertIn("holdout_spearman", metrics)
