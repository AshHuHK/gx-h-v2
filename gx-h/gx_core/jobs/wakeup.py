"""Best-effort local hint after a durable change. Also loaded by the isolated Job runner."""

import os
import stat


PIPE_NAME = ".gx-driver-wake"


def notify(directory):
    """One byte is enough for any number of changes. The workspace may have no reader."""
    try:
        fd = os.open(os.path.join(directory, PIPE_NAME), os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            if stat.S_ISFIFO(os.fstat(fd).st_mode):
                os.write(fd, b"w")
        finally:
            os.close(fd)
    except OSError:
        # No listener, a full pipe, or the workspace moved. Durable work survives all three.
        pass
