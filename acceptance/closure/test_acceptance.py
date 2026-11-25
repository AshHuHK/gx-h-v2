"""Public CLI acceptance for provenance, archive verification and immutable retries."""
import gzip
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from acceptance.closure import archive
from acceptance.closure.cases import GATE_OPERATIONS, HARDENING, MATRIX, Step, SUITES, observed


class Automation(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="gx-acceptance-cli-case-")
        self.addCleanup(temp.cleanup)
        self.runs = Path(temp.name)

    def command(self, *args, expected=0):
        argv = [str(ROOT / "bin/gx-acceptance"), *map(str, args), "--runs-dir", str(self.runs)]
        result = subprocess.run(argv, cwd=self.runs, text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def branch(self, run_id):
        self.command("run", "phase2a", "--case", "P2A-08", "--run-id", run_id, "--python", sys.executable)
        return self.runs / run_id

    def test_ac01(self):
        run = self.branch("branch")
        report = json.loads(self.command("report", "branch", "--json").stdout)
        self.assertEqual(report["cases"]["P2A-08"]["status"], "PASS")
        [command] = archive.read(run / "commands.json")
        self.assertEqual(command["returncode"], 0)
        self.assertTrue(command["executed"])
        self.assertIn(str(ROOT / "acceptance/phase2a/branch_failure.py"), command["argv"])
        self.assertEqual(command["cwd"], str(ROOT))
        self.assertEqual(command["classes"], ["SCRIPTED"])
        env = archive.read(run / "environment.json")
        self.assertRegex(env["sha"], r"^[a-f0-9]{40}$")
        self.assertIn("dirty_state", env)
        self.assertTrue(env["platform"] and env["test_interpreter"]["sqlite"])
        self.assertEqual(len(set(MATRIX)), 35)
        self.assertEqual(set(GATE_OPERATIONS), {"P2A-09", "P2A-10", "P2A-11", "P2A-12", "P2A-13", "REG01"})
        print(json.dumps({"observed_run": run.name, "case": "P2A-08", "verified_fields":
              ["git", "environment", "argv", "cwd", "exit", "classification", "report"]}))

    def test_ac02(self):
        run = self.branch("archive")
        unpacked = self.runs / "verified-evidence"
        self.command("verify", "archive", "--extract-to", unpacked)
        receipts = unpacked / "branch-failure/evidence"
        snapshot = self.runs / "unpacked-state.db"
        snapshot.write_bytes(gzip.decompress((receipts / "after_retry.sqlite.gz").read_bytes()))
        db = sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
        events = archive.read(receipts / "events.json")
        self.assertGreater(len(events), 0)
        self.assertIn("PASS", (unpacked / "branch-failure/console.log").read_text())
        original_digest = archive.sha256(run / "raw.tar.gz")
        # Change an inner digest and update the outer manifest to test *decompressed* verification.
        raw_manifest = archive.read_manifest(run / "raw-files.sha256")
        raw_manifest["branch-failure/console.log"] = "0" * 64
        archive.write_manifest(run / "raw-files.sha256", raw_manifest)
        outer = archive.read_manifest(run / "manifest.sha256")
        outer["raw-files.sha256"] = archive.sha256(run / "raw-files.sha256")
        archive.write_manifest(run / "manifest.sha256", outer)
        invalid = self.command("verify", "archive", expected=2)
        self.assertIn("checksum mismatch: raw/branch-failure/console.log", invalid.stderr)
        print(json.dumps({"original_archive_sha256": original_digest, "unpacked_events": len(events),
                          "sqlite_integrity": "ok", "tamper_exit": invalid.returncode}))

    def test_ac03(self):
        # One missing official host input and a successful, independent branch provoke a mixed run.
        self.command("run", "phase2a", "--case", "P2A-01", "--case", "P2A-08", "--run-id", "fault",
                     "--python", sys.executable, "--binary", self.runs / "absent-host", expected=2)
        first = self.runs / "fault"
        original = {p.name: archive.sha256(p) for p in first.iterdir()}
        initial = archive.read(first / "summary.json")
        self.assertEqual({case: rec["status"] for case, rec in initial["cases"].items()},
                         {"P2A-08": "PASS", "P2A-01": "BLOCKED"})
        self.command("run", "phase2a", "--failed-only", "fault", "--run-id", "retry", expected=2)
        retry = archive.read(self.runs / "retry/summary.json")
        self.assertEqual(set(retry["cases"]), {"P2A-01"})
        self.assertEqual(retry["parent"], {"run_id": "fault", "manifest_sha256": original["manifest.sha256"]})
        self.assertEqual(archive.read(self.runs / "retry/environment.json")["paths"]["binary"],
                         str(self.runs / "absent-host"))
        self.command("run", "phase2a", "--case", "P2A-08", "--run-id", "fault", expected=2)
        self.assertEqual(original, {p.name: archive.sha256(p) for p in first.iterdir()})
        self.command("verify", "fault")
        self.command("verify", "retry")
        self.branch("healthy")
        self.command("run", "phase2a", "--failed-only", "healthy", "--run-id", "nothing")
        no_op = archive.read(self.runs / "nothing/summary.json")
        self.assertEqual((no_op["status"], no_op["cases"]), ("NO_FAILURES", {}))
        print(json.dumps({"first_manifest": original["manifest.sha256"], "retry_cases": list(retry["cases"]),
                          "overwrite_rejected": True, "clean_retry": no_op["status"]}))

    def test_hardening_and_original_have_separate_case_selection(self):
        self.assertEqual(len(set(SUITES["all"])), 38)
        self.assertEqual(len(set(SUITES["hardening"])), 13)
        self.assertIn("T03", HARDENING)
        result = self.command("run", "hardening", "--case", "P2A-08", expected=2)
        self.assertIn("case is outside this suite", result.stderr)

    def test_v6_suite_dispatches_every_selected_case(self):
        from types import SimpleNamespace
        from acceptance.closure.cases import plan
        args = SimpleNamespace(suite="v6", python=Path(sys.executable), binary=Path("/missing"), cache=self.runs,
                               source_db=self.runs, historical_briefs=self.runs, catalog=self.runs,
                               original_failures=None, dependency_control_work=self.runs, seed_python=Path(sys.executable),
                               node=None)
        selected = SUITES[args.suite]
        steps = plan(args, selected, self.runs, "HEAD")
        self.assertEqual(set(selected), {case for step in steps for case in step.ids})
        self.assertTrue(all(step.argv and step.receipt for step in steps))
        self.assertEqual(next(step for step in steps if step.name == "v6-loopback").classes,
                         ("SCRIPTED", "OFFICIAL_HOST_LOOPBACK", "REAL_COMPUTE"))
        self.assertNotIn("V6-LOOPBACK", SUITES["all"])
        for step in steps:
            if step.name.endswith("-export"):
                self.assertEqual(step.classes, ("EXPORT_CONSISTENCY",))
        step = next(step for step in steps if step.name == "v6-decision")
        log = self.runs / step.receipt
        log.write_text("Ran 2 tests in 0.01s\nOK (skipped=2)\n")
        self.assertEqual(observed(step, self.runs, 0)[step.ids[0]]["status"], "FAIL")

    def test_reg01_exposes_unsettled_child_even_after_successful_wrapper_exit(self):
        step = Step("v31-regression", ("REG01",), [], ("OFFICIAL_HOST_LOOPBACK",), "execution.json", "regression")
        operations = {name: {"returncode": 0} for name in GATE_OPERATIONS["REG01"]}
        operations["extra-child-observation"] = {"returncode": 0}
        archive.save(self.runs / step.receipt, {"operations": operations})
        child = self.runs / "extra-child/observation.json"
        self.assertEqual(observed(step, self.runs, 0)["REG01"]["status"], "FAIL")
        child.parent.mkdir()
        archive.save(child, {"observed_status": "COLD_SETTLEMENT_TIMEOUT", "returncode": 1})
        result = observed(step, self.runs, 0)["REG01"]
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["supplemental_child_observation"]["returncode"], 1)
        archive.save(child, {"observed_status": "PASS", "returncode": 0})
        self.assertEqual(observed(step, self.runs, 0)["REG01"]["status"], "PASS")

    def test_focused_checks_require_their_observed_artifacts(self):
        step = Step("timeout", ("TIMEOUT",), [], ("SCRIPTED",), "console.log", "unittest",
                    artifacts=("timeout.json",))
        log = self.runs / "console.log"
        log.write_text("Ran 1 test in 0.25s\n\nOK\n")
        self.assertEqual(observed(step, self.runs, 0)["TIMEOUT"]["status"], "FAIL")
        archive.save(self.runs / "timeout.json", {"wall_limit": {"result": "timed_out"}})
        log.write_text("Ran 1 test in 0.25s\n\nOK (skipped=1)\n")
        self.assertEqual(observed(step, self.runs, 0)["TIMEOUT"]["status"], "FAIL")
        log.write_text("Ran 1 test in 0.25s\n\nOK\n")
        self.assertEqual(observed(step, self.runs, 0)["TIMEOUT"]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
