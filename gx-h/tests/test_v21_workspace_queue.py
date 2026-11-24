"""The public start entry and coordinator share durable, visible keyed requests."""
from argparse import Namespace
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gx_core.errors import DependencyUnmet, OutcomeUnknown, ResourceDenied
from gx_core.hostobs.app_server_client import AppServerDisconnected, AppServerTimeout
from gx_core.jobs.limits import cpu_threads_finding
from gx_core.jobs.service import JobService
from gx_core.native_release import native_state
from gx_core.orchestration import reactor, service
from gx_core.orchestration.engine import Engine
from gx_core.p0 import cli, coordinator, operator, profiles, workspace
from gx_core.persistence import store
from tests.test_tl1a_geometry import OfflineIngress


class WorkspaceQueueTests(unittest.TestCase):
    def test_cpu_request_larger_than_the_entire_pool_preserves_admission_refusal(self):
        refusal = cpu_threads_finding(3, free=0, allowed=2, held=2, holders=["other-job"])
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with native_state(state) as site, closing(store.open_store(site.db_path)) as conn:
            with patch.object(JobService, "_threads_or_refuse", side_effect=refusal), self.assertRaises(ResourceDenied) as seen:
                cli.start(conn, state, cli.demo_spec(), run_key="oversized")
            self.assertIs(seen.exception, refusal)
            run = workspace.find_run(conn, run_key="oversized")["run_id"]
            self.assertFalse(Engine(conn, service.default_registry()).waits(run))
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (run,)).fetchone()[0], 0)

    def test_partial_start_waits_for_sibling_job_threads_and_reuses_admitted_work(self):
        self.enterContext(patch.dict(os.environ, GX_TEST_RESOURCE_PROFILE="deterministic", GX_CODEX_OFFLINE="1"))
        state = Path(self.enterContext(tempfile.TemporaryDirectory())) / "state"
        spec = cli.demo_spec()
        # Simulate a submitter that stopped between admission and launch. Its real
        # CREATED Jobs and reservations hold both threads until their owner releases them.
        def admitted(service, row):
            return service.view(row)
        with native_state(state) as site, closing(store.open_store(site.db_path)) as conn:
            with patch.object(cli, "Driver", OfflineIngress), patch.object(
                    Engine, "start", side_effect=AppServerDisconnected("before first checkpoint")):
                with self.assertRaises(AppServerDisconnected):
                    cli.start(conn, state, spec)
            run = workspace.find_run(conn)["run_id"]
            with patch.object(JobService, "_start", admitted):
                sibling = cli.start(conn, state, spec, run_key="B")["run_id"]
            held = [row[0] for row in conn.execute(
                "SELECT job_id FROM jobs WHERE run_id=? AND status='CREATED' ORDER BY rowid", (sibling,))]
            self.assertEqual(len(held), 2)
        recovered = cli.execute(Namespace(command="resume", state=state, run_id=run, timeout=.3))
        self.assertEqual(recovered["status"], "WAITING_EXTERNAL_JOB")
        with closing(store.open_store(state / "gx.db")) as conn:
            engine = Engine(conn, service.default_registry())
            wait = engine.waits(run)["experiment"]
            self.assertEqual(wait["interrupt"]["payload"]["required_threads"], 1)
            self.assertEqual(wait["interrupt"]["payload"]["wait_for"], "cpu.threads_free")
            self.assertFalse(engine.standing(run, interrupt_ref=wait["interrupt_ref"]).complete)
            with self.assertRaises(DependencyUnmet):
                engine.resume(run_id=run, interrupt_ref=wait["interrupt_ref"])
            def own_jobs():
                return [row[0] for row in conn.execute("SELECT job_id FROM jobs WHERE run_id=? ORDER BY rowid", (run,))]
            self.assertEqual(own_jobs(), [])
            jobs = JobService(conn)
            jobs.cancel(held[0], reason="release one thread")
            self.assertTrue(engine.standing(run, interrupt_ref=wait["interrupt_ref"]).complete)
            with patch.object(JobService, "_start", admitted):
                reactor.drive_science(cli.context(conn), run)
            first, = own_jobs()
            blocked = engine.waits(run)["experiment"]["interrupt_ref"]
            self.assertFalse(engine.standing(run, interrupt_ref=blocked).complete)
            self.assertIsNone(reactor.drive_science(cli.context(conn), run))
            self.assertEqual(engine.waits(run)["experiment"]["interrupt_ref"], blocked)
            jobs.cancel(held[1], reason="release remaining thread")
            reactor.drive_science(cli.context(conn), run)
            first_again, second = own_jobs()
            self.assertEqual(first, first_again)
            self.assertEqual([jobs.wait(job_id, 30)["status"] for job_id in (first, second)], ["SUCCEEDED"] * 2)
            self.assertEqual(len(workspace.list_runs(conn)), 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM p0_human_inputs WHERE run_id=?", (run,)).fetchone()[0], 1)
            self.assertFalse(engine.state(run)["failed_nodes"])

    def test_start_while_active_replay_rejection_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            spec_file = Path(tmp) / "study.json"
            spec = cli.demo_spec()
            spec_file.write_text(json.dumps(spec))

            def start(key):
                return cli.execute(Namespace(command="start", state=state, spec=spec_file,
                                             example=None, run_key=key))

            a = start("A")
            self.assertEqual(start(None)["run_id"], a["run_id"])
            with native_state(state):
                request = start("B")
                self.assertEqual(request["status"], "queued_for_active_driver")
                self.assertEqual(start("B"), request)
                spec_file.write_text(json.dumps({**spec, "max_rounds": -1}))
                bad = start("invalid")
                with self.assertRaisesRegex(ValueError, "different specification"):
                    start("B")

            with closing(store.open_store(state / "gx.db")) as conn:
                self.assertEqual(len(coordinator.overview(conn)["workspace_requests"]), 2)
                coordinator.drain_starts(conn, state)
                coordinator.drain_starts(conn, state)
                status = coordinator.overview(conn)
                self.assertEqual([r["run_key"] for r in status["runs"]], ["A", "B"])
                failed, = status["workspace_requests"]
                self.assertEqual(failed["outbox_id"], bad["outbox_id"])
                self.assertEqual(failed["status"], "rejected")
                self.assertIn("positive max_rounds", failed["last_error"])
                with native_state(state), self.assertRaisesRegex(ValueError, "positive max_rounds"):
                    start("invalid")
                for row in conn.execute("SELECT job_id FROM jobs").fetchall():
                    self.assertTrue(JobService(conn).wait(row[0], 30)["terminal"])

    def test_uncreated_start_stays_pending_at_timeout_and_replays_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            spec = cli.demo_spec()
            with closing(store.open_store(state / "gx.db")) as conn:
                initial = coordinator.queue_start(conn, "delayed", spec)
                with patch.object(cli, "start", side_effect=AppServerTimeout("start reply delayed")) as start:
                    result = coordinator.drive_workspace(conn, state, 0.15)
                self.assertEqual(start.call_count, 1)
                self.assertEqual(result["driver"], "time_limit")
                self.assertEqual(result["runs"], [])
                pending, = result["workspace_requests"]
                self.assertEqual(pending["status"], "queued")
                self.assertIn("start reply delayed", pending["last_error"])
                replay = coordinator.queue_start(conn, "delayed", spec)
                self.assertEqual(replay["outbox_id"], initial["outbox_id"])
                self.assertEqual(replay["attempts"], 1)
                self.assertEqual(replay["last_error"], pending["last_error"])

            # A fresh process replays the same durable request. Pause after real
            # bootstrap so the test exercises the workspace driver without a model.
            actual_start = cli.start
            def start_and_pause(conn, directory, spec, *, run_key):
                actual_start(conn, directory, spec, run_key=run_key)
                cli.change_state(conn, workspace.find_run(conn, run_key=run_key), "pause")
            with closing(store.open_store(state / "gx.db")) as conn:
                with patch.object(cli, "start", side_effect=start_and_pause) as start:
                    result = coordinator.drive_workspace(conn, state, 4)
                self.assertEqual(start.call_count, 1)
                self.assertEqual(result["status"], "PAUSED")
                self.assertNotIn("workspace_requests", result)
                row, = conn.execute("SELECT attempts,last_error,consumed_at FROM outbox WHERE outbox_id=?",
                                    (initial["outbox_id"],)).fetchall()
                self.assertEqual(row["attempts"], 1)
                self.assertIsNone(row["last_error"])
                self.assertIsNotNone(row["consumed_at"])
                self.assertEqual(len(workspace.list_runs(conn)), 1)
                for job in conn.execute("SELECT job_id FROM jobs").fetchall():
                    self.assertTrue(JobService(conn).wait(job[0], 30)["terminal"])

    def test_partial_start_retry_preserves_run_and_allows_other_run_to_progress(self):
        with tempfile.TemporaryDirectory() as tmp, closing(store.open_store(Path(tmp) / "gx.db")) as conn:
            state, spec = Path(tmp), cli.demo_spec()
            run_a = cli.start(conn, state, spec, run_key="A")["run_id"]
            operator.submit(operator.local_context(conn), run_a, {"kind": "pause"}, "pause-A", queue_only=True)
            with patch.object(workspace, "bootstrap_action", side_effect=OutcomeUnknown("bootstrap reply lost")):
                with self.assertRaisesRegex(OutcomeUnknown, "bootstrap reply lost"):
                    cli.start(conn, state, spec, run_key="B")
            original = workspace.find_run(conn, run_key="B")
            self.assertIsNone(store.latest_checkpoint(conn, original["run_id"]))
            actual_start = cli.start
            attempts = []

            def retry_start(db, directory, spec, *, run_id):
                self.assertEqual(run_id, original["run_id"])
                attempts.append(run_id)
                if len(attempts) == 1:
                    raise AppServerDisconnected("lost bootstrap connection")
                pending, = coordinator.overview(db)["workspace_requests"]
                self.assertEqual(pending["status"], "queued")
                self.assertIn("lost bootstrap connection", pending["last_error"])
                self.assertEqual(coordinator.queue_start(db, "B", spec)["outbox_id"], pending["outbox_id"])
                self.assertEqual(workspace.find_run(db, run_id=run_a)["status"], "PAUSED")
                message, = db.execute("SELECT result_json,rejected FROM p0_operator_messages WHERE message_id='pause-A'")
                self.assertIsNotNone(message["result_json"])
                self.assertIsNone(message["rejected"])
                actual_start(db, directory, spec, run_id=run_id)
                cli.change_state(db, workspace.find_run(db, run_id=run_id), "pause")

            with patch.object(cli, "start", side_effect=retry_start):
                result = coordinator.drive_workspace(conn, state, 5)
            self.assertEqual(attempts, [original["run_id"], original["run_id"]])
            self.assertEqual(result["status"], "PAUSED")
            self.assertNotIn("driver", result)
            self.assertNotIn("workspace_requests", result)
            self.assertEqual({r["run_key"]: r["run_id"] for r in result["runs"]},
                             {"A": run_a, "B": original["run_id"]})
            self.assertIsNotNone(store.latest_checkpoint(conn, original["run_id"]))
            self.assertEqual(workspace.find_run(conn, run_key="B")["budget_ref"], original["budget_ref"])
            row, = conn.execute("SELECT attempts,last_error,consumed_at FROM outbox WHERE topic='p0.workspace.start'")
            self.assertEqual(row["attempts"], 1)
            self.assertIsNone(row["last_error"])
            self.assertIsNotNone(row["consumed_at"])
            for job in conn.execute("SELECT job_id FROM jobs").fetchall():
                self.assertTrue(JobService(conn).wait(job[0], 30)["terminal"])

    def test_unkeyed_partial_start_resumes_by_saved_id_in_a_mixed_workspace(self):
        for command in ("run", "resume"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                state, spec = Path(tmp) / "state", cli.demo_spec()
                with native_state(state) as site, closing(store.open_store(site.db_path)) as conn:
                    # After the original operator Intent commits but before its first checkpoint,
                    # another process can admit a keyed sibling from that shared project Intent.
                    with patch.object(cli, "Driver", OfflineIngress), patch.object(
                            Engine, "start", side_effect=AppServerDisconnected("first checkpoint pending")):
                        with self.assertRaisesRegex(AppServerDisconnected, "first checkpoint pending"):
                            cli.start(conn, state, spec)
                    original = workspace.find_run(conn)
                    rid = original["run_id"]
                    self.assertIsNone(original["run_key"])
                    self.assertIsNone(store.latest_checkpoint(conn, rid))
                    sibling = cli.start(conn, state, spec, run_key="B")["run_id"]
                    cli.change_state(conn, workspace.find_run(conn, run_id=sibling), "pause")
                    operator.submit(operator.local_context(conn), rid, {"kind": "pause"}, "pause-original", queue_only=True)
                result = cli.execute(Namespace(command=command, state=state, run_id=rid if command == "resume" else None,
                                               timeout=4))
                self.assertEqual(result["status"], "PAUSED")
                self.assertNotIn("driver", result)
                with closing(store.open_store(state / "gx.db")) as conn:
                    current = workspace.find_run(conn, run_id=rid)
                    self.assertIsNone(current["run_key"])
                    self.assertEqual(current["budget_ref"], original["budget_ref"])
                    self.assertEqual([r["run_id"] for r in workspace.list_runs(conn)], [rid, sibling])
                    self.assertIsNotNone(store.latest_checkpoint(conn, rid))
                    self.assertEqual(conn.execute("SELECT count(*) FROM p0_human_inputs WHERE run_id=?", (rid,)).fetchone()[0], 1)
                    for job in conn.execute("SELECT job_id FROM jobs").fetchall():
                        self.assertTrue(JobService(conn).wait(job[0], 30)["terminal"])

    def test_portable_installation_inherits_proxy_and_restricts_bypass(self):
        # Only the configuration reader is exercised. No sockets are used.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp)
            (cfg / "p0_network.json").write_text('{"mode":"environment"}')
            env = {"HTTPS_PROXY": "http://localhost:8080", "NO_PROXY": "127.0.0.1,example.org"}
            with patch.object(profiles.config, "CONFIG_DIR", cfg), patch.object(profiles.os, "environ", env):
                values = profiles.proxy_settings()
                self.assertEqual(values["https_proxy"], "http://localhost:8080")
                self.assertEqual(values["no_proxy"], "127.0.0.1")
