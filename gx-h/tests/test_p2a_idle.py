"""SCRIPTED closure checks on local SQLite, native pipes and the detached CPU runner."""

from contextlib import closing
from argparse import Namespace
from datetime import timedelta
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from gx_core import budgets
from gx_core.domain import clock
from gx_core.hostobs.app_server_client import StdioTransport
from gx_core.jobs import executor
from gx_core.jobs import service as jobs
from gx_core.native_release import native_state
from gx_core.p0 import cli, idle, metering, profiles, schema
from gx_core.p0.driver import Driver
from gx_core.p0.root_delivery import RootDelivery
from gx_core.persistence import records, store
from tests import test_cpu_runner, test_p0_root_delivery


class IdleBackoff(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with native_state(self.dir):
            pass
        self.db = self.enterContext(closing(store.open_store(self.dir / "gx.db")))
        schema.install(self.db)
        self.enterContext(patch.dict(os.environ, {"GX_CODEX_OFFLINE": "1"}))
        self.child_env = {"HOME": str(self.dir), "PATH": os.defpath, "GX_CODEX_OFFLINE": "1",
                          "PYTHONNOUSERSITE": "1", **profiles.proxy_settings()}
        from gx_core.resource_profile import child_resource_environment
        self.child_env.update(child_resource_environment())
        self.rid = self.run_record()

    def tearDown(self):
        destination = os.environ.get("GX_IDLE_EVIDENCE")
        if destination:
            folder = Path(destination) / self._testMethodName
            folder.mkdir(parents=True, exist_ok=False)
            with closing(sqlite3.connect(folder / "gx.db")) as backup:
                self.db.backup(backup)
            events = [dict(row) for row in self.db.execute("SELECT * FROM events ORDER BY event_id")]
            (folder / "events.json").write_text(json.dumps(events, indent=2) + "\n")
            job = self.dir / "jobs/count-rows"
            if job.is_dir():
                for name in ("status.json", "outcome.json"):
                    source = job / name
                    if source.is_file():
                        (folder / name).write_bytes(source.read_bytes())

    def run_record(self):
        rid = records.create_run(self.db, objective="Wait for input", project_ref="idle", actor="test",
                                 inputs={"inputs": {"p0": True, "p0_spec": {"metering": {"grace_seconds": 1}}}})
        ref = budgets.create_root(self.db, run_id=rid, actor="test", profile_name="p0")
        self.db.execute("UPDATE runs SET budget_ref=?,status='ACTIVE' WHERE run_id=?", (ref, rid))
        store.save_checkpoint(self.db, run_id=rid, workflow_id="scripted", workflow_version="1", node="input", state={})
        return rid

    def test_idle01_exponential_cap_and_reset(self):
        worker = self.enterContext(closing(Driver(self.db, self.dir, self.rid)))
        with patch.object(time, "monotonic", return_value=20), closing(worker.passes(60, shared=True)) as passes:
            self.assertEqual(next(passes), 0)
            sleeps = [round(next(passes), 3) for _ in range(9)]
            self.assertEqual(sleeps, [.1, .2, .4, .8, 1.6, 3.2, 4., 4., 4.])
            worker.wake()
            self.assertAlmostEqual(next(passes), .1)
        print("IDLE01 idle seconds", sleeps)

    def test_idle02_committed_inbox_interrupts_single_and_workspace(self):
        # Prepare the sending process before the timed wait. The parent opens the Driver's
        # FIFO before letting the sender commit, including the scan-to-wait race.
        sender = """
import sys
import time
from gx_core.jobs import wakeup
from gx_core.p0 import operator
from gx_core.persistence import store
conn = store.connect(sys.argv[1])
notify = wakeup.notify
def committed(directory):
    print(time.monotonic(), flush=True)
    notify(directory)
wakeup.notify = committed
print("ready", flush=True)
sys.stdin.readline()
operator.submit(operator.local_context(conn), sys.argv[2], {"kind":"pause"}, sys.argv[3], queue_only=True)
conn.close()
"""
        for mode in ("selected", "workspace"):
            with self.subTest(mode=mode):
                rid = self.rid if mode == "selected" else self.run_record()
                with subprocess.Popen([sys.executable, "-B", "-c", sender, str(self.dir / "gx.db"), rid, mode],
                                      cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, text=True, env=self.child_env) as writer:
                    self.assertEqual(writer.stdout.readline().strip(), "ready")
                    observed = []
                    wait = idle.WorkspaceWait.wait
                    def wait_for_commit(listener, seconds, drivers=()):
                        if seconds < 1 or observed:
                            return wait(listener, seconds, drivers)
                        writer.stdin.write("commit\n")
                        writer.stdin.flush()
                        ready = wait(listener, seconds, drivers)
                        returned = time.monotonic()
                        self.assertIsNone(ready)
                        observed.append(returned - float(writer.stdout.readline()))
                        return ready
                    try:
                        with patch.object(idle, "MIN_SECONDS", 2), patch.object(idle.WorkspaceWait, "wait", wait_for_commit):
                            result = cli.execute(Namespace(command="run", state=self.dir, timeout=7,
                                                           run_id=rid if mode == "selected" else None))
                        self.assertEqual(writer.wait(timeout=2), 0)
                    finally:
                        if writer.poll() is None:
                            writer.kill()
                self.assertEqual(result["status"], "PAUSED")
                self.assertEqual(len(observed), 1)
                self.assertLess(observed[0], .25)
                self.assertIsNotNone(self.db.execute("SELECT result_json FROM p0_operator_messages WHERE message_id=?",
                                                     (mode,)).fetchone()[0])
                print("IDLE02", mode, "commit wake seconds", round(observed[0], 4))

    def test_idle02_rollback_does_not_wake_and_native_pipe_wakes_only_its_run(self):
        with idle.WorkspaceWait(self.db) as listener, closing(store.connect(self.dir / "gx.db")) as peer:
            with store.transaction(peer):
                with self.assertRaises(ValueError), store.transaction(peer):
                    peer.execute("UPDATE runs SET objective='discarded' WHERE run_id=?", (self.rid,))
                    raise ValueError("rollback")
            self.assertEqual(listener.wait(.025), set())
        # The committed outer savepoint transaction can advance SQLite data_version
        # even after its only update was undone. Observe the independent pipe on a fresh baseline.
        with idle.WorkspaceWait(self.db) as listener:
            with closing(StdioTransport([sys.executable, "-I", "-S", "-c",
                                         "import sys; sys.stdin.readline(); print('ready', flush=True)"],
                                        env=self.child_env)) as transport:
                from types import SimpleNamespace
                worker = Driver(self.db, self.dir, self.rid)
                worker._native = SimpleNamespace(client=SimpleNamespace(transport=transport))
                transport.write_line(b"go\n", 1)
                self.assertEqual(listener.wait(2, [worker]), {self.rid})
                self.assertEqual(transport.read_line(1), b"ready\n")

    def test_idle02_detached_outcome_interrupts_an_existing_wait(self):
        fixture = test_cpu_runner.DetachedCpuJob()
        fixture.job = executor.paths(self.dir / "jobs", "count-rows")
        fixture.job.create()
        with idle.WorkspaceWait(self.db) as listener:
            fixture.start("import time; time.sleep(.3); print(5)", timeout=3)
            started = time.monotonic()
            until = started + 4
            while time.monotonic() < until:
                self.assertIsNone(listener.wait(until - time.monotonic()))
                fact = executor.observe(fixture.job.job_dir)
                if fact["state"] == executor.FINISHED:
                    break
            self.assertEqual((fact["state"], fact["outcome"]["exit_code"]), (executor.FINISHED, 0))
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 3)
            print("IDLE02 detached CPU outcome seconds", round(elapsed, 4))

    def test_idle02_store_version_wakes_when_filesystem_cannot_create_fifo(self):
        with patch.object(os, "mkfifo", side_effect=OSError("no FIFO support")), idle.WorkspaceWait(self.db) as listener:
            with closing(store.connect(self.dir / "gx.db")) as other:
                other.execute("UPDATE runs SET objective='external input' WHERE run_id=?", (self.rid,))
            self.assertIsNone(listener.fd)
            self.assertIsNone(listener.wait(2))
            self.assertEqual(listener.wait(.025), set())

    def test_idle02_signals_interrupt_both_blocked_cli_modes(self):
        child = """
import sys
from gx_core.p0 import cli, idle
idle.MIN_SECONDS = 20
wait = idle.WorkspaceWait.wait
def announce(listener, seconds, drivers=()):
    if seconds > 2:
        print("waiting", flush=True)
    return wait(listener, seconds, drivers)
idle.WorkspaceWait.wait = announce
raise SystemExit(cli.main(["run", "--state", sys.argv[1], "--timeout", "30", *sys.argv[2:]]))
"""
        for mode, sig in (("selected", signal.SIGINT), ("workspace", signal.SIGTERM),
                          ("selected", signal.SIGTERM), ("workspace", signal.SIGINT)):
            selector = ["--run-id", self.rid] if mode == "selected" else []
            with self.subTest(mode=mode, signal=sig), subprocess.Popen([sys.executable, "-B", "-c", child,
                str(self.dir), *selector], cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=self.child_env) as proc:
                self.assertEqual(proc.stdout.readline().strip(), "waiting")
                started = time.monotonic()
                try:
                    proc.send_signal(sig)
                    self.assertEqual(proc.wait(timeout=1.5), 130 if sig == signal.SIGINT else -sig)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                print("IDLE02", mode, sig.name, "exit seconds", round(time.monotonic() - started, 4))

    def test_idle03_job_and_lease_clip_a_long_wait(self):
        self.enterContext(patch.object(clock, "_source", return_value=clock.now()))
        worker = Driver(self.db, self.dir, self.rid)
        def next_wait():
            worker.idle_seconds = idle.MAX_SECONDS
            now = time.monotonic()
            return worker._next_delay(clock.now_iso(), now, now + 30)
        task = records.create_task(self.db, run_id=self.rid, actor="test", kind="research_round", role="planner")
        lease = store.issue_lease(self.db, task_id=task, owner="worker", worker_role="planner", seconds=30, attempt=1)
        self.db.execute("UPDATE leases SET expires_at=? WHERE lease_id=?", (clock.iso_after(.45), lease["lease_id"]))
        self.assertAlmostEqual(next_wait(), .45, delta=.02)
        created = clock.iso_after(-jobs.start_deadline_seconds() + .18)
        self.db.execute("INSERT INTO jobs(job_id,run_id,capability_id,capability_version,status,idempotency_key,"
                        "submit_intent_json,input_manifest_ref,created_at) "
                        "VALUES('job',?,'fixture','1','CREATED','job','{}','in',?)", (self.rid, created))
        self.assertAlmostEqual(next_wait(), .18, delta=.02)
        self.db.execute("UPDATE jobs SET status='SUBMITTING',observed_at=?", (clock.iso_after(-jobs.SUBMIT_GRACE_SECONDS + .09),))
        self.assertAlmostEqual(next_wait(), .09, delta=.02)
        store.release_lease(self.db, lease["lease_id"])
        self.db.execute("UPDATE jobs SET status='OUTCOME_UNKNOWN'")
        self.assertAlmostEqual(next_wait(), jobs.MAX_POLL_SECONDS, delta=.02)

    def test_idle03_stopped_run_wakes_at_usage_grace_and_settles(self):
        peer = test_p0_root_delivery.Peer(self.db, self.rid)
        receipt = RootDelivery(peer)
        turn = receipt.deliver("root", "compute", event_id="start")["turn_id"]
        receipt.observe("turn/started", {"threadId": "root", "turn": {"id": turn}})
        peer.turns[-1]["status"] = "interrupted"
        receipt.observe("turn/completed", {"threadId": "root", "turn": peer.turns[-1]})
        self.db.execute("UPDATE runs SET status='PAUSED' WHERE run_id=?", (self.rid,))
        terminal_at = clock.iso_after(-.35)
        grace_at = clock.parse(terminal_at) + timedelta(seconds=1)
        self.db.execute("UPDATE p0_meter_turns SET terminal_at=?", (terminal_at,))
        worker = self.enterContext(closing(Driver(self.db, self.dir, self.rid)))
        initial = metering.next_deadline_seconds(self.db, self.rid)
        self.assertGreater(initial, 0)
        started = time.monotonic()
        waits = []
        wait = idle.WorkspaceWait.wait
        def measure(listener, delay, drivers=()):
            remaining = (grace_at - clock.now()).total_seconds()
            self.assertLessEqual(delay, remaining + .025)
            result = wait(listener, delay, drivers)
            waits.append((delay, (clock.now() - grace_at).total_seconds()))
            return result
        worker.idle_seconds = idle.MAX_SECONDS
        with patch.object(idle, "MIN_SECONDS", 4), patch.object(idle.WorkspaceWait, "wait", measure):
            result = worker.drive(2)
        elapsed = time.monotonic() - started
        row = result["metering"]["turns"][0]
        self.assertEqual(row["confidence"], "ESTIMATED")
        grace_to_commit = (clock.parse(row["estimated_at"]) - clock.parse(row["terminal_at"])).total_seconds()
        self.assertGreaterEqual(grace_to_commit, 1)
        self.assertTrue(all(delay <= initial + .005 for delay, overshoot in waits), waits)
        print("IDLE03 grace remaining/closure seconds", round(initial, 4), round(elapsed, 4),
              "scheduled wait and wake offset", [[round(value, 4) for value in pair] for pair in waits],
              "post-deadline settlement seconds", round(grace_to_commit - 1, 4))

    def test_idle03_product_services_job_and_lease_at_their_own_deadlines(self):
        # Script only the wall/monotonic clocks and the passage of time while blocked.
        # JobService, lease recovery, durable effects and the public Driver all run normally.
        origin = clock.parse(clock.now_iso())
        elapsed = [0.]
        self.enterContext(patch.object(clock, "_source", side_effect=lambda: origin + timedelta(seconds=elapsed[0])))
        self.enterContext(patch.object(time, "monotonic", side_effect=lambda: 100 + elapsed[0]))
        task = records.create_task(self.db, run_id=self.rid, actor="test", kind="research_round", role="planner")
        lease = store.issue_lease(self.db, task_id=task, owner="worker", worker_role="planner", seconds=30, attempt=1)
        self.db.execute("UPDATE tasks SET status='RUNNING',attempt=1,lease_id=? WHERE task_id=?", (lease["lease_id"], task))
        self.db.execute("UPDATE leases SET expires_at=? WHERE lease_id=?", (clock.iso_after(.42), lease["lease_id"]))
        self.db.execute("INSERT INTO jobs(job_id,run_id,capability_id,capability_version,status,idempotency_key,"
                        "submit_intent_json,input_manifest_ref,created_at) "
                        "VALUES('job',?,'fixture','1','CREATED','job','{}','in',?)",
                        (self.rid, clock.iso_after(-jobs.start_deadline_seconds() + .18)))
        slept = []
        def advance(listener, delay, drivers=()):
            self.assertGreaterEqual(delay, 0)
            slept.append(delay)
            self.assertLess(len(slept), 15)
            elapsed[0] += delay + .001
            return set()
        with patch.object(idle, "MIN_SECONDS", 4), patch.object(idle.WorkspaceWait, "wait", advance):
            with closing(Driver(self.db, self.dir, self.rid)) as worker:
                worker.drive(1)
        status, ended = self.db.execute("SELECT status,finished_at FROM jobs WHERE job_id='job'").fetchone()
        released = self.db.execute("SELECT released_at FROM leases WHERE lease_id=?", (lease["lease_id"],)).fetchone()[0]
        self.assertEqual(status, "FAILED")
        self.assertAlmostEqual((clock.parse(ended) - origin).total_seconds(), .18, delta=.004)
        self.assertAlmostEqual((clock.parse(released) - origin).total_seconds(), .42, delta=.004)
        print("IDLE03 scripted clock Job/Lease elapsed", (clock.parse(ended) - origin).total_seconds(),
              (clock.parse(released) - origin).total_seconds())
