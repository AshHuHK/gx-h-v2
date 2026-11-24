"""Local process handoff, durable commit fallback and FIFO path ownership."""

from contextlib import ExitStack, closing
import errno
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gx_core.jobs import wakeup
from gx_core.native_release import ReleaseStateError, native_state
from gx_core.p0.driver import Driver
from gx_core.p0.idle import WorkspaceWait
from gx_core.persistence import store
from tests import test_p2a_idle


def _reader(directory, channel):
    """Commands synchronize real process lifetimes without assuming which reader runs first."""
    try:
        with closing(store.connect(directory / "gx.db")) as conn, ExitStack() as ownership, ExitStack() as listeners:
            channel.send(os.getpid())
            while True:
                action = channel.recv()
                if action == "stop":
                    break
                if action == "lock":
                    try:
                        ownership.enter_context(native_state(directory))
                    except ReleaseStateError as error:
                        channel.send(str(error))
                    else:
                        channel.send("owned")
                elif action == "open":
                    listener = listeners.enter_context(WorkspaceWait(conn))
                    channel.send(os.fstat(listener.fd).st_ino if listener.fd is not None else None)
                elif action == "close":
                    listeners.close()
                    ownership.close()
                    channel.send("closed")
                elif action == "wait":
                    channel.send("armed")
                    started = time.monotonic()
                    ready = listener.wait(3)
                    channel.send({"workspace_wake": ready is None, "elapsed_s": time.monotonic() - started,
                                  "returned_at": time.monotonic()})
    finally:
        channel.close()


