"""P0 Job provenance, resource accounting, and real detached-process failure boundaries."""
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from gx_core import budgets
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import BudgetExceeded, SchemaInvalid
from gx_core.jobs import executor
from gx_core.jobs.grid_workflow import observe_grid, submit_grid
from gx_core.jobs.service import JobService
from gx_core.orchestration import seams
from gx_core.p0 import intent, manifest, science
from gx_core.persistence import records, store


def lose_launch_ack(db, request, receipt_file):
    """Read the committed admission using a second connection, then die after the real fork."""
    conn = store.open_store(db)
    launch = executor.launch

    def start(directory):
        reader = store.connect(db)
        try:
            before = manifest.read_manifest(reader, directory.name)
            status = reader.execute("SELECT status FROM jobs WHERE job_id=?", (directory.name,)).fetchone()[0]
        finally:
            reader.close()
        identity = launch(directory)
        Path(receipt_file).write_text(json.dumps({"job_id": directory.name, "status_at_launch": status,
                                                "before": before, "identity": identity}))
        os._exit(73)

    with patch.object(executor, "launch", side_effect=start):
        submit_grid(conn, **request)


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.db = self.directory / "gx.db"
        self.conn = store.open_store(self.db)
        self.addCleanup(self.conn.close)
        self.run, self.budget = self.make_run()
        self.artifacts = ArtifactService(self.conn)
        self.grid = {"fixture_version": "1", "synthetic": True, "method_id": "fixture-grid",
                     "method_version": "1", "delay_seconds": .2,
                     "candidates": [{"candidate_id": "peak", "x": 2}, {"candidate_id": "away", "x": 0}]}
        self.raw = self.artifacts.publish_bytes(run_id=self.run, namespace=f"{self.run}/input",
                   rel_path="grid.json", kind="grid_input", data=ids.canonical_json(self.grid))
        intent.bind_main(self.conn, self.run, "operator-thread")

    def make_run(self):
        with store.transaction(self.conn) as tx:
            budget = budgets.create_root(tx, run_id=None, profile_name="p0", actor="test")
            run = records.create_run(tx, objective="Measure a synthetic numerical grid", project_ref="grid",
                                     workflow_id="science_grid", workflow_version="1", actor="test", budget_ref=budget)
            for state in ("AUDITING", "PLANNING", "ACTIVE"):
                store.transition(tx, machine="run", subject_id=run, to_state=state, actor="test")
        return run, budget

    def change(self, key, kind, value, version=0):
        mid = f"{self.run}-{key}-{version}"
        intent.stage_input(self.conn, self.run, mid, {"kind": "change", "items": [
            {"item_id": key, "item_type": kind, "value": value, "expected_version": version}]})
        return intent.accept_input(self.conn, self.run, "operator-thread", f"turn-{mid}", mid)

    def request(self, name="route-a"):
        frozen = science.freeze_spec(self.conn, self.run, {"threshold": -.25})
        return {"run_id": self.run, "operation_id": name, "input_ref": self.raw.artifact_ref,
                "binding": {"workflow_id": "science_grid", "node_id": "experiment", "path_id": "A",
                            "evaluator": frozen}}

    def evidence(self, name, values):
        target = os.environ.get("GX_P0_JOB_EVIDENCE_DIR")
        if target:
            out = Path(target)
            out.mkdir(parents=True, exist_ok=True)
            (out / (name + ".json")).write_text(json.dumps(values, indent=2, sort_keys=True) + "\n")

    def test_original_launch_manifest_survives_submitter_death(self):
        self.change("source", "data_source", {"artifact_ref": self.raw.artifact_ref})
        intent.bind_dependencies(self.conn, self.run, "workflow", self.run, {"source": "execution"})
        req = self.request()
        receipt_file = self.directory / "launch.json"
        child = multiprocessing.get_context("spawn").Process(target=lose_launch_ack,
                                                            args=(self.db, req, str(receipt_file)))
        child.start()
        child.join(20)
        self.assertEqual(child.exitcode, 73)
        receipt = json.loads(receipt_file.read_text())
        job = receipt["job_id"]
        before = receipt["before"]
        head = before["admission"]
        self.assertEqual(receipt["status_at_launch"], "SUBMITTING")
        self.assertIsNone(before["completion"])
        self.assertEqual(head["owner"]["kind"], "workflow_node")
        self.assertEqual((head["owner"]["node_id"], head["task_id"], head["attempt_id"]),
                         ("experiment", None, None))
        self.assertEqual(head["inputs"][0]["sha256"], self.raw.sha256)
        self.assertEqual(head["evaluator"], req["binding"]["evaluator"])
        self.assertEqual(head["intent_dependencies"]["source"]["version"], 1)
        self.assertTrue(head["source"]["tree_sha256"] and head["runtime"]["observed_dependencies"]["python"])
        begin = time.monotonic()
        with patch.object(executor, "launch", side_effect=AssertionError("attempted a duplicate launch")):
            replay = submit_grid(self.conn, **req)
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["job_id"], job)
            self.assertEqual(JobService(self.conn).wait(job, timeout_seconds=10)["status"], "SUCCEEDED")
            done = observe_grid(self.conn, job)
        after = manifest.read_manifest(self.conn, job)
        self.assertEqual(before["admission_hash"], after["admission_hash"])
        terminal = after["completion"]
        identity = receipt["identity"]
        self.assertEqual(terminal["backend_job_id"], f"runner:{identity['runner_pid']}:{identity['runner_started']}")
        self.assertTrue(terminal["started_at"] and terminal["finished_at"])
        self.assertEqual({s["output_name"] for s in terminal["logs"]}, {"stdout", "stderr"})
        self.assertEqual(terminal["exit_code"], 0)
        proof = manifest.validate_evidence(self.conn, run_id=self.run, job_id=job, artifact_ref=done["observations_ref"])
        self.assertEqual(proof["manifest_hash"], after["manifest_hash"])
        newer_eval = science.freeze_spec(self.conn, self.run, {"threshold": -2})
        replay = submit_grid(self.conn, **{**req, "binding": {**req["binding"], "evaluator": newer_eval}})
        self.assertEqual(replay["job_id"], job)
        self.assertEqual(manifest.read_manifest(self.conn, job), after)
        count = self.conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (self.run,)).fetchone()[0]
        self.assertEqual(count, 1)
        self.evidence("lost-launch-ack", {"expected": "recover the accepted process with its original manifest",
            "exitcode_injected": child.exitcode, "status_before_child_ack": receipt["status_at_launch"],
            "actual": terminal["status"], "receipt": identity, "new_launches_on_recovery": 0,
            "job_count": count, "manual_intervention": False, "recovery_seconds": time.monotonic() - begin,
            "prelaunch": before, "terminal": after, "proof": proof})

    def test_changed_bytes_external_shell_and_current_method_ban(self):
        self.change("methods", "method_ban", {"forbidden_capabilities": []})
        req = self.request()
        job = submit_grid(self.conn, **req)["job_id"]
        self.assertEqual(JobService(self.conn).wait(job, timeout_seconds=10)["status"], "SUCCEEDED")
        ref = observe_grid(self.conn, job)["observations_ref"]
        validate = lambda ref: manifest.validate_evidence(self.conn, run_id=self.run, job_id=job, artifact_ref=ref)
        original_proof = validate(ref)
        # This external process genuinely computes its own output; the operator then imports it.
        shell_file = self.directory / "shell-result.json"
        with shell_file.open("w") as output:
            subprocess.run([sys.executable, "-I", "-c", "import json; print(json.dumps({'score': -(2-2)**2}))"],
                           stdout=output, check=True)
        injected = self.artifacts.publish_bytes(run_id=self.run, job_id=job, namespace=f"{self.run}/import",
            rel_path="result.json", kind="synthetic_grid", origin="JOB_OUTPUT", data=shell_file.read_bytes())
        with self.assertRaises(SchemaInvalid) as external:
            validate(injected.artifact_ref)
        self.assertEqual(external.exception.details["reason"], "artifact_mismatch")
        for damaged_ref in (self.raw.artifact_ref, ref):
            entry = self.artifacts.describe(damaged_ref)
            path = self.artifacts.resolve_path(entry["namespace"], entry["rel_path"])
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            with self.assertRaises(SchemaInvalid) as damaged:
                validate(ref)
            self.assertEqual(damaged.exception.details["reason"], "integrity_mismatch")
            path.write_bytes(original)
        self.change("methods", "method_ban", {"forbidden_capabilities": ["fixture-grid@1"]}, 1)
        with self.assertRaises(SchemaInvalid) as ineligible:
            validate(ref)
        self.assertEqual(ineligible.exception.details["reason"], "current_intent")
        with patch.object(executor, "launch", side_effect=AssertionError("launched prohibited method")):
            with self.assertRaises(SchemaInvalid) as refused:
                submit_grid(self.conn, **{**req, "operation_id": "forbidden"})
            self.assertEqual(refused.exception.details["reason"], "current_intent")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        self.assertTrue(self.artifacts.verify(ref)["ok"])
        self.evidence("hard-constraint-and-external-output", {"job_id": job, "original_proof": original_proof,
            "external_process_exit": 0, "external_import": injected.artifact_ref,
            "external_result_validation": external.exception.details, "changed_bytes_validation": damaged.exception.details,
            "after_change_validation": ineligible.exception.details, "gx_admission": refused.exception.details,
            "history_retained": True, "new_jobs": 0})

    def test_project_caps_share_one_ledger_across_runs(self):
        limits = dict(job_submissions=4, cpu_seconds=80, gpu_jobs=1, gpu_minutes=5,
                      worker_tasks=4, tokens=120, api_calls=2)
        budgets.activate_project_budget(self.conn, "grid", limits, phase_id="p0-test")
        _, second_budget = self.make_run()
        held = budgets.reserve(self.conn, budget_ref=self.budget, subject_kind="integration",
                               subject_id="first", amounts=limits, actor="test")
        for key in ("cpu_seconds", "gpu_minutes", "api_calls", "tokens"):
            with self.subTest(key=key), self.assertRaises(BudgetExceeded):
                budgets.reserve(self.conn, budget_ref=second_budget, subject_kind="integration",
                                subject_id=key, amounts={key: 1}, actor="test")
        actual = dict(cpu_seconds=30, gpu_minutes=3, api_calls=1, tokens=50)
        budgets.settle(self.conn, held["reservation_ref"], actual=actual, actor="test")
        budgets.settle(self.conn, held["reservation_ref"], actual=actual, actor="test")
        self.change("token-ceiling", "resource_limit", {"resource": "tokens", "unit": "reserved_tokens", "max": 55})
        with self.assertRaises(BudgetExceeded) as token:
            budgets.reserve(self.conn, budget_ref=second_budget, subject_kind="integration",
                            subject_id="too-large", amounts={"tokens": 6}, actor="test")
        self.assertEqual(token.exception.details["available"], 5)
        self.change("cpu-ceiling", "resource_limit", {"resource": "cpu_seconds", "max": 59})
        with patch.object(executor, "launch", side_effect=AssertionError("launched unbudgeted process")):
            with self.assertRaises(BudgetExceeded) as cpu:
                submit_grid(self.conn, **self.request("over-budget"))
        self.assertEqual(cpu.exception.details["reason"], "current_intent")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        state = budgets.project_budget_state(self.conn, "grid")
        self.assertEqual(state["spent"]["tokens"], 50)
        self.assertEqual(state["reserved"]["tokens"], 0)
        self.evidence("project-resource-ledger", {"resource_units": list(limits), "project": state,
            "native_token_refusal": token.exception.details, "actual_grid_admission_refusal": cpu.exception.details,
            "actual_gpu_execution": False, "gpu_reservation_checked": True})

    def test_external_change_after_backend_completion_before_gx_publication(self):
        job = submit_grid(self.conn, **self.request())["job_id"]
        outcome = self.directory / "jobs" / job / "outcome.json"
        deadline = time.monotonic() + 10
        while not outcome.is_file() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(outcome.is_file())
        staged = self.conn.execute("SELECT staging_ref FROM jobs WHERE job_id=?", (job,)).fetchone()[0]
        output = self.directory / staged / "observations.json"
        subprocess.run([sys.executable, "-I", "-c",
                        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_bytes(p.read_bytes()+b' ')",
                        str(output)], check=True)
        done = observe_grid(self.conn, job)
        ref = done["observations_ref"]
        # The scientific output and GX publication hash agree; the backend originally produced different bytes.
        self.assertTrue(self.artifacts.verify(ref)["ok"])
        with self.assertRaises(SchemaInvalid) as violation:
            manifest.validate_evidence(self.conn, run_id=self.run, job_id=job, artifact_ref=ref)
        self.assertEqual(violation.exception.details["reason"], "integrity_mismatch")
        self.assertNotEqual(violation.exception.details["runner_sha256"], violation.exception.details["published_sha256"])
        self.evidence("staging-replacement", {"job_id": job, "external_process_exit": 0,
            "historical_artifact_ref": ref, "validation": violation.exception.details,
            "manual_intervention": False, "accepted_scientific_results": 0})

    def test_terminal_cleanup_preserves_native_turn_until_usage(self):
        # Job and model effects have independent completion receipts. Finish the workflow first.
        self.conn.execute("UPDATE budgets SET run_id=? WHERE budget_ref=?", (self.run, self.budget))
        hold = budgets.reserve(self.conn, budget_ref=self.budget, subject_kind="native_turn",
                               subject_id="last-decision", amounts={"api_calls": 1, "tokens": 300}, actor="test")
        unused = budgets.reserve(self.conn, budget_ref=self.budget, subject_kind="task",
                                 subject_id="unused", amounts={"worker_tasks": 1}, actor="test")
        for _ in range(2):
            seams.release_run_holds(self.conn, run_id=self.run, actor="backend", reason="workflow completed")
        state = budgets.state(self.conn, self.budget)
        statuses = {r["reservation_ref"]: r["status"] for r in state["reservations"]}
        self.assertEqual((statuses[hold["reservation_ref"]], statuses[unused["reservation_ref"]]), ("HELD", "RELEASED"))
        self.assertEqual(state["reserved"], {"api_calls": 1, "tokens": 300})
        for _ in range(2):
            budgets.settle(self.conn, hold["reservation_ref"], actual={"api_calls": 1, "tokens": 210},
                           actor="native-usage-receipt")
        result = budgets.state(self.conn, self.budget)
        self.assertEqual(result["spent"], {"api_calls": 1, "tokens": 210})
        self.assertEqual(result["reserved"], {})
        self.evidence("terminal-native-usage", {"reservation": hold["reservation_ref"],
            "state_before_receipt": statuses[hold["reservation_ref"]], "reserved_before_receipt": state["reserved"],
            "after_repeated_receipt": result["spent"], "reserved_after_receipt": result["reserved"],
            "usage_source": "injected delayed official-receipt value at the budget owner"})

    def test_native_metering_requires_enforceable_units(self):
        budgets.require_native_metering(self.conn, self.run)
        self.change("calls", "resource_limit", {"resource": "api_calls", "unit": "gx_calls", "max": 4})
        other_run, _ = self.make_run()
        budgets.require_native_metering(self.conn, other_run)
        rejected = self.change("calls", "resource_limit",
                               {"resource": "api_calls", "unit": "provider_requests", "max": 4}, 1)
        self.assertEqual(rejected["status"], "unsupported")
        self.assertEqual(intent.read_item(self.conn, self.run, "calls")["version"], 1)
        self.assertEqual(intent.read_item(self.conn, self.run, "calls")["value"]["unit"], "gx_calls")
        # Simulate importing an external database that already has an explicit physical cap.
        strict = {"resource": "api_calls", "unit": "provider_requests", "max": 4}
        self.conn.execute("UPDATE p0_intent_versions SET value_json=? WHERE run_id=? AND item_id='calls'",
                          (ids.canonical_json(strict), self.run))
        with self.assertRaises(BudgetExceeded) as denied:
            budgets.require_native_metering(self.conn, other_run)
        self.assertEqual(denied.exception.details["reason"], "native_provider_limit_unenforceable")
        self.assertEqual(denied.exception.details["unit"], "provider_requests")
        self.assertEqual(budgets.state(self.conn, self.budget)["reserved"], {})
        self.evidence("native-provider-hard-limit", {"original_run": self.run, "sibling_run": other_run,
            "production_ingress_rejected": True, "unchanged_authorized_version": 1,
            "imported_explicit_physical_limit": denied.exception.details, "provider_requests_sent": 0})

    def test_intent_tightened_between_admission_and_launch(self):
        self.change("pending-methods", "method_ban", {"forbidden_capabilities": []})
        start = JobService._start

        def change_before_launch(service, row):
            self.change("pending-methods", "method_ban", {"forbidden_capabilities": ["fixture-grid"]}, 1)
            return start(service, row)

        with patch.object(JobService, "_start", change_before_launch), patch.object(
                executor, "launch", side_effect=AssertionError("started a newly prohibited Job")):
            with self.assertRaises(SchemaInvalid) as rejected:
                submit_grid(self.conn, **self.request("blocked-between-checks"))
        job = rejected.exception.details["job_id"]
        record = manifest.read_manifest(self.conn, job)
        self.assertEqual(record["completion"]["status"], "FAILED")
        self.assertIsNone(record["completion"]["started_at"])
        self.assertFalse(record["completion"]["current_at_completion"])
        state = budgets.state(self.conn, self.budget)
        self.assertEqual((state["reserved"], state["spent"]), ({}, {}))
