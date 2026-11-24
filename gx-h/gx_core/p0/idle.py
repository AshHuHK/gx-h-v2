"""Interruptible waits for the process that already drives a GX workspace."""

import os
import selectors
import stat
import time

from ..jobs.wakeup import PIPE_NAME
from ..persistence import store


MIN_SECONDS = 0.1
MAX_SECONDS = 4.0


class WorkspaceWait:
    """Listen before inspecting durable work so notifications arriving during a pass survive."""

    def __init__(self, conn):
        self.conn = conn
        self.path = store.data_root(conn) / PIPE_NAME
        self.fd = None
        self.version = conn.execute("PRAGMA data_version").fetchone()[0]

    def __enter__(self):
        try:
            try:
                os.mkfifo(self.path, 0o600)
            except FileExistsError:
                pass  # An abruptly terminated Driver may leave its empty FIFO behind.
            fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW)
            if stat.S_ISFIFO(os.fstat(fd).st_mode):
                self.fd = fd
            else:
                os.close(fd)
        except OSError:
            pass  # Some filesystems cannot host a FIFO. Keep servicing the same deadlines.
        store.driver_connections.add(self.conn)
        return self

    def __exit__(self, *exc):
        store.driver_connections.discard(self.conn)
        if self.fd is not None:
            try:
                if os.path.samestat(os.fstat(self.fd), self.path.lstat()):
                    self.path.unlink()
            except FileNotFoundError:
                pass
            finally:
                os.close(self.fd)

    def wait(self, seconds, drivers=()):
        """None wakes the workspace, a set identifies native peers, an empty set is a timeout."""
        if seconds <= 0:
            return set()
        with selectors.DefaultSelector() as selector:
            if self.fd is not None:
                selector.register(self.fd, selectors.EVENT_READ, None)
            for driver in drivers:
                for fd in driver.read_fds:
                    selector.register(fd, selectors.EVENT_READ, driver.rid)
            until = time.monotonic() + seconds
            while True:
                left = until - time.monotonic()
                if self.fd is None:
                    version = self.conn.execute("PRAGMA data_version").fetchone()[0]
                    if version != self.version:
                        self.version = version
                        return None
                if left <= 0:
                    return set()
                ready = {key.data for key, _ in selector.select(left if self.fd is not None else min(left, .25))}
                if ready:
                    break
        if None in ready:
            os.read(self.fd, 65536)
            return None
        return ready
