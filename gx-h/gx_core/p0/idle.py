"""Interruptible waits for the process that already drives a GX workspace."""

import os
import selectors
import time

from ..jobs.wakeup import PIPE_NAME, open_fifo
from ..persistence import store


MIN_SECONDS = 0.1
MAX_SECONDS = 4.0
VERSION_CHECK_SECONDS = .25


class WorkspaceWait:
    """Listen before inspecting durable work so notifications arriving during a pass survive."""

    def __init__(self, conn):
        self.conn = conn
        self.path = store.data_root(conn) / PIPE_NAME
        self.fd = None
        self.version = conn.execute("PRAGMA data_version").fetchone()[0]
        self.next_version_check = 0.

    def __enter__(self):
        try:
            os.mkfifo(self.path, 0o600)
        except OSError:
            pass  # Reuse an existing private FIFO where available.
        self.fd = open_fifo(self.path, os.O_RDWR)
        if self.fd is not None:
            self.next_version_check = time.monotonic() + VERSION_CHECK_SECONDS
        store.driver_connections.add(self.conn)
        return self

    def __exit__(self, *exc):
        store.driver_connections.discard(self.conn)
        self._close_fifo()

    def _close_fifo(self):
        if self.fd is not None:
            # The stable workspace name also serves the next owner and any direct
            # library reader. Only this descriptor belongs to this waiter.
            fd = self.fd
            self.fd = None
            try:
                os.close(fd)
            except OSError:
                pass

    def wait(self, seconds, drivers=()):
        """None wakes the workspace, a set identifies native peers, an empty set is a timeout."""
        if seconds <= 0:
            return set()
        with selectors.DefaultSelector() as selector:
            if self.fd is not None:
                try:
                    selector.register(self.fd, selectors.EVENT_READ, None)
                except OSError:
                    self._close_fifo()
                    self.next_version_check = 0.
            for driver in drivers:
                for fd in driver.read_fds:
                    selector.register(fd, selectors.EVENT_READ, driver.rid)
            until = time.monotonic() + seconds
            ready = set()
            while True:
                # Another reader may drain the hint, and some writers bypass the
                # FIFO. Keep a bounded fallback across both long and short waits.
                now = time.monotonic()
                if None in ready or now >= self.next_version_check:
                    version = self.conn.execute("PRAGMA data_version").fetchone()[0]
                    changed = self.version != version
                    self.version = version
                    self.next_version_check = now + VERSION_CHECK_SECONDS
                    if None in ready or changed:
                        if self.fd is not None:
                            try:
                                os.read(self.fd, 65536)
                            except BlockingIOError:
                                pass  # A peer already drained the readable hint.
                            except OSError:
                                self._close_fifo()
                        return None
                if ready:
                    return ready
                if now >= until:
                    return set()
                ready = {key.data for key, _ in selector.select(min(until, self.next_version_check) - now)}
