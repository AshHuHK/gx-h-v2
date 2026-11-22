"""Exercise a real local project driver with synthetic host receipts and controlled launches."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from gx_core import contracts, project_continuation as driver, project_messages
from gx_core.native_release import native_state
from gx_core.persistence import store


class ProjectDriverFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Exercise the actual release binaries without the possibility of a paid
        # model call if a no-launch assertion regresses in a child process.
        bin_dir = Path(tmp.name) / "bin"
        bin_dir.mkdir()
        codex = bin_dir / "codex"
        codex.write_text('#!/bin/sh\ntouch "${0%/*}/unexpected-native-start"\nexit 97\n')
        codex.chmod(0o700)
        env = patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(lambda: self.assertFalse((bin_dir / "unexpected-native-start").exists()))
        self.state = Path(tmp.name) / "project"
        self.project = "river-level-statistics"
        self.prompt = Path(tmp.name) / "question.md"
        self.prompt.write_text("Compare independent hourly river level forecasts and review their differences.")
        with native_state(self.state) as state, closing(store.open_store(state.db_path)):
            pass
        self.started = datetime.now(timezone.utc) - timedelta(seconds=2)
        self.launches = 0

    def run_project(self, **kwargs):
        return driver.run_project(self.state, self.project, **kwargs)

    def receipt(self, label, *, reason=None, actions=(), run_statuses=None, unknown=False, pending=False):
        at = self.started + timedelta(milliseconds=self.launches)
        self.launches += 1
        values = {
            "native-session.json": {"project_ref": self.project, "root_thread_id": "original", "session_id": "original",
                                    "uncertain": unknown, "cleanup_pending": False, "pending_effects": ["write"] if pending else []},
            f"launch-{label}.json": {"launch_id": label, "at": at.isoformat()},
            f"result-{label}.json": {"status": "OUTCOME_UNKNOWN" if unknown else "COMPLETED",
                "failure": {"type": "AppServerTimeout"} if unknown else None, "root_thread_id": "original",
                "session_id": "original", "resumed": self.launches > 1, "root_turn_status": None if unknown else "completed",
                "recovery_observed": False, "pending_requests": [], "unresolved_previous_effects": [],
                "recovery_only": False, "cleanup_failure": None,
                "project_continuation": {"reason": reason, "actions": list(actions)} if reason else None}}
        if run_statuses is not None:
            values[f"result-{label}.json"]["project_continuation"]["run_statuses"] = dict(run_statuses)
        for name, value in values.items():
            (self.state / name).write_text(json.dumps(value))

    @staticmethod
    def view(status, actions=()):
        return {"status": status, "actions": list(actions), "run_ids": ["river"], "run_statuses": {"river": "COMPLETED" if status == "TERMINAL" else status},
                "anchor_run_id": "river"}


class ProjectContinuation(ProjectDriverFixture):
    def test_committed_child_acquire_wakes_only_the_original_lease_after_a_driver_crash(self):
        from tests.test_native_facades import FacadeFixture
        from tests import test_module_collaboration as collaboration

        peers = FacadeFixture()
        self.addCleanup(peers.doCleanups)
        peers.setUp()
        task, child = peers.assign()
        other, other_child = peers.assign("prices")
        acquire = peers.acquire(task, child)
        peers.acquire(other, other_child)
        directory = peers.project.state.directory
        run = peers.project.run_id
        record = driver.settings(directory, collaboration.PROJECT, run, None, None)
        now = self.started.isoformat()
        session = dict(launch_id="killed", status=None, root_turn_status=None,
                       root_thread_id=collaboration.ROOT, started_at=now)
        proof = {"task_id":task, "lease_ref":acquire["lease_ref"], "attempt":acquire["attempt"]}
        def proven(*args, settled_effects, **kwargs):
            settled_effects.append(proof)
        work = self.view("WAITING_WORKER")
        work.update(waiting_task_ids=[task, other], run_ids=[run], run_statuses={run: "WAITING_CLIENT"})
        with (patch.object(driver, "native_active", return_value=False),
              patch.object(driver, "native_sessions", return_value=[session]),
              patch.object(driver, "closure_obstacle", side_effect=proven),
              patch.object(driver, "next_project_work", return_value=work)):
            report = driver.observe(directory, record)
            self.assertEqual(report["status"], "READY")
            self.assertEqual([(row["task_id"],row["thread"]) for row in report["worker_recovery"]], [(task,child)])
            proof["lease_ref"] = "expired-or-replaced"
            self.assertEqual(driver.observe(directory, record)["status"], "WAITING_WORKER")

    def test_queued_input_resumes_unchanged_original_work_only_once(self):
        work = self.view("READY", ["review current observations"])
        self.receipt("first", reason="NO_PROGRESS", actions=work["actions"], run_statuses=work["run_statuses"])
        project_messages.enqueue(self.state, "addition", "Consider the held-out data.")

        def launch(cmd, **kwargs):
            self.assertIn("--continue-project", cmd)
            self.assertEqual(cmd[cmd.index("--project") + 1], self.project)
            read = project_messages.take_for_turn(self.state, "original")
            self.assertEqual([row["id"] for row in read], ["addition"])
            project_messages.update_delivery(self.state, read, "turn_completed", "original-turn")
            self.receipt(cmd[cmd.index("--launch-id") + 1], reason="NO_PROGRESS", actions=work["actions"],
                         run_statuses=work["run_statuses"])
            return Mock(wait=lambda: 0)

        with patch.object(driver, "next_project_work", return_value=work), patch.object(driver.subprocess, "Popen", side_effect=launch) as proc:
            self.assertEqual(self.run_project(max_sessions=1, status_only=True)["status"], "BUDGET_LIMIT")
            self.assertEqual(self.run_project(status_only=True)["status"], "READY")
            self.assertEqual(self.run_project()["status"], "NO_PROGRESS")
            self.assertEqual(self.run_project(background=True)["status"], "NO_PROGRESS")
            self.assertEqual(proc.call_count, 1)

    def test_project_result_survives_a_final_root_message_timeout(self):
        self.receipt("final", unknown=True)
        with patch.object(driver, "next_project_work", return_value=self.view("TERMINAL")), \
                patch.object(driver.subprocess, "Popen") as launch:
            report = self.run_project()
            self.assertEqual(report["status"], "RUN_COMPLETED", report)
            self.assertEqual(report["native_closure"]["failure_type"], "AppServerTimeout")
            launch.assert_not_called()
            path = self.state / "native-session.json"
            descriptor = json.loads(path.read_text())
            descriptor["pending_effects"] = ["unresolved write"]
            path.write_text(json.dumps(descriptor))
            self.assertEqual(self.run_project()["status"], "OWNER_ACTION_REQUIRED")
            launch.assert_not_called()

    def test_entry_starts_arbitrary_prompt_hands_off_changed_actions_then_reuses_terminal_project(self):
        actions = ["independent review of observations"]
        current = self.view("EMPTY")
        dispatched = []

        def launch(cmd, **kwargs):
            self.assertEqual(self.run_project()["status"], "ALREADY_RUNNING")
            self.assertTrue(kwargs["pass_fds"])
            self.assertIn("--continue-project", cmd)
            self.assertNotIn("--collaboration", cmd)
            at = lambda key: cmd[cmd.index(key) + 1]
            self.assertEqual(at("--project"), self.project)
            content = Path(at("--prompt-file")).read_text()
            dispatched.append(content)
            if len(dispatched) == 1:
                self.assertEqual(content, self.prompt.read_text())
                current.update(self.view("READY", actions))
                self.receipt(at("--launch-id"), reason="HANDOFF", actions=actions)
            else:
                self.assertIn("same Codex thread", content)
                self.assertEqual(json.loads((self.state / "project-driver.json").read_text())["dispatch"]["actions"], actions)
                current.update(self.view("TERMINAL"))
                self.receipt(at("--launch-id"), reason="TERMINAL")
            return Mock(wait=lambda: 0)

        with patch.object(driver, "next_project_work", return_value=current), patch.object(driver.subprocess, "Popen", side_effect=launch):
            first = self.run_project(prompt=self.prompt, max_sessions=1)
            self.assertEqual(first["status"], "BUDGET_LIMIT")
            self.assertEqual(self.run_project()["status"], "BUDGET_LIMIT")
            final = self.run_project(max_sessions=2)
            self.assertEqual(final["status"], "RUN_COMPLETED")
            self.assertEqual(final["native_launches"], 2)
            self.assertEqual(self.run_project(background=True)["status"], "RUN_COMPLETED")
        self.assertEqual(len(dispatched), 2)

    def test_healthy_job_sleeps_without_model_and_only_prompts_for_original_new_observation(self):
        current = self.view("WAITING_JOB")
        self.receipt("first", reason="WAITING_JOB")
        elapsed = 0.0
        planned_at = []

        def tick(seconds):
            nonlocal elapsed
            elapsed += seconds
            if elapsed == 120:
                current.update(self.view("READY", ["reconcile existing Job river-0007"]))

        def plan(*args, **kwargs):
            planned_at.append(elapsed)
            return current

        def launch(cmd, **kwargs):
            self.assertEqual(elapsed, 120)
            self.assertEqual(json.loads((self.state / "project-driver.json").read_text())["dispatch"]["actions"],
                             ["reconcile existing Job river-0007"])
            self.receipt(cmd[cmd.index("--launch-id") + 1], reason="TERMINAL")
            current.update(self.view("TERMINAL"))
            return Mock(wait=lambda: 0)

        with patch.object(driver, "next_project_work", side_effect=plan), patch.object(driver.time, "sleep", side_effect=tick), \
                patch.object(driver.time, "monotonic", side_effect=lambda: elapsed), \
                patch.object(driver.subprocess, "Popen", side_effect=launch) as proc:
            self.assertEqual(self.run_project(max_wall_seconds=300)["status"], "RUN_COMPLETED")
            self.assertEqual(planned_at, [0, 0, 60, 120, 120])
            self.assertEqual(proc.call_count, 1)

    def test_no_progress_survives_restart_but_real_new_work_reenables_execution(self):
        current = self.view("READY", ["inspect existing evidence 1"])
        self.receipt("one", reason="NO_PROGRESS", actions=current["actions"])
        with patch.object(driver, "next_project_work", return_value=current), patch.object(driver.subprocess, "Popen") as proc:
            self.assertEqual(self.run_project()["status"], "NO_PROGRESS")
            self.assertEqual(self.run_project(background=True)["status"], "NO_PROGRESS")
            current["actions"] = ["review newly deposited observations"]
            self.assertEqual(self.run_project(status_only=True)["status"], "READY")
            proc.assert_not_called()

    def test_cli_status_selected_absent_run_and_empty_bootstrap_without_writes_or_model(self):
        for more, expected in (([], "NEEDS_PROMPT"), (["--run-id", "other-run"], "BLOCKED"),
                               (["--prompt-file", str(self.prompt)], "READY")):
            check = subprocess.run([str(contracts.HARNESS_ROOT / "bin/gx-project"), "--state-dir", str(self.state),
                                    "--project", self.project, "--status", *more], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(json.loads(check.stdout)["status"], expected)
        self.assertFalse((self.state / "project-driver.json").exists())
        self.assertFalse((self.state / "project-prompt.md").exists())

    def native_claim(self, *, principal="native:worker-9", root="original", waiting_job=False):
        at = datetime.now(timezone.utc)
        started, expires = at.isoformat(), (at + timedelta(hours=1)).isoformat()
        host = {"session_ref": root, "policy_selection": {"source": "codex_native_events_v1", "native_agent_ref": root}}
        actor = "native:worker:" + principal
        with closing(sqlite3.connect(self.state / "gx.db")) as db, db:
            db.execute("INSERT INTO runs(run_id,project_ref,objective,status,host_json,created_at,updated_at) "
                       "VALUES('river',?,'river trends','ACTIVE',?,?,?)", (self.project, json.dumps(host), started, started))
            db.execute("INSERT INTO tasks(task_id,run_id,kind,role,status,attempt,owner,lease_id,created_at,updated_at)"
                       " VALUES('analysis-1','river','analysis','researcher','CLAIMED',1,?,'lease-1',?,?)",
                       (actor, started, started))
            db.execute("INSERT INTO leases(lease_id,task_id,owner,principal,worker_role,issued_at,expires_at,attempt)"
                       " VALUES('lease-1','analysis-1',?,?,'researcher',?,?,1)", (actor, principal, started, expires))
            if waiting_job:
                db.execute("INSERT INTO jobs(job_id,run_id,task_id,capability_id,capability_version,status,idempotency_key,"
                           "submit_intent_json,input_manifest_ref,created_at) VALUES('original','river','analysis-1',"
                           "'measure','1','CREATED','original','{}','manifest',?)", (started,))
        return expires

    def test_an_uncommitted_result_does_not_wake_the_driver_until_its_original_row_commits(self):
        self.native_claim(waiting_job=True)
        self.receipt("initial", reason="WAITING_JOB")
        armed = threading.Event()
        views = []
        observe = driver.observe

        def read(*args, **kwargs):
            report = observe(*args, **kwargs)
            views.append(report["status"])
            if len(views) == 2:
                armed.set()
            return report

        def settle_row():
            self.assertTrue(armed.wait(timeout=5))
            with closing(sqlite3.connect(self.state / "gx.db")) as db, db:
                db.execute("UPDATE jobs SET status='FAILED' WHERE job_id='original'")
                time.sleep(1.3)  # Keep the transaction open across at least one local sample.
                self.assertEqual(views, ["WAITING_JOB", "WAITING_JOB"])

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(driver, "observe", side_effect=read), \
                patch.object(driver.subprocess, "Popen", return_value=Mock(wait=lambda: 97)) as launch:
            writing = pool.submit(settle_row)
            report = self.run_project(run_id="river", max_wall_seconds=12)
            writing.result(timeout=2)
        self.assertEqual(views, ["WAITING_JOB", "WAITING_JOB", "READY"])
        self.assertIn("with original Job original FAILED", report["work"]["actions"][0])
        launch.assert_called_once()
