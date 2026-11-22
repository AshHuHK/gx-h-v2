"""The public start entry and coordinator share durable, visible keyed requests."""
from argparse import Namespace
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gx_core.jobs.service import JobService
from gx_core.native_release import native_state
from gx_core.p0 import cli, coordinator, profiles
from gx_core.persistence import store


class WorkspaceQueueTests(unittest.TestCase):
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
