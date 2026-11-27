"""Separate source and coordinate labels from numerical conclusions in a researcher's PCNA report."""
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.multi_workflow.test_pcna_native import INTERFACE
from acceptance.v62a.interleavings_fixture import PRODUCT, draft
from acceptance.v622.test_close import CloseStudy
from acceptance.v623 import test_close_metrics
from gx_core.artifacts import ArtifactService
from gx_core.errors import GxError
from gx_core.p0 import cli, manifest


class PCNAReportReferences(unittest.TestCase):
    def prepare(self, stack):
        card = PRODUCT / "examples/pcna-conversation/goal.json"
        config = json.loads(card.read_bytes())
        for item in config["input_files"]:
            for field in ("path", "acquisition"):
                item[field] = str((card.parent / item[field]).resolve())
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        grant = directory / "grant.json"
        grant.write_text(json.dumps(config))
        with patch.object(cli, "run_until_stopped", side_effect=lambda conn, args, rid, timeout: cli.snapshot(conn, rid)), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["research", "--state", str(directory / "state"), "--run-key", "cited-inputs",
                "--goal", "Inspect the deposited interfaces and their applicability.", "--goal-config", str(grant)]), 0)
        s = stack.enter_context(CloseStudy(directory, reopen=True))
        s.next_actor("run")
        args = s.open_args("source-audit", inputs=s.materials, caps=[INTERFACE], limits={"cpu_seconds": 400})
        args.update(module_id="investigation", focus="evaluation")
        module = s.call(s.lead, "gx_open_module", args, "commission-both-originals")
        worker = s.bind(module["task_id"])
        s.call(s.lead, "gx_decide_revision", {"decision": draft()}, "wait-for-report")
        planned = s.submit_node(worker, "first-interface", s.materials[0], capability=INTERFACE,
                                parameters={"pair": "12", "block_index": -1})
        job = s.settle(planned["aliases"]["local:first-interface"])
        worker = s.next_actor(module["scope"])
        summary = job["result_ref"]
        outputs = manifest.read_manifest(s.conn, job["job_id"])["completion"]["outputs"]
        table, = [row["artifact_ref"] for row in outputs if row["output_name"] == "interface-table.json"]
        page = test_close_metrics.EndpointClose.read_page(self, s, worker, summary, 0, "verified-summary-page")
        self.assertTrue(any(row["id"] == "n_contact_residue_pairs" for row in page["endpoint_rows"]))
        for index, ref in enumerate([*s.materials, table]):
            s.call(worker, "gx_read_artifact", {"ref": ref, "operation_key": f"read-{index}"}, f"read-{index}")
        assets = ArtifactService(s.conn)
        self.assertIn("n_contact_residue_pairs", assets.read_json(table)["pairs"]["12"]["whole"])
        return s, assets, worker, summary, table

    def test_accept_bilingual_scope_signed_parameter_and_existing_json_field(self):
        with ExitStack() as stack:
            s, assets, worker, summary, table = self.prepare(stack)
            original = s.materials[0]
            scope = "The original was measured using model 1, entity 1（模型 1、实体 1）。"
            step = "Inspect block_index=-1 and /pairs/12/whole/n_contact_residue_pairs before pooling."
            receipt = s.report(worker, summary, key="publish-grounded-labels", artifact_refs=[original, summary, table],
                               findings=[{"text": scope, "source_refs": [original]}], next_steps=[step])
            report = assets.read_json(receipt["report_ref"])
            self.assertEqual(report["next_steps"], [step])
            self.assertTrue(any(f["text"] == scope for f in report["findings"]))
            self.assertTrue(any(m["metric_id"] == "n_contact_residue_pairs" for m in report["verified_metrics"]))

    def test_reject_unmatched_original_wrong_parameter_and_nonexistent_json_field(self):
        cases = ("uncorrelated-original", "wrong-model", "wrong-parameter", "missing-pointer", "bare-fraction")
        for case in cases:
            with self.subTest(case=case), ExitStack() as stack:
                s, _, worker, summary, table = self.prepare(stack)
                refs = [s.materials[0]]
                field = "/findings/0/text"
                if case == "uncorrelated-original":
                    text = "The selected specimen is 模型 1、实体 1."
                    refs = [s.materials[1]]
                elif case == "wrong-model":
                    text = "The recorded coordinate selection used model 2."
                elif case == "wrong-parameter":
                    text = "Inspect block_index=-2 with registered {{metric:n_contact_residue_pairs}}."
                    refs = [summary]
                elif case == "missing-pointer":
                    text = "Inspect /pairs/12/whole/missing_field."
                    refs = [table]
                else:
                    text = "The deposited model 1 establishes a ratio of 5/6."
                # Supplying all the accessible outputs elsewhere in the report cannot authorize
                # a label or path outside this finding's own source citations.
                with self.assertRaises(GxError) as rejection:
                    s.report(worker, summary, key="unfounded-" + case,
                             artifact_refs=[*s.materials, summary, table],
                             findings=[{"text": text, "source_refs": refs}])
                self.assertEqual(rejection.exception.details.get("pointer"), field)
                self.assertEqual(s.rows("SELECT 1 FROM v62_module_reports WHERE run_id=?", s.run_id), [])


if __name__ == "__main__":
    unittest.main()