class FifoLifecycle(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with native_state(self.workspace) as site:
            self.db = self.enterContext(closing(store.open_store(site.db_path)))
        self.db.execute("CREATE TABLE fifo_probe(value TEXT)")
        self.path = self.workspace / wakeup.PIPE_NAME
        sink = os.environ.get("GX_FIFO_EVIDENCE")
        self.evidence = Path(sink) if sink else None
        if self.evidence is not None:
            self.evidence.mkdir(parents=True, exist_ok=True)

    def save(self, name, data):
        if self.evidence is not None:
            with (self.evidence / name).open("x") as output:
                json.dump(data, output, indent=2)
                output.write("\n")

    def peer(self):
        ctx = multiprocessing.get_context("spawn")
        channel, child = ctx.Pipe()
        process = ctx.Process(target=_reader, args=(self.workspace, child))
        process.start()
        child.close()
        def finish():
            if process.is_alive():
                try:
                    channel.send("stop")
                except BrokenPipeError:
                    pass
                process.join(2)
            if process.is_alive():
                process.terminate()
                process.join(2)
            channel.close()
            process.close()
        self.addCleanup(finish)
        self.assertEqual(self.reply(channel), process.pid)
        return channel, process.pid

    def reply(self, channel):
        self.assertTrue(channel.poll(10), "reader did not acknowledge the prior command")
        return channel.recv()

    def command(self, channel, action):
        channel.send(action)
        return self.reply(channel)

    def arm(self, peer):
        self.assertEqual(self.command(peer, "wait"), "armed")

    def woke(self, peer):
        seen = self.reply(peer)
        self.assertTrue(seen["workspace_wake"], seen)
        self.assertLess(seen["elapsed_s"], 1.5)
        return seen

    def test_native_lock_handoff_and_job_hint_before_and_during_wait(self):
        first, first_pid = self.peer()
        second, second_pid = self.peer()
        self.assertEqual(self.command(first, "lock"), "owned")
        inode = self.command(first, "open")
        self.assertIsNotNone(inode)
        refused = self.command(second, "lock")
        self.assertIn("another native client", refused)
        # The direct reader API also works outside the CLI's exclusive ownership.
        self.assertEqual(self.command(second, "open"), inode)
        self.assertEqual(self.command(first, "close"), "closed")
        self.assertEqual(self.command(second, "lock"), "owned")
        self.assertEqual(self.path.lstat().st_ino, inode)

        # Pure Job hints generate no SQLite writes. They must reach the successor's open FD.
        wakeup.notify(self.workspace)
        self.arm(second)
        early = self.woke(second)
        self.arm(second)
        self.assertFalse(second.poll(.05))
        notified = time.monotonic()
        wakeup.notify(self.workspace)
        late = self.woke(second)
        self.assertGreaterEqual(late["returned_at"], notified)
        self.assertEqual(self.command(second, "close"), "closed")
        self.assertEqual(self.path.lstat().st_ino, inode)
        self.save("handoff.json", {"writer_pid": os.getpid(), "reader_pids": [first_pid, second_pid],
                                   "native_lock_contention": refused, "fifo_inode": inode, "early": early,
                                   "late": late, "late_signal_to_return_s": late["returned_at"] - notified})

    def test_consumed_or_disconnected_hint_still_observes_commits(self):
        first, first_pid = self.peer()
        second, second_pid = self.peer()
        inode = self.command(first, "open")
        self.assertIsNotNone(inode)
        self.assertEqual(self.command(second, "open"), inode)
        self.arm(first)
        self.arm(second)
        def commit(note):
            published = []
            with store.transaction(self.db):
                self.db.execute("INSERT INTO fifo_probe VALUES(?)", (note,))
                store.on_transaction_end(self.db, on_commit=lambda: published.append(time.monotonic()))
            return published[0]
        hinted_at = commit("managed commit, one FIFO hint")
        shared = [self.woke(first), self.woke(second)]

        # Replace a still-open endpoint to reproduce old cleanup or an external workspace move.
        # Keep both readers on the old inode. This producer cannot deliver a byte to either.
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        replacement = self.path.stat().st_ino
        self.assertNotEqual(replacement, inode)
        self.arm(first)
        self.arm(second)
        self.assertFalse(first.poll(.05))
        self.assertFalse(second.poll(.05))
        stale_at = commit("managed commit, replaced FIFO")
        stalled = [self.woke(first), self.woke(second)]
        # An independent raw writer can also commit before the reader enters select.
        self.db.execute("INSERT INTO fifo_probe VALUES('SQLite autocommit before wait')")
        self.arm(first)
        early = self.woke(first)
        self.assertEqual(self.command(first, "close"), "closed")
        self.assertEqual(self.command(second, "close"), "closed")
        self.assertEqual(self.path.lstat().st_ino, replacement)
        self.save("fallback.json", {"writer_pid": os.getpid(), "reader_pids": [first_pid, second_pid],
                                    "open_inode": inode, "replacement_inode": replacement,
                                    "one_hint_two_readers": shared, "stale_fds": stalled, "early_autocommit": early,
                                    "one_hint_commit_to_return_s": [r["returned_at"] - hinted_at for r in shared],
                                    "stale_commit_to_return_s": [r["returned_at"] - stale_at for r in stalled],
                                    "commits": [r[0] for r in self.db.execute("SELECT value FROM fifo_probe ORDER BY rowid")]})
        if self.evidence is not None:
            with closing(sqlite3.connect(self.evidence / "fallback.db")) as backup:
                self.db.backup(backup)
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_type_owner_and_mode_are_checked_by_reader_and_writer(self):
        target = self.workspace / "untouched"
        target.write_bytes(b"unchanged")
        observations = []
        for kind in ("file", "symlink", "permissions", "owner"):
            with self.subTest(kind=kind), ExitStack() as resources:
                ownership = {}
                if kind == "file":
                    self.path.write_bytes(b"unchanged")
                elif kind == "symlink":
                    self.path.symlink_to(target)
                else:
                    os.mkfifo(self.path, 0o600)
                    watcher = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
                    resources.callback(os.close, watcher)
                    if kind == "permissions":
                        self.path.chmod(0o620)
                    else:
                        ownership = self.foreign_owner(resources)
                original = self.path.lstat()
                with WorkspaceWait(self.db) as listener:
                    self.assertIsNone(listener.fd)
                    wakeup.notify(self.workspace)
                    with closing(store.connect(self.workspace / "gx.db")) as other:
                        other.execute("INSERT INTO fifo_probe VALUES(?)", (kind,))
                    self.assertIsNone(listener.wait(2))
                self.assertTrue(os.path.samestat(original, self.path.lstat()))
                if kind in ("file", "symlink"):
                    self.assertEqual(self.path.read_bytes(), b"unchanged")
                else:
                    with self.assertRaises(BlockingIOError):
                        os.read(watcher, 1)
                observations.append({"kind": kind, "owner": original.st_uid, "reader_euid": os.geteuid(),
                                     "mode": oct(stat.S_IMODE(original.st_mode)), "inode": original.st_ino,
                                     **ownership})
                self.path.unlink()
        self.save("path-validation.json", observations)

    def foreign_owner(self, resources):
        uid_map = [list(map(int, line.split())) for line in Path("/proc/self/uid_map").read_text().splitlines()]
        foreign_uid = 65534 if os.geteuid() != 65534 else 65533
        detail = {"uid_map": uid_map, "tested_owner": foreign_uid}
        try:
            os.chown(self.path, foreign_uid, -1)
        except OSError as error:
            mapped = any(first <= foreign_uid < first + count for first, _, count in uid_map)
            if error.errno not in (errno.EACCES, errno.EPERM) and not (error.errno == errno.EINVAL and not mapped):
                raise
            detail.update(owner_source="fixture_lstat", chown_errno=error.errno)
        else:
            self.assertEqual(self.path.lstat().st_uid, foreign_uid)
            return dict(detail, owner_source="filesystem")

        # A single-UID namespace cannot chown to another UID. Supply only this
        # FIFO's lstat owner to the wakeup module; all I/O and real credentials stay intact.
        def foreign_lstat(path):
            info = os.lstat(path)
            if Path(path) != self.path:
                return info
            fields = list(info)
            fields[stat.ST_UID] = foreign_uid
            return os.stat_result(fields)

        local_os = SimpleNamespace(**vars(os))
        local_os.lstat = foreign_lstat
        resources.enter_context(patch.object(wakeup, "os", local_os))
        return detail

    def test_readiness_may_be_drained_before_read(self):
        with WorkspaceWait(self.db) as listener, ExitStack() as handles:
            second = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
            handles.callback(os.close, second)
            wakeup.notify(self.workspace)
            read = os.read
            drained = []
            def peer_first(fd, size):
                drained.append(read(second, 1))
                return read(fd, size)  # real kernel EAGAIN after the other reader's read
            with patch.object(os, "read", side_effect=peer_first):
                self.assertIsNone(listener.wait(2))
            self.assertEqual(drained, [b"w"])
            self.assertEqual(listener.wait(.02), set())
        # A high-numbered, closed descriptor produces actual kernel EBADF without
        # being reused by the selector's new epoll descriptor.
        with WorkspaceWait(self.db) as listener, ExitStack() as handles:
            high = fcntl.fcntl(listener.fd, fcntl.F_DUPFD_CLOEXEC, 128)
            os.close(high)
            os.close(listener.fd)
            listener.fd = high
            native, writer = os.pipe()
            handles.callback(os.close, native)
            handles.callback(os.close, writer)
            os.write(writer, b"native ready")
            ready = listener.wait(1, [SimpleNamespace(read_fds=(native,), rid="peer")])
            self.assertEqual(ready, {"peer"})
            self.assertIsNone(listener.fd)
            with closing(store.connect(self.workspace / "gx.db")) as other:
                other.execute("INSERT INTO fifo_probe VALUES('registration failed')")
            self.assertIsNone(listener.wait(1))
        with WorkspaceWait(self.db) as listener:
            wakeup.notify(self.workspace)
            observed = []
            def closed_read(fd, size):
                os.close(fd)
                try:
                    return read(fd, size)
                except OSError as error:
                    observed.append(error.errno)
                    raise
            with patch.object(os, "read", side_effect=closed_read):
                self.assertIsNone(listener.wait(1))
            self.assertEqual(observed, [errno.EBADF])
            self.assertIsNone(listener.fd)
            with closing(store.connect(self.workspace / "gx.db")) as other:
                other.execute("INSERT INTO fifo_probe VALUES('read failed')")
            self.assertIsNone(listener.wait(1))
        self.save("read-race.json", {"drained_bytes": len(drained[0]), "next_wait": "timeout",
                                     "native_ready_after_fifo_registration_error": sorted(ready),
                                     "read_errno": observed[0], "subsequent_commits_observed": 2})

    def test_healthy_fifo_idle_driver_version_check_frequency(self):
        # Reuse the IDLE01 Run and its offline Driver so the read cost is measured
        # in its ordinary five-second Driver loop, including the original work per pass.
        fixture = test_p2a_idle.IdleBackoff("test_idle01_exponential_cap_and_reset")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        checks, waits = [], []
        fixture.db.set_trace_callback(lambda sql: checks.append(time.monotonic())
                                      if sql.strip().lower() == "pragma data_version" else None)
        self.addCleanup(fixture.db.set_trace_callback, None)
        wait = WorkspaceWait.wait
        def measured(listener, delay, drivers=()):
            self.assertIsNotNone(listener.fd)
            before = time.monotonic()
            result = wait(listener, delay, drivers)
            waits.append({"requested_s": delay, "blocked_s": time.monotonic() - before})
            self.assertEqual(result, set())
            return result
        started, cpu = time.monotonic(), time.process_time()
        with closing(Driver(fixture.db, fixture.dir, fixture.rid)) as worker, patch.object(WorkspaceWait, "wait", measured):
            result = worker.drive(5)
        wall, used = time.monotonic() - started, time.process_time() - cpu
        self.save("idle-overhead.json", {"wall_s": wall, "process_cpu_s": used, "waits": waits,
                                         "pragma_data_version_times_s": [t - started for t in checks],
                                         "reads_including_initial": len(checks), "reads_per_second": len(checks) / wall})
        self.assertEqual(result["driver"], "time_limit")
        self.assertGreater(sum(sample["blocked_s"] for sample in waits), 4)
        self.assertLessEqual(len(waits), 8)
        self.assertLess(used, wall * .5)
        self.assertGreaterEqual(len(checks), 10)
        self.assertLessEqual(len(checks), wall / .25 + 2)
