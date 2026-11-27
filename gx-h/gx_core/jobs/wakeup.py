"""Best-effort local hint after a durable change. Also loaded by the isolated Job runner."""

import os
import stat


PIPE_NAME = ".gx-driver-wake"


def _private_fifo(info):
    return (stat.S_ISFIFO(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600)


def open_fifo(path, access):
    """Open a private FIFO belonging to this user. An unusable hint channel is optional."""
    try:
        before = os.lstat(path)
        if not _private_fifo(before):
            return None
        fd = os.open(path, access | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            if os.path.samestat(before, opened) and _private_fifo(opened):
                return fd
        except OSError:
            pass
        os.close(fd)
    except OSError:
        pass
    return None


def notify(directory):
    """One byte is enough for any number of changes. The workspace may have no reader."""
    fd = open_fifo(os.path.join(directory, PIPE_NAME), os.O_WRONLY)
    if fd is None:
        return
    try:
        os.write(fd, b"w")
    except OSError:
        pass  # The last reader may have closed or the pipe may be full.
    finally:
        os.close(fd)
