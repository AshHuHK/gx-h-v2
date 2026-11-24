"""Provenance controls for T03 intake. Synthetic records here do not adjudicate historical cases."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from acceptance.closure.profile_cases import original_failures, retain_original_file, supporting_files


class HistoricalIntake(unittest.TestCase):
    def test_preserves_cited_bytes_and_rejects_changed_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            out = directory / "out"
            out.mkdir()
            log = directory / "failed.log"
            log.write_text("ERROR: test_resource (test_example.Probe.test_resource)\n")
            report = directory / "classification.json"
            record = {"test_id": "test_example.Probe.test_resource", "classification": "environment",
                      "reason": "reference trace", "evidence": log.name}
            report.write_text(json.dumps({"failures": [record]}))
            _, _, finding = original_failures(report, output=out)[record["test_id"]]
            self.assertEqual((out / finding["archived_evidence"]).read_bytes(), log.read_bytes())
            digest = finding["evidence_sha256"]
            record["evidence_sha256"] = digest
            report.write_text(json.dumps({"failures": [record]}))
            log.write_text("different run\n")
            _, _, disputed = original_failures(report, output=out)[record["test_id"]]
            self.assertEqual(disputed["classification"], "unresolved")
            self.assertIn("hash mismatch", disputed["reason"])
            self.assertEqual(len(list((out / "original-evidence").iterdir())), 1)

    def test_log_from_different_run_cannot_support_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            out = directory / "out"
            out.mkdir()
            source = directory / "classification.json"
            evidence = directory / "trace.log"
            evidence.write_text("ERROR: test_idle (test_example.Probe.test_idle)\n")
            environment = directory / "environment.json"
            environment.write_text('{"python": "3.13"}')
            item = {"test_id": "test_example.Probe.test_resource", "classification": "fixture",
                    "reason": "test assertion", "evidence": evidence.name}
            source.write_text(json.dumps({"failures": [item], "original_run_log": evidence.name,
                                          "environment_file": environment.name}))
            submitted = original_failures(source)
            with self.assertRaisesRegex(ValueError, "identities differ"):
                supporting_files(source, source.read_text(), out, submitted)
            evidence.write_text("ERROR: test_resource (test_example.Probe.test_resource)\n")
            files = supporting_files(source, source.read_text(), out, submitted)
            self.assertEqual((out / files["environment_file"]["archived_evidence"]).read_bytes(),
                             environment.read_bytes())

    def test_implicit_full_log_keeps_distinct_subtest_tracebacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            out = source / "out"
            out.mkdir()
            log = source / "original.log"
            case = "test_example.Probe.test_mode"
            records = []
            sections = []
            for mode in ("cold", "warm"):
                subtest = f" (mode='{mode}')"
                sections.append(f"ERROR: test_mode ({case}){subtest}\nRuntimeError: {mode}")
                records.append({"original_id": f"ERROR: {case}{subtest}", "test_id": case,
                                "original_error": f"RuntimeError: {mode}", "classification": "environment",
                                "reason": "original execution", "evidence": log.name})
            log.write_text("\n======\n".join(sections))
            bundle = source / "review.json"
            bundle.write_text(json.dumps({"failures": records}))
            rows = original_failures(bundle, output=out)
            self.assertIn("original_run_log", supporting_files(bundle, bundle.read_text(), out, rows))
            self.assertEqual(len({r["matched_trace_sha256"] for _, _, r in rows.values()}), 2)
            records[0]["original_error"] = "RuntimeError: warm"
            bundle.write_text(json.dumps({"failures": records}))
            with self.assertRaisesRegex(ValueError, "traceback or heading differs"):
                supporting_files(bundle, bundle.read_text(), out, original_failures(bundle, output=out))

    def test_external_reference_is_not_added_to_archived_user_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "review"
            source.mkdir()
            (directory / "unrelated.log").write_text("unrelated")
            with self.assertRaisesRegex(ValueError, "outside"):
                retain_original_file(source, "../unrelated.log", source)


if __name__ == "__main__":
    unittest.main()
